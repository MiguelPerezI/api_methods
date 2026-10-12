# dss_bashrc_scenario.py — design & history

Create a `custom_python` scenario in a Dataiku DSS project whose payload
prints the `~/.bashrc` of the DSS host, run it once, and show the run log —
the full flow (list projects → pick one → create scenario → set snippet →
run → display output) driven from the terminal through the public REST API.

## Usage

```bash
# interactive: connect -> pick a project -> create + run + show:
DKU_API_KEY=... python3 dss_bashrc_scenario.py

# non-interactive project choice:
DKU_API_KEY=... python3 dss_bashrc_scenario.py --project MYPROJECT

# create the scenario but do not run it (e.g. run later from the DSS UI):
DKU_API_KEY=... python3 dss_bashrc_scenario.py --project MYPROJECT --no-run

# self-signed TLS + own scenario name:
DKU_API_KEY=... python3 dss_bashrc_scenario.py \
    --url https://dss-host:11443 --insecure --scenario-name "Show bashrc"

# run a custom python payload instead of the built-in bashrc snippet:
DKU_API_KEY=... python3 dss_bashrc_scenario.py --project MYPROJECT \
    --payload-file my_snippet.py
```

`--url` also falls back to env `DSS_URL`; the key comes from env
`DKU_API_KEY` or `--api-key-file FILE` — never a `--api-key` flag (see
design decisions).

## API research (2026-09-30 — DSS public API 15)

Sources:

- REST docs, **Scenarios** + **Projects** sections:
  <https://doc.dataiku.com/dss/api/15/rest/>
