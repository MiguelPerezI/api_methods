# dss_scenariocopy_console.py — design & history

The two-instance scenario console: connect to a **source** DSS, pick a
project, pick one of its existing scenarios, then **run** / **delete** it
there — or **copy** it onto a **target** DSS (pick the target project),
after which the copy on the target can itself be **run** or **deleted**.
This is the migration tool of the collection: the run/delete machinery is
[dss_scenariorun_console.md](dss_scenariorun_console.md)'s, the copy is
new and carries the full scenario definition + payload across instances.

Same connection/auth machinery as every other script, **times two**: the
source follows the usual chain (`--url` > env `DSS_URL` > hand-edited
`CONNECTION DEFAULTS` > prompt; key from env `DKU_API_KEY` /
`--api-key-file` / hidden prompt); the target is the mirror image
(`--target-url` / env `DSS_URL_TARGET` / prompt; key from env
`DKU_API_KEY_TARGET` / `--target-api-key-file` / hidden prompt) and is
connected **lazily** — only when the first copy is chosen, so a
run/delete-only session never needs the target credentials.

## Usage

```bash
# interactive: source projects -> scenarios -> run / delete / copy menu:
DKU_API_KEY=... python3 dss_scenariocopy_console.py

# with the target pre-configured (copy then needs only project picks):
DKU_API_KEY=... DSS_URL_TARGET=http://dss-b:11000 DKU_API_KEY_TARGET=... \
    python3 dss_scenariocopy_console.py

# non-interactive selection of source project / scenario / target project
# (the action menus themselves stay interactive):
DKU_API_KEY=... python3 dss_scenariocopy_console.py \
    --project MYPROJECT --scenario "Nightly rebuild" --target-project HISPROJECT
```

After a copy lands, the post-copy menu offers **run** / **delete** of the
copy on the target, or **back** to the source scenario's actions (where
deleting the source scenario is the usual last migration step). `b`
leaves an action menu for the scenario picker; `q`/EOF/Ctrl+C leaves
anywhere — after a completed run, leaving returns that run's own exit
code (FAILED → 1), like the scenario-run console.

## API facts used

- `GET /projects/` — the project pickers (+ `DSS-Version` banner), for
  both instances.
- `GET /projects/{pk}/scenarios/` — list (ScenarioWithStatus items:
  `id`, `name`, `type`, `active`, `running`, ...); also the copy's
  id-clash check on the target.
- **`GET /projects/{pk}/scenarios/{id}/`** (MONITOR_JOBS) — the full
  scenario object `{id, name, type, active, description, params,
  triggers, reporters}`; for `step_based` scenarios `params.steps` are
  the steps. This is what makes a *faithful* copy possible. (Documented
  with a trailing slash — the client calls it that way; the mock accepts
  either.)
- **`POST /projects/{pk}/scenarios/`** (WRITE_CONF) — create; the body
  is the full scenario object above and **the requested `id` is honored
  when free** (docs list `id` in the request schema), so a copy keeps
  the source id whenever the target project does not already have it.
  (Docs quirk carried over from the bashrc tool: the page documents
  "Response 204", but real DSS returns JSON with at least `id`, which
  the keep-both path requires.)
- **`PUT /projects/{pk}/scenarios/{id}`** (WRITE_CONF, response 204) —
  replace an existing target scenario's settings: the overwrite branch
  of the clash menu. The body's `id` field is stripped there (the id is
  fixed by the path).
- Payload (`GET`/`PUT .../payload`, `{"script": ...}`) — the code of a
  `custom_python` scenario. GET on a non-custom scenario is tolerated as
  "no payload" (docs: "only makes sense for custom scenarios").
- `DELETE /projects/{pk}/scenarios/{id}`, `POST .../run`,
  `GET .../get-last-runs`, `GET .../{runId}/log` — exactly as the
  scenario-run console uses them (trigger-fire semantics; a run is
  finished exactly when it carries `result`; no "runs" path segment).

## Design decisions

1. **The copy is created inactive, always.** `active: False` is forced
   on the target, both for create and overwrite. Triggers/reporters are
   faithfully stored with it, but an unreviewed copy must not start
   answering its automatic triggers on the target instance; the UI
   "Active" toggle is the deliberate last step. This is reported in the
   copy output ("created on target: id X (inactive)").
2. **Copy = definition + payload, verified.** Definition via
   `GET .../scenarios/{id}/`, payload via `GET .../payload`, then after
   creating/updating on the target the payload is PUT and read back
   (round-trip verify, as the bashrc tool does). A copy report prints a
   one-line inventory: N step(s) / trigger(s) / reporter(s), payload
   lines.
3. **Id-clash menu on the target.** When the target project already has
   the source scenario's id: **overwrite** (PUT settings + payload —
   the re-run-migration case; only offered when the *types* match,
   because a PUT cannot sensibly turn a `step_based` scenario into a
   `custom_python` one), **keep both** (POST without id — DSS assigns
   one — named `"X (copy)"`), or **abort** (default — the empty-answer
   choice that changes nothing). Non-interactive ambiguity is never
   resolved silently.
4. **Old-DSS fallback.** If `GET .../scenarios/{id}/` 404s (older DSS
   without the full-scenario endpoint), the copy falls back to the
   listing item + payload and prints a loud note: name/type/active and
   code are copied, `step_based` steps and triggers/reporters are NOT.
   (The mock's `--legacy-scenarios` flag makes GET/PUT on the single-
   scenario path answer 404 to exercise this.)
