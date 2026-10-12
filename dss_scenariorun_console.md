# dss_scenariorun_console.py — design & history

Pick a DSS project, pick one of its **existing** scenarios, run it, and
show the run's status and log. Nothing is created, modified or deleted —
this runs what already exists in DSS (the scenario **tools** here
create scenarios; this **console** fires them).

Same connection/auth machinery as every other script (URL chain
`--url` > env `DSS_URL` > hand-edited `CONNECTION DEFAULTS` > prompt;
key from env `DKU_API_KEY` / `--api-key-file` / hidden prompt); same
terminal-UI helpers as [dss_proxydiag_scenario.md](dss_proxydiag_scenario.md)
(`screen`/`ask`/`Quit`, `DssClient` trimmed to the methods used).

## Usage

```bash
# interactive: connect -> pick project -> pick scenario -> confirm -> run:
DKU_API_KEY=... python3 dss_scenariorun_console.py

# non-interactive project + scenario (id or name), no confirm:
DKU_API_KEY=... python3 dss_scenariorun_console.py \
    --project MYPROJECT --scenario "Nightly rebuild" --yes

# just list a project's scenarios with last-run markers (nothing is run):
DKU_API_KEY=... python3 dss_scenariorun_console.py --project MYPROJECT --list
```

After a run completes the script asks **"Run another scenario from this
project? [y/N]"** — `y` loops back to the scenario picker (with refreshed
last-run markers), `n`/EOF/`q`/Ctrl+C leaves; when a run already
completed, leaving returns that run's own exit code (FAILED → 1),
otherwise a clean quit is exit 0.

## API facts used

- `GET /projects/` returns the plain list of projects (+ `DSS-Version`
  header, used for the banner).
- `GET /projects/{pk}/scenarios/` returns a list of scenario objects
  (`id`, `name`, `type`, `active`, ...).
- A manual run = `POST …/scenarios/{id}/run` with body `{}` — an **async
  trigger fire**: `{runId, trigger, cancelled}`. `cancelled: true` means
  the scenario did not start (already running, or another trigger won).
- The run is finished exactly when the matching entry of
  `GET …/scenarios/{id}/get-last-runs?limit=N` (newest-first) carries a
  `"result"` key (`result.outcome`: SUCCESS / FAILED / ABORTED / ...).
  Matching: prefer the run whose `trigger.runId` equals the fire's
  `runId`; else the newest run started after the fire (5 s skew).
- The log is plain text at `…/scenarios/{id}/{runId}/log` — **no "runs"
  segment** in the path. A scenario whose steps emit nothing can return
  an empty log (shown as "(the run produced no log output)").
- Scenarios are addressed by internal id; names may be duplicated across
  scenarios (DSS allows it), so `--scenario` resolves id first and name
  second, and an **ambiguous name exits 3** listing the matching ids.
- Last-run markers (one `get-last-runs?limit=1` per scenario) need
  RUN_JOBS; a key with only MONITOR_JOBS still gets the scenario list,
  with "(no last-run info)" markers.

## Design decisions

1. **Nothing is written.** The console runs existing scenarios only — no
   create payload/PUT, no delete; the only POST is the trigger fire the
   user asked for. A default-**YES** confirm still gates every run
   (scenarios may change data — that is what they do); `--yes` skips it
   for automation.
2. **Markers, not a status dump.** The scenario list rows carry
   `[inactive]`, `(RUNNING now)`, `(last: SUCCESS|FAILED|...)` or
   `(never run)`, built from one cheap `get-last-runs?limit=1` per
   scenario; a failing call only costs that one marker, never the
   listing.
3. **The run-match machinery is the diag tool's, verbatim** — fire-ms
   clock-skew window, `trigger.runId` match with newest-first fallback,
   poll every 2 s until `result` appears. `Ctrl+C` during the wait leaves
   the run alone — "outcome unknown; it keeps going in DSS" — repeated
   later passes (or `--list`) show its final state via the markers.
4. **Exit codes carry the run's outcome**: SUCCESS/others → 0,
   FAILED/ABORTED → 1, unknown project/scenario (or an ambiguous name)
   → 3, usage → 2 — and this survives quitting at the "run another?"
   prompt (`q`/EOF/Ctrl+C after a completed run returns that run's own
   code, not a blanket clean 0; found in testing).
5. **Conventions as the rest of the repo**: stdlib only (urllib,
   argparse, getpass), `.format()` over f-strings, key never on a
   `--api-key` flag, no machine-specific paths in logic.

## Verification

Verified 2026-10-01 against `tests/mock_dss_server.py` (the only mock
changes this script needed: none — every endpoint already existed for the
scenario tools; four scenarios were seeded through the API itself:
`a-fast` prints a line, `b-fail` exits 1, two scenarios share the name
`dupe`).

| Scenario | Result |
|---|---|
| Interactive: pick project TEST → pick `a-fast` → confirm (default yes) → run → log shown | "run finished: SUCCESS (in 2.0s)", payload output in the log, exit 0 |
| FAILED run (`b-fail`, exits 1) | "run finished: FAILED (in 2.0s)", "(the run produced no log output)", exit 1 — and exit 1 **also** when leaving via `n` / EOF / `q` right after |
| Run another? loop (`y`, then `n`) | picker rendered again with refreshed markers; final exit = last run's outcome |
| Same-name reuse marker check | after earlier runs the picker shows `(last: SUCCESS)`/`(last: FAILED)` — markers update between invocations |
| `--project TEST --scenario a-fast --yes` | non-interactive success run, exit 0 |
| `--scenario dupe` (duplicate name) | full listing + "`'dupe' matches 2 scenarios by name - run one by id`", exit 3; `--scenario custom_python_3` then runs `dupe number one` |
| `--list` | scenario rows with markers, exit 0, nothing fired |
| `--scenario nosuch` | exit 3, "error: scenario 'nosuch' not found in project TEST" |
| `--project EMPTY --list` / interactive pick of EMPTY + `q` | "(no scenarios in this project - …)" hint, exit 0 |
| Run slower than `--wait-timeout` (`--run-delay 8` mock, `--wait-timeout 3`) | "WARNING: the run did not finish within 3s", exit 1, nothing cleaned up (unknown runs are left alone) |
| Wrong API key (401) | "error: HTTP 401 …", "(the API key was rejected - check DKU_API_KEY)", exit 1 |
| bashrc tool regression on the same mock | exit 0, log shown (mock unchanged) |

## History

### v1 — 2026-10-01

- Built for: "list projects for the user to use, user chooses project,
  then choose which scenario to execute, and then the scenario runs" —
  the missing generic runner next to the incident-specific scenario
  tools (`dss_bashrc_scenario.py` / `dss_proxyfix_scenario.py` /
  `dss_proxydiag_scenario.py`).
- Driver + `--list`, `--project`, `--scenario ID|NAME`, `--yes`,
  `--wait-timeout` (240 s default)/`--timeout`/`--insecure`.
- No mock changes required (a first in this collection): the scenario
  subset already covered list / payload / run / get-last-runs / log /
  delete for the previous tools.
- Found while verifying: EOF at the "run another?" prompt after a FAILED
  run returned a blanket 0 (clean-quit precedence); fixed to return the
  last run's own exit code.