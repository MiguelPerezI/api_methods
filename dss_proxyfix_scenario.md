# dss_proxyfix_scenario.py — design & history

Create a `custom_python` scenario in a Dataiku DSS project whose payload
removes a line from the `~/.bashrc` of the user the scenario runs as, then
restarts DSS — and watch the whole thing: the script waits for the restart
outage and confirms DSS comes back.

The target is either proxy-shaped (the defaults this tool was built for) or
any line containing a given phrase:

- `--proxy https_proxy=naappproxyhttpnopers.wlb.nam.nsroot.net:9999` (default):
  lines matching `(export) <var> = …<host>` — host-anchored, any quoting.
- `--phrase "ulimit -n 4096"`: every line containing that exact text
  (matched literally, whole line removed) — exports, aliases, comments,
  anything.

Built on top of `dss_bashrc_scenario.py`'s API research (same scenario
endpoints; that tool diagnosed the line's presence — this one fixes it).

## Usage

```bash
# interactive: connect -> pick a project -> create + confirm + run + watch:
DKU_API_KEY=... python3 dss_proxyfix_scenario.py

# non-interactive project choice (still asks before running):
DKU_API_KEY=... python3 dss_proxyfix_scenario.py --project MYPROJECT

# fully non-interactive (--yes skips the confirmation):
DKU_API_KEY=... python3 dss_proxyfix_scenario.py --project MYPROJECT --yes

# create the scenario but do not run it (run later from the DSS UI):
DKU_API_KEY=... python3 dss_proxyfix_scenario.py --project MYPROJECT --no-run

# different target line / different dss install path:
DKU_API_KEY=... python3 dss_proxyfix_scenario.py --project MYPROJECT \
    --proxy http_proxy=someproxy.example.com:8080 \
    --dss-bin /opt/dataiku/dss_data/bin/dss

# an arbitrary phrase: remove every .bashrc line containing it
# (matched literally; --strip-env VAR adds the restart-env strip):
DKU_API_KEY=... python3 dss_proxyfix_scenario.py --project MYPROJECT \
    --phrase "ulimit -n 4096"
DKU_API_KEY=... python3 dss_proxyfix_scenario.py --project MYPROJECT \
    --phrase "somewhere.example.com" --strip-env HTTP_PROXY
```

`--url` also falls back to env `DSS_URL`; the key comes from env
`DKU_API_KEY` or `--api-key-file FILE` — never a `--api-key` flag. Defaults
match the task this was built for:
`--proxy https_proxy=naappproxyhttpnopers.wlb.nam.nsroot.net:9999`,
`--dss-bin /dataiku/dss_data/bin/dss`. `--proxy` and `--phrase` are
mutually exclusive; the scenario name defaults to
`Remove https_proxy line and restart DSS` (proxy targets) or
`Remove bashrc phrase and restart DSS` (phrase targets), reusing a
same-name scenario instead of duplicating.

## What the payload does (in order, all logged in the run log)

1. Prints host / scenario user / bashrc path / target (same diagnostics as
   the bashrc tool, so the log is self-describing).
2. Reads `~/.bashrc` and finds the matching lines: for a proxy target,
   `^\s*(export\s+)?<var>\s*=.*<host>`; for a phrase target, a literal
   containment match (`re.escape`d) — same whole-line removal semantics.
3. **Not found → stop**: prints `PROXYFIX-RESULT: no-line-found`, does NOT
   restart DSS, run ends SUCCESS. Nothing is written at all.
4. Found → writes a timestamped backup `~/.bashrc.bak-proxyfix-<ts>`
   (`shutil.copy2`), rewrites the file with the matching lines removed
   (temp file + `shutil.copymode` + `os.replace` = atomic, permissions
   preserved), re-reads to verify no residue; if residue remains it
   restores nothing but aborts the restart and exits FAILED.
5. Strips the target variable (any case spelling) from the environment it
   passes to the restart — implied by `--proxy`, opt-in `--strip-env VAR`
   for `--phrase` (see design decision 3).
6. Launches `<dss-bin> restart` **detached** (`start_new_session=True`,
   stdout/stderr → `~/dss-restart-scenario.log`), prints
   `PROXYFIX-RESULT: line-removed, restart launched`, flushes, returns.
   The backend running the scenario dies seconds later — expected.

