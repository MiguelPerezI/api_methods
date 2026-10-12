# Dataiku API Methods

Standalone scripts that operate on a Dataiku DSS instance through its
**public REST API** (version 15 — <https://doc.dataiku.com/dss/api/15/rest/>).

Rules every script in this collection follows:

- **Stdlib-only Python 3** — runs on any Ubuntu box, nothing to `pip install`.
- **No secrets in argv or code** — API key from env `DKU_API_KEY` or
  `--api-key-file`, never a `--api-key` flag (keeps keys out of `ps` and
  shell history).
- **No machine-specific paths** — hosts, ports and projects are always flags
  or env vars.
- **One script = one API method**, paired with a sibling `dss_*.md` that
  records the API research, design decisions, verification and full history.
- **Testable offline** — DSS instances here are air-gapped, so every script is
  verified against `tests/mock_dss_server.py` before use.

## Conventions

| Thing | Convention |
|---|---|
| Base URL | `--url http://host:port` (fallback: env `DSS_URL`); all calls under `/public/api` |
| Auth | `Authorization: Bearer <key>`; an admin-privilege key satisfies every endpoint's READ_CONF/WRITE_CONF needs |
| Privileges | documented per endpoint in each script's `.md` |
| Exit codes | `0` ok · `1` API/other failure · `3` target not found · `2` usage error |
| Docs | each `dss_*.py` ships with `dss_*.md` (design + history); this file is the master index |

## Script index

| Script | Purpose | Endpoints used | Design doc |
|---|---|---|---|
| [dss_restart_webapp.py](dss_restart_webapp.py) | One-shot: restart a project webapp's backend and verify it came back up | `GET /projects/{pk}/webapps/` · `GET …/backend/state` · `PUT …/backend/actions/restart` | [dss_restart_webapp.md](dss_restart_webapp.md) |
| [dss_webapp_console.py](dss_webapp_console.py) | Interactive terminal UI: pick project → pick webapp → status/start/stop/restart | all of the above · `GET /projects/` · `PUT …/backend/actions/stop` | [dss_webapp_console.md](dss_webapp_console.md) |
| [dss_webappctl.py](dss_webappctl.py) | One-shot, scriptable webapp-backend control: `--action status\|start\|stop\|restart` with idempotent start/stop (state pre-checks) and a confirming wait — the automation counterpart of the console | same webapp endpoints as the console | [dss_webappctl.md](dss_webappctl.md) |
| [dss_codeenv_console.py](dss_codeenv_console.py) | Interactive terminal UI for code envs: list → status/packages/save/update/rebuild/jupyter/delete/create | `GET·POST·PUT·DELETE /admin/code-envs…` · `POST …/packages` · `POST …/jupyter` | [dss_codeenv_console.md](dss_codeenv_console.md) |
| [dss_bashrc_scenario.py](dss_bashrc_scenario.py) | Guided one-shot: list projects → pick one → create a `custom_python` scenario that prints the DSS host's `~/.bashrc` → run it → show the run log | `GET /projects/` · `GET·POST /projects/{pk}/scenarios/` · `GET·PUT …/payload` · `POST …/run` · `GET …/get-last-runs` · `GET …/{runId}/log` | [dss_bashrc_scenario.md](dss_bashrc_scenario.md) |
| [dss_proxyfix_scenario.py](dss_proxyfix_scenario.py) | Remediation: scenario that backs up `~/.bashrc` of the DSS backend user, removes the target line (a proxy line via `--proxy`, or any line containing a `--phrase`), and restarts DSS — the script watches the outage and confirms DSS comes back | same scenario endpoints as the bashrc tool · `GET /projects/` as the back-online probe | [dss_proxyfix_scenario.md](dss_proxyfix_scenario.md) |
| [dss_proxydiag_scenario.py](dss_proxydiag_scenario.py) | READ-ONLY incident diagnosis: scenario that reports proxy variables in the backend's environment (incl. `/proc` per-process), proxy lines in shell + DSS config files, the SSO endpoints found there, and DNS/TCP/direct-HTTPS reachability of proxy and IdP from the DSS host — then offers to delete the scenario again | same scenario endpoints as the bashrc tool · `DELETE …/scenarios/{id}` | [dss_proxydiag_scenario.md](dss_proxydiag_scenario.md) |
| [payload_strip_all_proxy.py](payload_strip_all_proxy.py) | Incident fix payload for `dss_proxyfix_scenario.py --payload-file`: removes **every** `*proxy*` export from `~/.bashrc` (except `no_proxy`) + JVM `-D*proxy*` lines, and strips **all** proxy vars from the relaunch environment | runs via dss_proxyfix_scenario.py | [dss_proxyfix_scenario.md](dss_proxyfix_scenario.md) |
| [dss_scenariorun_console.py](dss_scenariorun_console.py) | Interactive scenario runner: pick project → pick one of its **existing** scenarios (with last-run markers) → confirm → fire the manual trigger → poll → show outcome + run log → optional run-another loop (creates nothing) | `GET /projects/` · `GET /projects/{pk}/scenarios/` · `POST …/run` · `GET …/get-last-runs` · `GET …/{runId}/log` | [dss_scenariorun_console.md](dss_scenariorun_console.md) |
| [dss_scenariocopy_console.py](dss_scenariocopy_console.py) | Two-instance scenario console: pick a project on the source DSS → pick a scenario → run / delete / **copy to a second (target) DSS** (pick its project there; definition + payload, created inactive, clash menu overwrite/keep-both/abort) → then run / delete the copy on the target | all of the above · `GET /projects/{pk}/scenarios/{id}/` (full definition) · `POST /projects/{pk}/scenarios/` (full body, requested id) · `PUT …/scenarios/{id}` (settings) · `GET·PUT …/payload` · `DELETE …/scenarios/{id}` | [dss_scenariocopy_console.md](dss_scenariocopy_console.md) |
| [dss_usergroups_console.py](dss_usergroups_console.py) | Interactive user admin: pick a user → stage group adds/removes (multi-pick; admin groups marked) and a DSS profile change → one GET-round-trip save with re-read verify (needs an **admin-privilege** key) | `GET /admin/users/` · `GET /admin/users/{login}` · `PUT /admin/users/{login}` · `GET /admin/groups` | [dss_usergroups_console.md](dss_usergroups_console.md) |

### dss_restart_webapp.py — design in brief

DSS restarts a webapp backend with `PUT …/webapps/{webappId}/backend/actions/restart`
(WRITE_CONF, returns an async *future*), and `GET …/backend/state` reports
`{projectKey, webAppId}` **only** when the backend is down — that asymmetry is
the liveness signal. Since the API addresses webapps by internal id (not name),
the script resolves `--webapp` against the project's listing first, then
restarts and polls state every 2 s until the backend confirms. Full research,
decisions and the nine verified scenarios: [dss_restart_webapp.md](dss_restart_webapp.md).

### dss_webapp_console.py — design in brief

The interactive counterpart: connects to the DSS host (URL from `--url` /
env `DSS_URL` / hand-edited defaults at the top of the file, or an
interactive prompt), lists the projects visible to the API key, then the
webapps of the chosen project (with `[up]`/`[down]` markers), then offers
status / start / stop / restart on the chosen webapp. DSS has **no dedicated
start endpoint**, so START is the restart action guarded by a state pre-check
(no-op when already running); every action is confirmed by polling
`backend/state`. Design decisions and the verified session flows:
[dss_webapp_console.md](dss_webapp_console.md).

### dss_webappctl.py — design in brief

The scriptable counterpart of both webapp tools: one invocation, one
action — `--action status|start|stop|restart --project KEY --webapp
ID|NAME`. Start and stop are **idempotent** (a `backend/state` pre-check
short-circuits to "nothing to do", exit 0); restart always fires; the
wanted state is then confirmed by polling (`--no-wait` to skip), and a
`--wait-timeout` hit is deliberately exit 0 with a stderr warning —
"accepted, unconfirmed" (the action stands on DSS). Exit codes match
dss_restart_webapp.py (3 = unknown project/webapp incl. listing 404s,
Ctrl+C = 130), so the three webapp scripts are interchangeable parts:
one-shot restart-only, this one-shot four-action ctl, and the console.
Full design and the verified flows: [dss_webappctl.md](dss_webappctl.md).

### dss_codeenv_console.py — design in brief

The code-env counterpart — but everything lives under `/admin/code-envs`
and needs an **admin-privileged key**. Connects, lists the instance code
envs (with `[out-of-date]`/`[cannot-update]` markers), then per env:
status (requested vs installed packages), a line editor for the requested
packages, save (GET-round-trip PUT, 204), update (install requested, no
rebuild) and rebuild (`?forceRebuildEnv=true` — the API has **no dedicated
rebuild endpoint**; both actions are synchronous, hence a 300 s default
per-call timeout), Jupyter integration toggle, typed-confirmation delete,
and a guided create. Edits stay in memory until saved, and install actions
always run on the *saved* server definition — unsaved edits never reach
DSS as a side effect. Full research and the verified flows:
[dss_codeenv_console.md](dss_codeenv_console.md).

### dss_bashrc_scenario.py — design in brief

The scenario tool: a "python snippet scenario" in DSS is a `custom_python`
scenario whose code lives behind `…/scenarios/{id}/payload`
(`PUT {"script": …}`, not in the definition). The script connects, lists
the projects, lets you pick one (or takes `--project`), then creates the
scenario with the minimal client-validated body (`{name, type, params}`)
— reusing a same-name scenario instead of piling up duplicates (DSS
allows duplicate names) — sets the payload (a pure-stdlib snippet that
prints host/user/home + `~/.bashrc`), verifies it round-trip, fires the
manual trigger and polls `get-last-runs` (a run is finished exactly when
it carries a `result`; `POST …/run` itself only returns an async trigger
fire) until the run ends, then prints the plain-text run log — the
"let me see the bashrc" part — and points at the same log in the DSS UI.
Full research (incl. the docs-vs-client create-response quirk and the
no-`runs`-segment log path) and the verified flows:
[dss_bashrc_scenario.md](dss_bashrc_scenario.md).

### dss_proxyfix_scenario.py — design in brief

The remediation built on that diagnostic: same scenario machinery, but
the payload backs up `~/.bashrc` of the user the scenario runs as,
removes the target line — either a proxy line (`--proxy VAR=HOST:PORT`,
host-anchored regex, any quoting; the default targets the
`https_proxy=naappproxy…` line) or **any line containing a given phrase**
(`--phrase TEXT`, matched literally, e.g. an alias or `ulimit -n 4096`) —
using an atomic rewrite with preserved permissions and a re-read
verification, then launches `<dss-bin> restart` **detached** — the
scenario runs inside the backend it kills, so the restart must survive
its parent's death and the payload flushes its report first. The
restart's environment is **stripped of the target variable** (implied by
`--proxy`, opt-in `--strip-env`): the scenario process inherited it from
the current backend and `bin/dss` does not re-source `~/.bashrc`, so
without the strip the *new* backend would come up proxied again. When
nothing matches, nothing is written and DSS is not restarted. The driver
writes `PROXYFIX-RESULT:` markers into the run log and reads them back to
tell the outcomes apart, then watches with a short-timeout probe client:
wait for the outage (any API failure = DSS restarting), wait for
`GET /projects/` to answer again, report the run's fate (killed by the
restart — expected). A default-**NO** confirmation gate covers the
destructive step (`--yes` for automation). The mock simulates the
self-restart (killed run + 503 outage window). Full design and verified
flows: [dss_proxyfix_scenario.md](dss_proxyfix_scenario.md).

