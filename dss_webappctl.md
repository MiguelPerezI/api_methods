# dss_webappctl.py — design & history

One-shot, scriptable control of a DSS project webapp **backend**:
`--action status|start|stop|restart` with state pre-checks and a
confirming wait. This is the non-interactive counterpart of
[dss_webapp_console.md](dss_webapp_console.md) and the generalization of
[dss_restart_webapp.md](dss_restart_webapp.md) (which only restarts) —
same API mechanics, packaged for runbooks, cron and CI: exit codes carry
the outcome, no prompts, nothing interactive anywhere.

## Usage

```bash
DKU_API_KEY=... python3 dss_webappctl.py --url http://dss-host:11000 \
    --project MYPROJECT --webapp my_webapp --action restart

DKU_API_KEY=... python3 dss_webappctl.py --url ... --project P --webapp W1 \
    --action start --no-wait          # idempotent, fire-and-forget

DKU_API_KEY=... python3 dss_webappctl.py --url ... --project P --webapp W1 \
    --action stop
```

`--webapp` takes an id **or** a name; an exact id match wins, an unknown
ref exits 3 listing what's available, an ambiguous name (DSS allows
duplicates) fails with the matching ids so the caller can re-run by id.
Batch across a project's webapps is a shell loop over this script — no
client-side batching, by design.

## API facts used (research: dss_webapp_console.md)

| Endpoint | Method | Privileges | Purpose |
|---|---|---|---|
| `/projects/{pk}/webapps/` | GET | READ_CONF | Resolve `--webapp` id/name |
| `/projects/{pk}/webapps/{id}/backend/state` | GET | READ_CONF | Liveness + pre-checks + confirm polls |
| `/projects/{pk}/webapps/{id}/backend/actions/restart` | PUT | WRITE_CONF | start (guarded) / restart |
| `/projects/{pk}/webapps/{id}/backend/actions/stop` | PUT | WRITE_CONF | stop |

- **There is no start endpoint.** START is the restart action guarded by
  a state pre-check; the docs' restart is "launch the backend (stop it
  and relaunch it if running)", so from a down state restart == start.
- **Liveness asymmetry**: `backend/state` returns only
  `{projectKey, webAppId}` when down; a `futureId` present means up. That
  single check drives the pre-checks and the post-action confirmation.
- Actions return an async *future* (`jobId`, `alive`); the stop future
  carries `hasResult` when done.

## Design decisions

1. **Idempotent start/stop, honest restart.** `start` on a running
   backend and `stop` on a stopped one print "nothing to do" and exit 0
   without touching DSS actions — safe in cron. `restart` always fires
   (that's what restart means).
2. **Confirm by polling, forgive the timeout.** After an action the state
   is polled every 2 s until the wanted state shows (up for
   start/restart, down for stop). A `--wait-timeout` hit prints
   "accepted but unconfirmed" to **stderr** and exits **0** — deliberate,
   as in dss_restart_webapp.py: the action stands on DSS, only the
   confirmation is missing. Runbooks that must gate on boot should check
   the `backend is up`/`backend is down` lines (or use `--no-wait` and
   poll `--action status` themselves).
3. **Same resolver, same exit codes as dss_restart_webapp.py** (404 → 3
   incl. unknown project, failure → 1, usage → 2, Ctrl+C → 130) so the
   two one-shots are interchangeable in scripts; this one only adds the
   `stop` call and the `--action` switch.
4. **No prompts, no hidden state, no config file**: one-shot style —
   `--url`/env `DSS_URL` required, key from env `DKU_API_KEY` /
   `--api-key-file` only (never a flag; `ps` and shell history stay
   clean). Stdlib only, `.format()` over f-strings.

## Verification

Verified 2026-10-09 against `tests/mock_dss_server.py` (no mock changes:
the webapp subset already served every endpoint — TEST: W1 `my_webapp`
down + W2 `data_monitor` up; PROD: P1 `scoring_console` down; EMPTY
webapp-less). Mock was run with `--boot-delay 1` (fast boot) and
`--boot-delay 6` (timeout checks).

| Scenario | Result |
|---|---|
| `status` on down W1 / up W2 | "backend NOT running" / "backend RUNNING (jobId=MOCKJOB-W2, alive=True, ...)", exit 0 |
| `start` on **up** W2 | "already running (...) - nothing to do", exit 0, **no action call fired** |
| `start` on down W1 | restart accepted → poll → "backend is up" (1.0 s boot), exit 0; state confirmed via curl |
| `restart` on up | accepted, re-poll → up, exit 0 |
| `stop` on up | stop accepted (hasResult=True) → "backend is down", exit 0 |
| `stop` on down | "already stopped - nothing to do", exit 0 |
| `start --no-wait` on down | accepted, returns immediately, exit 0 |
| `restart --wait-timeout 1` with boot-delay 6 | stderr "accepted but ... did not confirm the wanted state within 1s", exit 0 (action stood; next status showed up) |
| unknown webapp in TEST | "no webapp with id or name 'nosuch' ... available: W1 'my_webapp', W2 'data_monitor'", exit 3 |
| EMPTY (no webapps) / unknown project | "'EMPTY' ... project has no webapps", exit 3 / 404 listing error, exit 3 |
| wrong API key | HTTP 401 error, exit 1; missing required flags → argparse exit 2 |
| Ambiguous duplicate webapp names | resolver shared with dss_restart_webapp.py: fails with the matching ids, exit 1 (not re-shown live: the mock world seeds no duplicate webapp names — same coverage as the sibling tool) |
| Regressions on the same mock | dss_restart_webapp.py restart+verify exit 0; dss_scenariorun_console.py --list exit 0 |

## History

### v1 — 2026-10-09

- Built for: "Create another script to restart/stop/Start DSS project
  webapps" — filling the one remaining webapp shape in the collection:
  console (`dss_webapp_console.py`) and restart-only one-shot
  (`dss_restart_webapp.py`) existed; a **single flag-driven status /
  start / stop / restart** for automation did not.
- Mechanics and the start/stop idempotency rules are the console's
  research, packaged in the restart tool's one-shot skeleton (same
  resolver, same exit-code mapping, same polling constants).
- Found while verifying: nothing new in the API; the only test-setup
  lesson is that a timed-out wait must be exercised with a mock
  `--boot-delay` larger than `--wait-timeout` (and on a freshly-bound
  port — a stale mock on the same port silently answers with the old
  delay).