The script greps the run log for the `PROXYFIX-RESULT:` markers to tell the
three outcomes apart (see below).

## API research

Same endpoint set as [dss_bashrc_scenario.md](dss_bashrc_scenario.md)
(projects list, scenario create/list, payload GET/PUT, run, get-last-runs,
run log — all validated there against the DSS 15 REST docs plus the
official `dataiku-api-client-python` source). No new endpoints; the only
additions are client-side: outage-aware polling and a second short-timeout
HTTP client for it.

## Design decisions

1. **A separate script, not a mode of the bashrc tool.** The task is
   destructive (edits a file, restarts a server) and needs a different
   post-run flow (wait out an outage instead of waiting for a run) — one
   script = one job keeps the diagnostic tool safe and simple.
2. **The restart must be launched detached.** The scenario runs inside the
   very backend that `dss restart` kills; waiting on the child would kill
   the run mid-line and could truncate the log. `setsid` + output redirect
   means the restart survives its parent's death and the payload can print
   and flush a clean, complete report first.
3. **Strip the target variable from the restart's environment — the subtle
   one.** The scenario process inherited `https_proxy` from the *current*
   backend, which was started back when the bashrc still had the line.
   `bin/dss` does not re-source `~/.bashrc`, so a naive `subprocess` restart
   would hand the proxy to the *new* backend and the whole exercise would
   change nothing. The payload pops the variable (any case spelling) from
   the env it passes to `Popen`, so the restarted DSS comes up proxy-free —
   as if it had been started from a clean shell after the bashrc edit.
   Verified in testing: the restarted process saw no proxy vars. With
   `--phrase` there is no obvious variable to strip, so nothing is stripped
   unless `--strip-env VAR` is given (e.g. `--phrase "somewhere.example.com"
   --strip-env HTTP_PROXY`).
4. **Backup before edit, verify after edit.** `copy2` keeps a timestamped
   backup next to the file; the rewrite is atomic (temp file in the same
   directory, mode copied, `os.replace`) so a crash mid-write cannot leave
   a half-written bashrc; a re-read confirms the line is really gone
   before the restart is allowed to fire. Residue or unreadable bashrc →
   FAILED, no restart.
5. **Not-found is a clean exit, not a failure.** If the line is already
   gone the payload writes nothing and does not restart; the script reads
   the `no-line-found` marker from the log and exits 0 with the log shown.
   (On the target server the line's presence was already confirmed with
   `dss_bashrc_scenario.py`, so the mainline path is the expected one.)