### dss_proxydiag_scenario.py — design in brief

The READ-ONLY diagnosis for the incident that made the proxyfix tool
necessary, built for the day the proxy lines actually broke something:
after `http_proxy`/`https_proxy` lines were added to the backend user's
`~/.bashrc` and DSS relaunched, SSO login failed with
`ERR_OPENID_IDP_EXCHANGE_CODE_FAILURE` (`UnknownHostException` on the
proxy host) — the backend's OIDC code exchange was being sent through a
proxy the DSS server can't resolve. When the server has no reachable
shell, scenario injection is the only diagnostic channel (and SSO being
down doesn't block it: the public API authenticates the key itself, never
the IdP). The payload reports, in one run: the proxy variables in **the
scenario process's environment — a child of the backend, so its inherited
env IS the backend's env** (telling valued apart from present-but-empty —
empty is inert, and DSS-internal `DKU_HPROXY_*` vars don't count); every
backend java process (start time, JVM `-D*proxy*` options,
`/proc/<pid>/environ` proxy vars); every `proxy` line in the shell
startup files and the DSS configuration — the datadir is discovered from
the backend's `-Djava.io.tmpdir` when `DSS_DATADIR` is absent (the gap
the first live run hit), and the proxy host is matched as a literal too,
feeding the SSO endpoints discovered there to the probes; DNS + TCP
reachability of proxy and IdP from the DSS host; and a direct HTTPS GET
to the IdP with proxies deliberately ignored (`ProxyHandler({})`) — any
HTTP answer proves login works again the moment the backend stops
proxying. `--grep TEXT` hunts any other phrase; secrets are masked
(`clientSecret` → `***`), nothing is written, nothing is relaunched, and
every section is guarded so one crash can't hide the rest. **Once the
report is printed**, the script offers to delete the scenario again
(default NO, `--delete-scenario` skips the question; the run log goes
with it, so the offer only appears when the report is safely in the
terminal). Full design and the verified scenarios:
[dss_proxydiag_scenario.md](dss_proxydiag_scenario.md).