- Dataiku's official API client (ground truth for body/field names the docs
  under-specify):
  [`dataikuapi/dss/project.py` `create_scenario()`](
  https://github.com/dataiku/dataiku-api-client-python/blob/master/dataikuapi/dss/project.py)
  and
  [`dataikuapi/dss/scenario.py` `set_payload` / `run` / `get_last_runs` / run-log calls](
  https://github.com/dataiku/dataiku-api-client-python/blob/master/dataikuapi/dss/scenario.py)

- All calls are relative to `{dss-url}/public/api` on the DSS server.
- Authentication: `Authorization: Bearer <API_KEY>` on every call.

| # | Endpoint | Method | Privileges | Purpose |
|---|---|---|---|---|
| 1 | `/projects/` | GET | READ_CONF | List the projects visible to the key |
| 2 | `/projects/{projectKey}/scenarios/` | GET | MONITOR_JOBS | List scenarios (collision check) |
| 3 | `/projects/{projectKey}/scenarios/` | POST | WRITE_CONF | Create the scenario |
| 4 | `/projects/{projectKey}/scenarios/{scenarioId}/payload` | GET | MONITOR_JOBS | Read the python payload back |
| 5 | `/projects/{projectKey}/scenarios/{scenarioId}/payload` | PUT | WRITE_CONF | Set the python payload |
| 6 | `/projects/{projectKey}/scenarios/{scenarioId}/run` | POST | RUN_JOBS | Fire the manual trigger |
| 7 | `/projects/{projectKey}/scenarios/{scenarioId}/get-last-runs?limit=` | GET | RUN_JOBS | Poll the runs |
| 8 | `/projects/{projectKey}/scenarios/{scenarioId}/{runId}/log` | GET | READ_CONF | Plain-text run log |

Response shapes that drive the script (all validated against the official
client, which parses them on real DSS):

- **Create** (endpoint 3): the body is the *definition* — the official
  client sends exactly `{'params': {}, 'type': …, 'name': …}` and reads
  `response['id']` as the scenario id for every later call. The docs'
  fuller request body (`id`, `active`, `description`, `triggers`,
  `reporters`) is optional; the client omits all of it. (Docs quirk: the
  page documents "Response 204", but the official client parses a JSON
  body — real DSS returns JSON with at least `id`, which is what this
  script requires.)
- **Payload** (endpoints 4/5): the python snippet is **not** part of the
  scenario definition; it lives behind `…/payload` with body
  `{"script": <python>}` (GET returns `{"script": …}`). This is the
  documented way a `custom_python` scenario gets its code — the settings
  object of the official client saves the code through this same call.
- **Run** (endpoint 6): returns a **trigger fire** — `{runId, trigger:
  {id, …}, cancelled}` — *not* a scenario run, and `runId` there is the
  **trigger run** id. The manual fire can be cancelled (e.g. the scenario
  is already running); `cancelled: true` means nothing will run.
- **Runs** (endpoint 7): newest-first array of
  `{runId, scenario: {projectKey, id}, trigger, start, end?, result?}`.
  A run is finished **exactly when the `result` key is present**
  (that is how the official client detects completion);
  `result.outcome` ∈ SUCCESS / WARNING / FAILED / ABORTED.
- **Run log** (endpoint 8): plain text (`Content-Type: text/plain`), path
  has **no `runs` segment**: `…/scenarios/{scenarioId}/{runId}/log`.

## Design decisions

1. **`custom_python` scenario type.** DSS scenarios come in two types
   (`step_based`, `custom_python`); "execute a python snippet" is exactly
   what `custom_python` is (the snippet is the scenario payload). The
   create body is the minimal shape the official client sends:
   `{"name": …, "type": "custom_python", "params": {}}` — the scenario id
   comes back in the response and is used verbatim afterwards.
2. **The snippet is pure stdlib** (`os` / `getpass` / `platform`) so it
   runs in the default DSS backend python with no code env. It prints the
   host name, the user the scenario runs as, that user's home and the
   content of `~/.bashrc`. That user is the **DSS backend user** (or the
   scenario's run-as), not the API-key owner — the script says so up front.
   A missing `.bashrc` is a *finding*, not a failure: the run stays SUCCESS
   and the log says so.
3. **Payload verified after write.** PUT `{"script": …}`, then GET it back
   and compare (same round-trip philosophy as the code-env console's
   save). A mismatch warns but does not abort — the run output is the
   authoritative check.
4. **Reuse by name.** DSS allows duplicate scenario names, so before
   creating, the script lists the project's scenarios; on a same-name
   match it offers *reuse + overwrite payload* (default), *create a
   duplicate anyway*, or *abort*. Re-running the tool therefore updates
   the existing scenario instead of piling up clones.
5. **Run is a trigger fire — poll, don't trust.** `POST …/run` only fires
   a manual trigger, so the script polls `get-last-runs` every 2 s and
   matches its own run: first by the trigger-fire `runId` echoed in the
   run's `trigger` object, with a start-time fallback (±5 s skew) taking
   the newest candidate. A run is done when `result` appears;
   `Ctrl+C` leaves the wait (the run itself stands on DSS) with exit 0.
6. **The log is the deliverable.** After the run finishes, the script
   fetches `…/{runId}/log` (text) and prints it — that is the "let the
   user see" part — and points at the same log in the DSS UI
   (project → Scenarios → History) for later viewings.
7. **Exit codes.** `0` ok · `1` API/other failure, cancelled trigger fire,
   run not finished within `--wait-timeout`, or run outcome FAILED/ABORTED
   (the snippet's output never became visible) · `3` `--project` key not
   found/visible · `2` usage error (argparse). A user-interrupted wait is
   exit 0: the scenario and its run stand on DSS.
8. **Key via env `DKU_API_KEY` or `--api-key-file` only** — no `--api-key`
   flag, so the key never appears in `ps` output or shell history (same
   convention as the other scripts in this repo).
9. **Stdlib only** (`urllib.request`, no `requests`), `.format()` over
   f-strings, no machine-specific paths — portable to any Ubuntu box with
   nothing to install.

## Known limitations / follow-up candidates

- The scenario is created with **no triggers** (manual runs only) and the
  default active state; the tool never schedules anything.
- The snippet sees the **backend user's** environment. If DSS runs as a
  service user, that `~/.bashrc` differs from a login shell's — that is
  the point of the diagnostic, but worth knowing.
- Run matching by start-time fallback could mis-attribute if the same
  scenario is manually fired from elsewhere at the same moment (the
  trigger-runId match covers the normal case).
- `--payload-file` runs arbitrary python on DSS as the backend user —
  intended for diagnostics; the tool does not sandbox it (same trust
  level as pasting into the DSS UI).
- No cleanup/delete: the scenario stays in the project (a `--delete` flag
  would be a trivial follow-up via `DELETE /projects/{pk}/scenarios/{id}`).
- The script targets API 15 docs; it does not pin or negotiate versions.

## Verification

Verified 2026-09-30. Live first: the full interactive flow was run by the
user against a **real DSS instance** the same day (scenario created, payload
set, run executed, `~/.bashrc` displayed from the run log) — including
against the external target server, where it revealed the
`https_proxy` proxy line that `dss_proxyfix_scenario.py` was then built to
remove. Offline, all twelve scenarios below were verified against
`tests/mock_dss_server.py`, whose scenario subset executes the payload for
real (`sys.executable -c`) after `--run-delay` seconds, so the whole
create → set payload → run → poll → log path is exercised end to end.

| Scenario | Result |
|---|---|
| Interactive: pick project TEST, create + run, default prompts | scenario created, run SUCCESS, log shows host/user/home + the real `~/.bashrc`, exit 0 |
| Re-run, same scenario name → reuse choice (default) | payload overwritten + round-trip verified, run again SUCCESS, exit 0 |
| Same name → choice 2 (create duplicate) | second scenario created (DSS allows duplicate names), exit 0 |
| Same name → choice 3 (abort) | "aborted - nothing was changed", exit 0 |
| `--project EMPTY` (project with nothing in it) | works, exit 0 |
| `--project NOSUCH` | exit 3, lists the visible projects |
| `--no-run` (no prompts) | scenario + payload set, "run later from the DSS UI", exit 0 |
| `--payload-file` with a snippet that raises | run FAILED, traceback in the printed log, exit 1 |
| `--wait-timeout 1` vs mock `--run-delay 4` | timeout warning, "may still be running", exit 1 |
| Wrong API key (401) | exit 1 |
| Mock not running (unreachable DSS) | exit 1 |
| `q` at the project prompt | "bye.", exit 0 |

Also observed while scripting the piped answers: EOF at *any* prompt
(same as Ctrl+D) exits cleanly with "bye." and 0 — mis-sequenced automation
input cannot leave the tool half-done.

## History

### v1 — 2026-09-30

- Researched the **Scenarios** section of the DSS 15 REST docs plus the
  official `dataiku-api-client-python` source (the docs under-document the
  create response and payload field names): `custom_python` scenario type,
  create → `['id']`, payload behind `PUT …/payload` `{"script": …}`, run
  → trigger fire (async), run finished ⇔ `result` present, log at
  `…/scenarios/{id}/{runId}/log` with no `runs` segment.
- Implemented: project picker (+`--project`), same-name reuse/prompt,
  create with the client-validated minimal body, payload set + round-trip
  verification, manual run + poll + trigger-fire matching, log display,
  `--payload-file` / `--no-run` / `--insecure` / timeouts, exit codes
  0/1/2/3.
- Extended `tests/mock_dss_server.py` with the scenario subset (create,
  payload, run with real payload execution, get-last-runs, text run log)
  and verified the twelve scenarios listed above.