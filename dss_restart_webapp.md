# dss_restart_webapp.py — design & history

Restart the backend of a Dataiku DSS project webapp through the public REST
API, then verify the backend actually came back up.

## Usage

```bash
# restart, resolving the webapp by name, then wait for the backend:
DKU_API_KEY=... python3 dss_restart_webapp.py \
    --url http://dss-host:11000 --project MYPROJECT --webapp my_webapp

# only report backend state (no restart):
DKU_API_KEY=... python3 dss_restart_webapp.py \
    --url http://dss-host:11000 --project MYPROJECT --webapp my_webapp --status

# fire and forget, self-signed TLS:
DKU_API_KEY=... python3 dss_restart_webapp.py \
    --url https://dss-host:11443 --insecure --no-wait \
    --project MYPROJECT --webapp W1
```

`--url` also falls back to env `DSS_URL`. The key comes from env `DKU_API_KEY`
or `--api-key-file FILE` — there is deliberately no `--api-key` flag (see design
decisions).

## API research (2026-09-02 — DSS public API 15)

Source: <https://doc.dataiku.com/dss/api/15/rest/>, **Webapps** section.

- All calls are relative to `{dss-url}/public/api` on the DSS server.
- Authentication: `Authorization: Bearer <API_KEY>` header on every call.
- Every response carries `DSS-API-Version` / `DSS-Version` headers (not
  validated by v1).

| # | Endpoint | Method | Privileges | Purpose |
|---|---|---|---|---|
| 1 | `/projects/{projectKey}/webapps/` | GET | READ_CONF | List webapps in a project |
| 2 | `/projects/{projectKey}/webapps/{webappId}` | GET | READ_CONF | Get one webapp definition |
| 3 | `/projects/{projectKey}/webapps/{webappId}/backend/state` | GET | READ_CONF | Current backend state |
| 4 | `/projects/{projectKey}/webapps/{webappId}/backend/actions/restart` | PUT | WRITE_CONF | Restart the backend |
| 5 | `/projects/{projectKey}/webapps/{webappId}/backend/actions/stop` | PUT | WRITE_CONF | Stop the backend |
| 6 | `/projects/{projectKey}/webapps/{webappId}/actions/trust?trustForEverybody=` | POST | admin (for `trustForEverybody`) | Trust webapp code |

Endpoints 4–6 are **not** used by v1; documented here because they shape the
follow-up options below.

Response shapes that drive the script:

- **Webapp listing item** (endpoint 1): `{backendRunning, type, projectKey, id,
  name, tags, createdBy, createdOn, lastModifiedBy, lastModifiedOn}`. `id` is
  the short internal id (e.g. `49K1IT7`) — *not* the display name.
- **Restart** (endpoint 4, HTTP 200): a *future* —
  `{hasResult, aborted, alive, startTime, runningTime, unknown, jobId}`.
  The docs: "If the backend was running, stop it and relaunch it." Restarting a
  stopped backend simply launches it.
- **Backend state** (endpoint 3): when the backend is running →
  `{projectKey, webAppId, futureId, futureInfo: {alive, jobId, runningTime,
  jobDisplayName: "Backend for Web app", payload, ...}}`. When it is down →
  only `{projectKey, webAppId}` — this asymmetry is what the script polls on.

## Design decisions

1. **Resolve by id or name.** Humans know webapp names; the API wants the
   internal id. The script lists the project's webapps once, prefers an exact
   id match, then an exact name match. Ambiguous name → error; no match →
   error listing available webapps (capped at 15), exit 3.
2. **Key via env `DKU_API_KEY` or `--api-key-file` only.** No `--api-key` flag
   so the key never appears in `ps` output or shell history. (Same env
   convention as the DataGate webapp.)
3. **Verify after restart.** Restart is asynchronous — it returns a future, not
   a running backend. The script polls `backend/state` every 2 s up to
   `--wait-timeout` (default 60 s) until a `futureId` appears. If the restart
   was accepted but the backend never reports state, it warns on stderr and
   still exits 0 (the requested action succeeded; the backend lag is DSS-side).
4. **`--status` mode.** Read-only state check (only needs READ_CONF), useful
   before/after restarts and as a cheap health probe.
5. **`--insecure` flag.** DSS instances are often fronted by self-signed TLS;
   skips certificate verification when given.
6. **Stdlib only** (`urllib.request`, no `requests`), no machine-specific
   paths, `.format()` over f-strings for maximum Python-version reach —
   portable to any Ubuntu box with nothing to install.
7. **Exit codes.** `0` ok · `1` API/other failure · `3` project or webapp not
   found (incl. HTTP 404) · `2` usage error (argparse default). Distinct codes
   so the script can drive automation (cron, other scripts) reliably.

## Known limitations / follow-up candidates

- **Static/visual webapps have no backend**; restarting them fails and the
  DSS error is surfaced as-is. A pre-check on the listing's `type` is possible
  but v1 lets DSS decide rather than hard-coding a type whitelist from one
  docs example.
- **Untrusted webapp code**: DSS refuses to start the backend of an untrusted
  webapp. Remedy is the trust endpoint (#6) — a `--trust` flag is a natural
  v2 addition (admin key is required for `trustForEverybody`, which the user's
  key has).
- **No `--stop`** in v1 (endpoint #5) — trivial sibling flag if ever needed.
- **No `--all`** to restart every webapp in a project.
- The script targets API 15 docs; it does not pin or negotiate versions.

## Verification

Verified 2026-09-02 offline against `tests/mock_dss_server.py` (DSS instances
here are air-gapped):

| Scenario | Result |
|---|---|
| `--status` while backend down | `backend NOT running`, exit 0 |
| Restart by **name** (4 s simulated boot delay) | polls, then `backend is up: jobId=…, alive=True`, exit 0 |
| `--status` while backend up | `backend RUNNING (jobId=…, runningTime=…ms)`, exit 0 |
| Restart by **id** with `--no-wait` | `restart accepted`, exit 0 |
| Wrong API key (401) | exit 1 |
| Unknown project (404) | exit 3 |
| Unknown webapp (resolution miss, shows available) | exit 3 |
| No `DKU_API_KEY` set | exit 1 |
| Unreachable DSS host | exit 1 |

## History

### v1 — 2026-09-02

- Researched the **Webapps** section of the DSS 15 REST API docs; identified
  restart (`PUT …/backend/actions/restart`, WRITE_CONF, returns a future),
  the state endpoint's running/down response asymmetry, and the id-vs-name
  distinction in listing items.
- Implemented: name/id resolution, Bearer auth via `DKU_API_KEY` /
  `--api-key-file`, restart + state-poll verification, `--status`, `--no-wait`,
  `--insecure`, DSS-JSON-aware error reporting, exit codes 0/1/2/3.
- Built `tests/mock_dss_server.py` (mock of the webapp API subset with
  simulated boot delay) and verified all nine scenarios listed above.