### payload_strip_all_proxy.py — in brief

Not a driver: an incident payload for
`dss_proxyfix_scenario.py --payload-file`. The built-in tool strips one
env var from the relaunch; this payload removes every `.bashrc` line
exporting a `*proxy*` variable (any case, `no_proxy` kept) or a JVM
`-D*proxy*` option, and strips **all** proxy-named variables from the
relaunch environment, then relaunches DSS detached printing the same
`PROXYFIX-RESULT` markers — so the driver's outage/back-online watching
works unchanged. Verified in a sandbox (relaunched process saw no proxy
vars at all) and against the mock's restart simulation. Details:
[dss_proxyfix_scenario.md](dss_proxyfix_scenario.md).

### dss_scenariorun_console.py — design in brief

The generic scenario runner next to the incident-specific scenario tools:
this one **runs what already exists** — no scenario is created, modified
or deleted (the only POST is the trigger fire the user asked for), which
makes it safe on any project and instance (it does not even require
WRITE_CONF on the payload). Connects, lists the projects visible to the
key, lists the chosen project's scenarios with last-run markers
(`[inactive]`, `(RUNNING now)`, `(last: SUCCESS|FAILED|...)`,
`(never run)` — one cheap `get-last-runs?limit=1` per scenario), then a
confirmatory **default-YES** gate (scenarios change data by design;
`--yes` skips it for automation) before firing `POST …/run`. The run-
match machinery is the diag tool's: 5 s clock-skew window,
`trigger.runId` match with newest-first fallback, 2 s polling until the
run carries a `result` (outcome → SUCCESS/FAILED/ABORTED), then the
plain-text run log. `--scenario` accepts id or name, but DSS allows
duplicate names, so an ambiguous name exits 3 listing the matching ids.
Exit codes carry the run's outcome — FAILED/ABORTED stay 1 even when you
quit at the "run another?" prompt right after (`q`/EOF/Ctrl+C are
otherwise a clean 0) — and a timeout warning leaves the run alone.
Full design and the verified flows:
[dss_scenariorun_console.md](dss_scenariorun_console.md).

