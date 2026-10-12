# dss_codeenv_console.py — design & history

Interactive terminal UI for managing the **instance-level code
environments** of a Dataiku DSS instance: connect → list code envs → pick
one → **status / packages / save / update / rebuild / jupyter / delete**,
plus `n` to create a new env from the list. Plain numbered menus, no
curses — works over any SSH session.

Every code-env endpoint in the API lives under `/admin/code-envs` and
requires an **admin-privileged API key** — this console is the reason the
collection's rule "admin key satisfies every privilege" exists.

## Usage

```bash
# recommended: configure once by editing the CONNECTION DEFAULTS at the top
# of the file (DSS_HOST / DSS_PORT / DSS_USE_TLS), then just:
DKU_API_KEY=... python3 dss_codeenv_console.py

# or point at a host explicitly:
DKU_API_KEY=... python3 dss_codeenv_console.py --url http://dss-host:11000

# self-signed TLS:
DKU_API_KEY=... python3 dss_codeenv_console.py --url https://dss-host:11443 --insecure
```

Connection precedence: `--url` → env `DSS_URL` → hand-edited defaults →
interactive URL prompt (if nothing is configured). Key precedence: env
`DKU_API_KEY` → `--api-key-file` → hidden `getpass` prompt (never a
`--api-key` flag, which would leak into `ps`/shell history).

Session keys: numbers choose · `r` refresh the list · `n` create a new
env · `b` back one level · `q` quit (EOF and Ctrl+C also quit cleanly).

## API research (2026-09-02 — DSS public API 15)

Source: <https://doc.dataiku.com/dss/api/15/rest/> — *Security and code
envs* section (groups "Code envs" and "Code env"). **All of it requires
Admin privileges.**

| # | Endpoint | Method | Privileges | Purpose |
|---|---|---|---|---|
| 1 | `/admin/code-envs` | GET | Admin | List code envs — items: `envName`, `envLang`, `kernelSpecName`, `canUpdateCodeEnv`, `owner`, `isUptodate`, `unknownKernelSpecStatus`, `deploymentMode` |
| 2 | `/admin/code-envs/{envLang}/{envName}` | POST | Admin | Create code env — body `{deploymentMode, pythonInterpreter, installCorePackages, installJupyterSupport}`; answers `{envName, messages}` |
| 3 | `/admin/code-envs/{envLang}/{envName}` | GET | Admin | Get the Code env object (full definition) |
| 4 | `/admin/code-envs/{envLang}/{envName}` | PUT | Admin | Update code env — body MUST be a previously GET-obtained object; **204 No Content** |
| 5 | `/admin/code-envs/{envLang}/{envName}` | DELETE | Admin | Delete code env — answers `{messages}` |
| 6 | `/admin/code-envs/packages{?forceRebuildEnv}` | POST | Admin | Update the packages of a code env; `forceRebuildEnv=true` (default `false`) forces a **full rebuild** |
| 7 | `/admin/code-envs/jupyter{?active}` | POST | Admin | Activate/deactivate the Jupyter integration (`active` required) |

Related but out of scope: **plugin** code envs have their own pair of
endpoints (`POST /plugins/{pluginId}/code-env/actions/create` and
`/actions/update`, which *do* return async futures). They manage a
plugin's embedded env, not the instance code envs this console targets.

Findings that shaped the design:

- **There is no dedicated rebuild endpoint and no async future** for code
  envs (unlike webapp backends). The packages action *is* the installer:
  `POST …/packages` = "update dependencies" (install requested packages
  into the existing env), `POST …/packages?forceRebuildEnv=true` = delete
  and rebuild the env from the requested packages. Both are
  **synchronous** — the call blocks while pip/conda runs inside DSS — so
  this console's default per-call HTTP timeout is 300 s (the webapp
  console's 30 s would cut real rebuilds off) and a hint is printed before
  the call.
- **The `packages` and `jupyter` actions take no env name in the path**,
  so the request body must identify the env. The docs don't show the
  request body for either; the console sends the **Code env object from a
  fresh GET** — never the locally-edited copy — so unsaved edits can
  never reach DSS as a side effect of an install action.
- **Package lists are newline-separated plain strings**, not structured
  lists: `specPackageList` (requested), `actualPackageList` (installed),
  `mandatoryPackageList` (DSS core). The package editor is a line editor
  over `specPackageList`.
- **PUT must round-trip a GET object** (documented warning: undocumented
  attributes may exist and must not be modified). The console loads the
  definition once, edits only `specPackageList`, PUTs the whole object,
  then re-GETs to re-sync.
- **envLang path parameter is documented lowercase** ("python or r") while
  the list returns `envLang: "PYTHON"` — the console lowercases for path
  calls.
- Actions answer with a `messages` object
  (`{anyMessage, error, messages, success, warning}`) — displayed after
  every action; `error` is flagged loudly.

## Design decisions

1. **Numbered menus, not curses** — same reasoning as the webapp console:
   works over any SSH session, pipeable for tests.
2. **Two nested loops** (env list → env menu) plus a modal create screen
   from the list (`n`), with `Quit` unwinding from any depth; the list
   re-fetches on every render.