5. **Overwrite mirrors the payload even when empty** (`PUT {"script":
   ""}`): otherwise a re-copied scenario whose source payload was
   deleted would leave the target's stale script running under the new
   definition. On plain create this PUT is skipped (a fresh scenario's
   payload is empty anyway).
6. **Runs are the scenario-run console's, verbatim** — fire-ms clock-skew
   window, `trigger.runId` match with newest-first fallback, 2 s polling
   until `result` appears, outcome + plain-text log, FAILED/ABORTED →
   exit 1 which survives leaving the script right after (carried through
   the menus via `state["last_run_code"]`).
7. **Deletes are default-NO confirmed** (scenario + run history are
   gone for good), `--yes` skips the two confirm gates for automation.
   Run, by contrast, fires straight from the menu choice — picking "run"
   in the actions menu *is* the confirmation.
8. **Conventions as the rest of the repo**: stdlib only, `.format()`
   over f-strings, keys never on a `--api-key` flag, exit codes 0 ok /
   1 failure / 3 not-found-or-ambiguous / 2 usage, and the target URL
   equal to the source URL is legal (same-instance project-to-project
   copy) with a printed note.

## Verification

Verified 2026-10-09 against **two** `tests/mock_dss_server.py`
processes at once (source :8765, target :8766 — per-process state, no
cross-talk; `--run-delay 1`). Mock additions this tool needed: `GET` /
`PUT /projects/{pk}/scenarios/{id}` (full object in, 204 out),
create honoring a requested `id` (duplicate → 400) and storing
`active/description/params/triggers/reporters`, and `--legacy-scenarios`.
Seeds (through the API): source TEST got `nightly-etl`
(`custom_python`, id `nightly_copy_1`, scripted payload),
`rebuild-flow` (`step_based`, id `steps_copy_1`, 2 steps + 1 trigger),
`failing one` (payload exits 1) and two same-name `dupe` scenarios.

| Scenario | Result |
|---|---|
| Copy `nightly-etl` to target PROD, back out | id preserved, `active: false` on target, description/type round-tripped, payload identical (curl-verified GET full + payload on target), exit 0 |
| Copy again to same target project → clash → **overwrite** | PUT settings path, single scenario on target, payload round-trip verified |
| Post-copy **run** on target | trigger fire → poll → "run finished: SUCCESS (in 1.0s)", **target-side log contains the copied payload's output** (`hello from source scenario`), then menu offers again |
| Post-copy **delete** (`y` at the gate) | deleted; target list curl-verified `[]`; back to source actions |
| Clash → **keep both** | new DSS-assigned id (`custom_python_1`), name `nightly-etl (copy)`, both ids present on target |
| Clash → **abort** (empty answer = default) | "aborted - nothing was changed on the target"; target list unchanged |
| Copy `rebuild-flow` (`step_based`) to target TEST | full GET on target shows both steps and the temporal trigger, inactive |
| Run on **source** (success + `failing one`) | SUCCESS exit 0 / FAILED exit 1 — and exit 1 survives `b` + `q` right after |
| Delete on source: `n` then `y` | `n` → "not deleted - nothing changed"; `y` → deleted, picker refreshes to 2 scenarios, curl-verified gone; `(last: FAILED)` marker shown after earlier runs |
| Source restarted with `--legacy-scenarios`, copy | loud fallback NOTE printed, copy succeeds from listing + payload (curl-verified on target), steps note applies |
| Type-mismatch clash (target has same id as `step_based`, source is `custom_python`) | overwrite **not offered**; only keep-both / abort; abort leaves target untouched |
| `--project TEST --scenario nightly-etl --target-project EMPTY` | all pickers skipped, clash menu still reached, overwrite works |
| `--scenario dupe` (duplicate names) | "'dupe' matches 2 scenarios by name - pick one by id:" + ids, exit 3; `--scenario nosuch` exit 3; `--project NOPE` exit 3 |
| Target URL = source URL (same-instance copy) | "(note: the target URL is the source instance - this copies within the same DSS)", copy into another project works |
| Wrong source key (401) | "error: HTTP 401 ...", "(the API key was rejected - check DKU_API_KEY / DKU_API_KEY_TARGET)", exit 1 |
| `q` at the first picker | clean exit 0 |
| bashrc tool + scenario-run console regressions on the updated mock | both unchanged, exit 0 (create/payload/run/get-last-runs/log untouched in behavior) |

## History

### v1 — 2026-10-09

- Built for: "user runs script, is prompted the list of projects from
  DSS instance A, chooses one, is listed the scenarios, selects one, and
  is given options run, delete, copy, etc. If copy: prompted with the
  projects of the second/target DSS; once the scenario is copied, is
  prompted with the options to delete or run the scenario."
- Driver + `--url/--target-url`, `--api-key-file/--target-api-key-file`,
  `--project/--scenario/--target-project`, `--yes` (delete gates),
  `--wait-timeout` (240 s default)/`--timeout`/`--insecure`; env
  `DSS_URL`/`DKU_API_KEY` (source) and `DSS_URL_TARGET`/
  `DKU_API_KEY_TARGET` (target, lazy).
- Mock grew full-definition storage + GET/PUT on the single-scenario
  path + requested-id create + duplicate-id 400 + `--legacy-scenarios`;
  everything else (runs, payload, delete, self-restart sim, code envs)
  unchanged — existing tools regression-passed against it.
- Found while verifying: a source run's outcome was lost when leaving
  the console afterwards (clean-quit precedence); fixed by threading the
  last run's exit code through the menus (`state["last_run_code"]`), as
  the scenario-run console does.