### dss_scenariocopy_console.py — design in brief

The two-instance scenario console: pick a project on the **source** DSS,
pick one of its scenarios, then run/delete it there — or **copy** it to a
**target** DSS. The target connects lazily (only when a copy is chosen):
`--target-url` / env `DSS_URL_TARGET` / prompt and `DKU_API_KEY_TARGET` /
`--target-api-key-file` / hidden prompt mirror the source's usual chain.
The copy pulls the **full scenario object** (`GET …/scenarios/{id}/` —
name/type/active/description/params/triggers/reporters) plus the
custom-scenario payload, and recreates it on the target with the **same
id** when free (`POST …/scenarios/` honors a requested id) — or offers
overwrite (`PUT …/scenarios/{id}`, same types only) / keep-both
(`"X (copy)"`, new id) / abort (default) on an id clash. The copy is
always created/brought **inactive** so its automatic triggers cannot fire
before review, and the overwrite path re-PUTs even an empty payload so no
stale script survives. An old DSS without the full-scenario GET falls
back to listing + payload with a loud "steps are NOT copied" note. After
the copy lands, the post-copy menu runs/deletes the copy on the target,
and `b` returns to the source scenario's actions — where deleting the
source is the usual last migration step. Runs share the scenario-run
console's machinery and exit-code conventions. Full design and the
verified two-mock flows:
[dss_scenariocopy_console.md](dss_scenariocopy_console.md).

### dss_usergroups_console.py — design in brief

The user-admin counterpart of the code-env console — same
edit-in-memory-then-save discipline, but everything lives under `/admin`
and therefore needs an **admin-privilege key** (a 403 gets its own hint).
Lists the users (login, display name, profile, group count, `[disabled]`
/ `[LDAP]` markers), then per user: multi-pick **add** from the groups
they're not in (admin groups carry `** admin group **` and warn when
staged), multi-pick **remove** from the effective set, or pick a new **DSS
profile** (the common three + every profile seen on the instance + manual
entry). Staged edits show inline and land in **one** `PUT
/admin/users/{login}` — a GET-round-trip as the docs demand ("MUST have
been previously obtained from a GET user call"; undocumented attributes
pass through untouched) — followed by a re-read comparison. A failed save
keeps the session and the staged edits alive. Full design and the
verified flows: [dss_usergroups_console.md](dss_usergroups_console.md).

