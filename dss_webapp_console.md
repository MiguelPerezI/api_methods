# dss_webapp_console.py — design & history

Interactive terminal UI for managing the webapp backends of a Dataiku DSS
instance: connect → pick a project → pick a webapp → **status / start / stop /
restart**, with numbered menus (no curses, works over plain SSH).

## Usage

```bash
# recommended: configure once by editing the CONNECTION DEFAULTS at the top
# of the file (DSS_HOST / DSS_PORT / DSS_USE_TLS), then just:
DKU_API_KEY=... python3 dss_webapp_console.py

# or point at a host explicitly:
DKU_API_KEY=... python3 dss_webapp_console.py --url http://dss-host:11000

# self-signed TLS:
DKU_API_KEY=... python3 dss_webapp_console.py --url https://dss-host:11443 --insecure
```

Connection precedence: `--url` → env `DSS_URL` → hand-edited defaults →
interactive URL prompt (if nothing is configured). Key precedence: env
`DKU_API_KEY` → `--api-key-file` → hidden `getpass` prompt (never a
`--api-key` flag, which would leak into `ps`/shell history).

Session keys: numbers choose · `r` refresh the current list · `b` back one
level · `p` jump to the project list · `q` quit (EOF and Ctrl+C also quit
cleanly; Ctrl+C during a wait skips the wait without killing the session).

## API research (2026-09-02 — DSS public API 15)

Source: <https://doc.dataiku.com/dss/api/15/rest/> — **Projects** and
**Webapps** sections.

| # | Endpoint | Method | Privileges | Purpose |
|---|---|---|---|---|
| 1 | `/projects/` | GET | READ_CONF | List projects visible to the key (optional `?tags=` filter) |
| 2 | `/projects/{projectKey}/webapps/` | GET | READ_CONF | List webapps; items carry `id`, `name`, `type`, `backendRunning` |
| 3 | `/projects/{projectKey}/webapps/{webappId}/backend/state` | GET | READ_CONF | Backend state; only `{projectKey, webAppId}` when down |
| 4 | `/projects/{projectKey}/webapps/{webappId}/backend/actions/restart` | PUT | WRITE_CONF | Stop-and-relaunch; returns an async *future* |
| 5 | `/projects/{projectKey}/webapps/{webappId}/backend/actions/stop` | PUT | WRITE_CONF | Stop the backend (200, no body) |

Findings that shaped the design:

- **There is no dedicated start endpoint.** The restart action's doc —
  "If the backend was running, stop it and relaunch it" — means restart on a
  stopped backend simply launches it. So the console's **START = restart
  action, guarded by a state pre-check** (no-op + hint when already running).
- **`GET /projects/`** lists only projects where the key holds READ_CONF; the
  documented schema shows just `projectKey`, but `name` (and other fields)
  ride along undocumented — the console displays `name` only when present.
- Every response carries **`DSS-Version` / `DSS-API-Version` headers**, used
  for the "connected to … DSS version …" banner, which doubles as the
  connection/key check (one `GET /projects/` at startup).

## Design decisions

1. **Numbered menus, not curses.** Works over any SSH session with no
   terminal capability requirements and no stdlib-beyond-defaults; simple to
   pipe for testing.
2. **Three nested loops** (projects → webapps → actions) with `Quit` raised on
   `q`/EOF to unwind cleanly from any depth; each level re-fetches its list
   on every render, so state is always fresh and `r` is nearly free.
3. **START/STOP are state-aware no-ops.** Start checks the state first and
   refuses to "start" a running backend (points at restart); stop likewise
   skips an already-stopped one. Restart always fires the restart action.
   This prevents accidental backend kills.
4. **Every action is confirmed by polling `backend/state`.** Start/restart
   poll every 2 s until a `futureId` appears (`--wait-timeout`, default 60);
   stop polls every 1 s until the state object is bare. Ctrl+C during a wait
   skips the wait — the request itself stands.
5. **API errors never crash the session.** Screen-level fetch failures show
   inline and the menu re-prompts (`r` retries, `q` quits); action failures
   print and return to the action menu. Only startup failures (bad URL/key,
   unreachable host) exit with code 1.
6. **Editable connection defaults at the top of the file**, since the
   host/port are fixed per site; flags/env still override for one-offs, and
   a prompt keeps the script usable on a fresh box before editing.
7. **Stdlib only** — `urllib.request`, `getpass`, no `requests`; `.format()`
   over f-strings for Python-version reach; ASCII-only output for SSH safety.

## Known limitations / follow-up candidates

- Only **backend webapps** (Bokeh/Shiny/code webapps with a backend) are
  meaningfully manageable; static/visual webapps appear in listings but have
  no backend actions — DSS's own error is surfaced if attempted.
- **Untrusted webapp code**: DSS refuses backend starts on untrusted code;
  remedy is the trust endpoint (`POST …/actions/trust?trustForEverybody=true`,
  admin key required) — candidate for a future `trust` action.
- Project picker shows only `projectKey` + `name`; the `?tags=` filter is not
  exposed (few projects per instance).
- No "restart all webapps" bulk action.

## Verification

Verified 2026-09-02 offline against `tests/mock_dss_server.py` (mock world:
projects EMPTY/PROD/TEST; TEST has `data_monitor` [SHINY, up] and `my_webapp`
[BOKEH, down]; 4 s simulated boot delay):

| Scenario | Result |
|---|---|
| Full flow: TEST → my_webapp → status (STOPPED) → start (polls, confirms up) → stop (confirms stopped) → restart (confirms up) → b/b/q | clean "bye.", exit 0 |
| Status on a running backend (data_monitor) | `RUNNING (jobId=…, uptime ~3600.0s)`, exit 0 |
| Empty project (EMPTY) | `(no webapps in this project)`, `b` returns to projects |
| URL prompt fallback (no `--url`/`DSS_URL`, defaults untouched) | asks for URL, then proceeds |
| Bad API key at startup | error, exit 1 |
| Truncated input → EOF mid-session | clean "bye.", exit 0 |
| Regression: `dss_restart_webapp.py` against the extended mock | unchanged behavior |

## History

### v1 — 2026-09-02

- Researched `GET /projects/` (READ_CONF, key-filtered, `projectKey`-only
  documented schema) and the stop action (PUT, WRITE_CONF, 200 empty);
  confirmed the API has **no dedicated start endpoint** → START = restart
  action + state pre-check.
- Implemented the three-level interactive console (projects → webapps →
  actions), DSS-version connect banner, state-aware start/stop, polling
  confirmation with Ctrl+C escape, per-screen error recovery, URL resolution
  chain ending in an interactive prompt, `getpass` key fallback.
- Extended `tests/mock_dss_server.py` with the projects-listing endpoint and
  a three-project world; verified the seven scenarios above plus the
  script-#1 regression.