6. **Outage-aware watching.** After firing, the script polls with a
   5 s-timeout client (a hung socket during a restart must read as "down"
   quickly): first for the run/**outage** (`--down-window`, default 90 s) —
   any API failure means DSS is restarting, which is the success signal —
   then for DSS to **come back** (`--wait-timeout`, default 300 s) via
   `GET /projects/`. If the run finishes while DSS is still up, the log
   markers decide: `no-line-found` → exit 0; `line-removed` → keep
   waiting for the outage (the backend dies seconds after the payload
   returns); error/FAILED → exit 1 with the log. No outage within the
   window → the restart did not happen → exit 1 pointing at
   `~/dss-restart-scenario.log`.
7. **Confirmation defaults to NO.** The single gate before the run spells
   out what will happen (backup + edit + restart interrupts *all* running
   activities on the instance) and defaults to N; `--yes` opts out for
   automation. `--no-run` never asks (create/set only).
8. **Exit codes.** `0` ok (including not-found and user-interrupted waits —
   the run stands on DSS) · `1` API failure, cancelled trigger fire, failed
   edit/launch, restart never observed, or DSS never came back · `3`
   `--project` not found/visible · `2` usage error.
9. **Key via env `DKU_API_KEY` or `--api-key-file` only**, stdlib only,
   `.format()` over f-strings — same conventions as the rest of the repo.
   Machine-specific values (`--dss-bin`) are flags, not hardcoded logic;
   the payload they generate does carry the path, which is the point.

## Incident payload: `payload_strip_all_proxy.py` — 2026-09-30

A self-contained payload for `--payload-file`, built for the incident this
tool's v1 was aimed at, escalated: SSO broke (`ERR_OPENID_IDP_EXCHANGE_CODE_
FAILURE`) after `http_proxy` was added next to the `https_proxy` line and
DSS was relaunched. The built-in `--phrase` mode removes every matching
line fine, but `--strip-env` strips **one** variable from the relaunch
environment, and the running backend now carries several
(`http_proxy` + `https_proxy` + uppercase spellings) — one strip is not
enough. The payload (same markers, so this driver watches it unchanged):

```bash
DKU_API_KEY=... python3 dss_proxyfix_scenario.py \
    --url http://dss-host:11000 --project MYPROJECT \
    --scenario-name "Remove all proxy lines and restart DSS" \
    --payload-file payload_strip_all_proxy.py
```

- removes every `.bashrc` line exporting a variable whose **name contains
  `proxy`** (any case) — except `no_proxy`/`NO_PROXY`, which only exempt
  hosts and are worth keeping — plus any line setting a JVM `-D*proxy*`
  option (e.g. a `DKU_BACKEND_JAVA_OPTS` line with `-Dhttp.proxyHost=`);
- strips **every** proxy-named env var (except `no_proxy*`) from the
  relaunch environment (verified inside the relaunched process: no proxy
  variables at all);
- backup + atomic rewrite + re-read verification + detached relaunch +
  `PROXYFIX-RESULT` markers exactly as the built-in payload;
- `DSS_BIN` comes from the env (used by the sandbox tests) or defaults to
  `/dataiku/dss_data/bin/dss` — one line to edit for other installs;
- scope: only `~/.bashrc` of the scenario user. If
  `dss_proxydiag_scenario.py` shows proxy lines in other shell files or in
  DSS configuration, handle those separately.

Verified: sandbox run against a planted incident `.bashrc` (4 offending
lines removed, `no_proxy`/`PATH`/`ulimit` kept, backup written, relaunched
process saw no proxy vars), no-line-found path (marker, no relaunch,
exit 0), and the full driver flow against the mock (outage detected,
back online, run killed-by-restart, exit 0).

## Known limitations / follow-up candidates

- The scenario runs as the DSS **backend user** (or the scenario's run-as);
  the restart therefore needs that user to own the DSS data dir. If the
  `dss` command is not executable by them, the run fails with the traceback
  in the log and DSS stays up (exit 1).
- `dss restart` is not instantaneous: running jobs/webapps are killed. The
  tool warns in the confirmation but does not check for active jobs first
  (a `GET /projects/{pk}/jobs/` pre-check would be a trivial follow-up).
- After a restart the run record may surface later, never, or as
  failed/aborted — the script reports whatever it finds and treats
  "saw outage + DSS back" as the authoritative success signal.
- The `restart_log` is overwritten on each run (fixed name) so the script
  can always point at one path.
- No cleanup/delete of the scenario (`DELETE /projects/{pk}/scenarios/{id}`
  would be the trivial follow-up, same as the bashrc tool).

## Verification

Verified 2026-09-30 offline. The payload's own logic was executed for real
against a sandbox `HOME` (fake `dss` binary — nothing on this machine was
modified or restarted); the driver's outage flow was verified against
`tests/mock_dss_server.py`, whose self-restart simulation (run killed
mid-flight + HTTP 503 outage window via `--restart-delay`) was added for
this script.

| Scenario | Result |
|---|---|
| Payload, sandbox bashrc with the target line | line found + removed, timestamped backup written, `https_proxy` stripped from the restart env, detached restart launched, marker printed, exit 0 |
| Restarted (fake) process environment | no proxy vars — the strip in decision 3 works |
| Payload, sandbox bashrc without the line | `no-line-found` marker, nothing written, no restart, exit 0 |
| Payload, missing `~/.bashrc` | `error` marker, exit 1, no restart |
| Driver vs mock: real payload, `--yes` (mainline) | scenario created, payload round-trip verified, outage detected ("DSS is down (as expected)"), DSS back online with version + project count, run reported killed-by-restart, truncated log shown, exit 0 |
| Driver vs mock: `--payload-file` printing `no-line-found` | run SUCCESS, marker read from log, "the target line is NOT in the .bashrc", log shown, exit 0 |
| Driver vs mock: `--payload-file` claiming `line-removed` but no restart (DSS stays up) | waited `--down-window`, "DSS never went down … the restart did not happen", exit 1 |
| Driver vs mock: `--payload-file` that raises | run FAILED, traceback in the printed log, exit 1 |
| Driver vs mock: confirm prompt with empty answer | "scenario ready - NOT run", exit 0 |
| Driver vs mock: `--no-run` | scenario + payload set, run-later hint, exit 0 |
| `--project NOSUCH` | exit 3, lists the visible projects |
| Wrong API key (401) | exit 1 |
| `--proxy` and `--phrase` together | usage error (exit 2) |
| Payload `--phrase`, sandbox line `alias ll='ls -l'` present | exactly that line removed (proxy + other lines untouched), `restart env: unchanged`, detached restart launched, exit 0 |
| Payload `--phrase` + `--strip-env https_proxy`, target `ulimit -n 4096`, proxy var in env | ulimit line removed, `restart env: stripped https_proxy=…` — restarted process saw no proxy vars |
| Payload `--phrase` for a line that isn't there | `no-line-found` marker, nothing written, no restart, exit 0 |
| Driver vs mock: `--phrase` mainline (real payload, `--yes`) | target shown as `any line containing "alias ll='ls -l'"`, outage + back-online + killed-run flow as with the proxy target, exit 0 |

Live use of the underlying scenario flow (create → payload → run → poll →
log) was confirmed on a real DSS instance with `dss_bashrc_scenario.py`
(same endpoint set and driver pattern), which found the proxy line on the
target server; this tool reuses exactly that machinery.

## History

### v1 — 2026-09-30

- Built after `dss_bashrc_scenario.py` (same-day) confirmed the target
  line exists in the DSS backend user's `~/.bashrc` on the external
  server; this tool performs the remediation: backup + remove the line +
  restart DSS from inside the scenario.
- Payload: host-anchored regex match (any quoting), atomic rewrite with
  preserved permissions, re-read verification, env-var strip before the
  detached restart, `PROXYFIX-RESULT` markers; run-as-user diagnostics
  identical to the bashrc tool.
- Driver: scenario reuse by name, payload round-trip verify, default-NO
  confirmation (+`--yes`), outage-aware watching with a short-timeout
  probe client (down-window / back-online), marker-driven outcome
  handling, exit codes 0/1/2/3.
- Extended `tests/mock_dss_server.py` with the self-restart simulation
  (killed run + 503 outage window) and verified the scenarios above.

#### Fixes found by the verification run itself

- The mock's restart detection originally matched any payload whose text
  contained a `/bin/dss` path + "restart" — which the *comments* of two
  test payloads happened to contain, so those runs were mis-classified
  as restarts (executed never, outage simulated) and the driver's
  failure paths tested nothing. Detection now requires the payload to
  actually Popen-launch a restart (as the real payload does); the
  mis-classification was caught because the driver's exit codes (0 where
  1 was expected) exposed it — the exit-code contract earning its keep.
- Cosmetic: `~/dss-restart-scenario.log/` had a stray trailing slash in
  three hint strings (a `~/{}/` format pattern); fixed to `~/{} …`.

### v1.1 — 2026-09-30

- `--phrase TEXT`: remove every `.bashrc` line containing an arbitrary
  (literally matched) phrase — not only proxy-shaped lines. The payload is
  now mode-agnostic: the driver resolves the target into a descriptor, a
  pre-computed regex source and the env-strip variable, all injected as
  literals; `--strip-env VAR` replaces the implied strip of the proxy var.
  `--proxy` and `--phrase` are mutually exclusive (usage error when both
  given); phrase-mode scenarios default to the name
  `Remove bashrc phrase and restart DSS`.
- Verified: payloads through the real `parse_args`/`build_target` path,
  proxy-mode regression (unchanged behavior), phrase edit/no-strip,
  `--strip-env`, not-found, mutual-exclusion guard, and the phrase-mode
  driver flow against the mock (rows above).