## Development / testing

Run any script against the local mock (no DSS needed):

```bash
python3 tests/mock_dss_server.py --port 8765 &
DKU_API_KEY=testkey python3 dss_restart_webapp.py \
    --url http://127.0.0.1:8765 --project TEST --webapp my_webapp
DKU_API_KEY=testkey python3 dss_webappctl.py \
    --url http://127.0.0.1:8765 --project TEST --webapp my_webapp \
    --action restart                  # also: status / start / stop (idempotent)
DKU_API_KEY=testkey python3 dss_webapp_console.py \
    --url http://127.0.0.1:8765     # interactive: project → webapp → actions
DKU_API_KEY=testkey python3 dss_codeenv_console.py \
    --url http://127.0.0.1:8765     # interactive: env list → actions (needs admin key)
printf '3\n\n' | DKU_API_KEY=testkey python3 dss_bashrc_scenario.py \
    --url http://127.0.0.1:8765     # scripted answers: project TEST, run, default prompts
DKU_API_KEY=testkey python3 dss_scenariorun_console.py \
    --url http://127.0.0.1:8765     # interactive: project → scenario → confirm → run
printf '3\n1\n\n\n' | DKU_API_KEY=testkey python3 dss_scenariorun_console.py \
    --url http://127.0.0.1:8765     # scripted: project TEST, scenario 1, confirm, decline again
DKU_API_KEY=testkey python3 dss_proxyfix_scenario.py \
    --url http://127.0.0.1:8765 --project TEST --yes   # restart sim: outage + back online
printf '\n' | DKU_API_KEY=testkey python3 dss_proxydiag_scenario.py \
    --url http://127.0.0.1:8765 --project TEST  # runs, then EOF keeps the scenario
printf 'y\ny\n' | DKU_API_KEY=testkey python3 dss_proxydiag_scenario.py \
    --url http://127.0.0.1:8765 --project TEST  # runs and deletes the scenario again
# the copy console needs TWO mocks (source + target):
python3 tests/mock_dss_server.py --port 8766 &
DKU_API_KEY=testkey DSS_URL=http://127.0.0.1:8765 \
    DKU_API_KEY_TARGET=testkey DSS_URL_TARGET=http://127.0.0.1:8766 \
    python3 dss_scenariocopy_console.py
#   interactive: project → scenario → run/delete/copy → (copy:) target
#   project → run/delete the copy on the target; --legacy-scenarios on the
#   source mock exercises the old-DSS copy fallback
DKU_API_KEY=testkey python3 dss_usergroups_console.py \
    --url http://127.0.0.1:8765     # interactive: user → stage group/profile edits → save
```

The mock serves the project/webapp, code-env and scenario subsets of the
public API with Bearer auth (token `testkey`), a three-project world
(EMPTY has no webapps; PROD has one down; TEST has one up + one down), a
four-code-env world (incl. an external one that refuses updates), a
scenario world created at runtime (runs execute the payload for real via
`sys.executable -c` after `--run-delay` seconds, so the whole create →
set payload → run → poll → log path is exercised; scenarios can also be
deleted again via `DELETE …/scenarios/{id}`, taking their run history
with them; create accepts the full documented scenario object — a
requested `id` is honored when free, a duplicate is a 400 — and stores
`active/description/params/triggers/reporters`, which
`GET/PUT /projects/{pk}/scenarios/{id}` then serve and replace, with
`--legacy-scenarios` answering 404 there to exercise copy fallbacks), a
users/groups world for the admin security subset (five seeded users incl.
a disabled account and an LDAP one, a five-group pool incl. an admin
group; `PUT /admin/users/{login}` merges a GET-round-trip body and
rejects unknown group names with 400), a **self-restart
simulation** (a run whose payload mentions a `/bin/dss … restart` is
killed mid-run and the mock answers 503 for `--restart-delay` seconds,
exercising the proxyfix tool's outage/back-online watching), DSS-Version
headers, 401/404 DSS-style JSON errors, and simulated delays (webapp
boot, code-env build, scenario run) so polling and waiting logic are
exercised. Extend it when adding scripts that touch new endpoints.