3. **Edit-in-memory, save-on-command.** Package edits mutate the local
   definition copy; the header shows a `*unsaved edits*` marker (dirty
   detection compares the package lists, not the raw strings); SAVE is
   the only path that PUTs, and it re-GETs afterwards to re-sync.
4. **Install actions run on the SAVED definition.** update/rebuild always
   send a fresh GET as the action body; if local edits are pending, the
   action warns ("this runs on the SAVED definition on the server") and
   can be cancelled — no silent side effects.
5. **update vs rebuild as two menu items** mapping to the same endpoint
   with `forceRebuildEnv=false/true`, mirroring the DSS UI's "Update
   dependencies" vs "Rebuild env" buttons.
6. **Destructive action guarded twice:** delete requires typing the env's
   exact name, and external envs (`canUpdateCodeEnv=false`, e.g.
   `EXTERNAL_CONDA_NAMED`) surface DSS's own refusal messages instead of
   pretending to succeed.
7. **In-menu error recovery:** screen-level fetch failures print inline
   and re-prompt; action failures print and return to the menu; only
   startup failures (bad URL/key, unreachable host) exit (code 1, with a
   special note when a 403 means the key is not admin-privileged).
8. **Editable connection defaults at the top of the file**, same
   URL/key resolution chains as `dss_webapp_console.py`.
9. **Stdlib only** — `urllib.request`, `getpass`; `.format()` over
   f-strings; ASCII-only output.

## Known limitations / follow-up candidates

- Only `specPackageList` is editable. `permissions`, `usableByAll`,
  `desc.installCorePackages`, conda settings etc. ride along unmodified
  in the GET→PUT round-trip (no editor for them).
- The package editor is a plain line editor; R syntax beyond
  `name==version` lines or conda spec blocks is not interpreted.
- On create, `pythonInterpreter` defaults to `PYTHON3` — the exact value
  depends on the DSS build (e.g. `PYTHON311`); check the DSS UI if
  unsure.
- Create is hardcoded to `deploymentMode=DESIGN_MANAGED` with core
  packages + Jupyter support installed; external/conda env creation is
  not offered.
- No project lens: code envs are instance-level in this API (there is no
  per-project env listing endpoint in the public API); project usage of
  an env is not shown.
- Plugin code envs (see research note) are not covered.

## Verification

Verified 2026-09-02 offline against `tests/mock_dss_server.py` (mock
code-env world: `basic_external` PYTHON EXTERNAL_CONDA_NAMED,
`canUpdateCodeEnv=false`; `py3_core` and `py_webapp` PYTHON
DESIGN_MANAGED, out of date; `r_stats` R, nothing installed; 2 s
simulated build delay on the packages action):

| Scenario | Result |
|---|---|
| Full cycle: py_webapp → status → packages (add `openpyxl==3.1.5`, delete `flask`) → header shows `*unsaved edits*` → save (204 + server list) → update (messages, refetch) → status reflects it | exit 0 |
| Rebuild: py3_core → rebuild (`forceRebuildEnv=true`) → "Rebuilt env …" → status shows mandatory+requested installed (4 packages) | exit 0 |
| Unsaved-changes guard: edit packages → update → warned, `n` → "skipped"; save → update → no warning, clean install | exit 0 |
| Create: `n` → `py_new_test` → python / `PYTHON311` → confirm → appears in the list (5 envs) | exit 0 |
| Delete: type the exact env name → "deleted 'py_new_test'", list back to 4 envs | exit 0 |
| Delete name mismatch (typed `basic_exterr`) | "name did not match - nothing deleted", env intact |
| Update on an external env (`canUpdateCodeEnv=false`) | DSS's error message surfaced: "(DSS reported ERRORS …)" |
| Jupyter activate on r_stats (lang `r` path calls) | success message, definition refetched |
| Create with an invalid env name (`bad name!`) | rejected before any API call, "nothing created" |
| Create aborted with `b` at the language menu | clean "aborted", nothing created |
| Bad API key at startup | `error: HTTP 401 …`, exit 1 |
| EOF at the first prompt | clean "bye.", exit 0 |
| Regression: `dss_restart_webapp.py` (restart + unknown project) | unchanged (exit 0 / exit 3) |
| Regression: `dss_webapp_console.py` full flow | unchanged, exit 0 |

## History

### v1 — 2026-09-02

- Researched the complete code-env surface of the public API: 7 admin
  endpoints, all requiring Admin privileges; confirmed there is **no
  dedicated rebuild endpoint and no async future** — rebuild is
  `POST /admin/code-envs/packages?forceRebuildEnv=true`, synchronous.
- Implemented the two-level console (env list → env menu) with a package
  line-editor over `specPackageList`, dirty-state tracking, GET-round-trip
  save (PUT → 204), update/rebuild on the saved server definition with an
  unsaved-edits guard, jupyter toggle, typed-confirmation delete, and a
  guided create flow; 300 s default per-call timeout because install
  actions are synchronous.
- Extended `tests/mock_dss_server.py` with the code-env world (4 envs
  incl. an external one that refuses updates, 2 s simulated build
  window, `isUptodate` computed from spec vs actual); verified the
  scenarios above plus regressions for scripts #1 and #2.