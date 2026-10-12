# dss_proxydiag_scenario.py — design & history

Create a `custom_python` scenario in a Dataiku DSS project whose payload
runs **READ-ONLY** SSO/proxy incident diagnostics on the DSS host, run it
once, and show the report.

Built for the incident where proxy lines (`http_proxy` / `https_proxy`)
added to the DSS backend user's `~/.bashrc` broke SSO login with
`ERR_OPENID_IDP_EXCHANGE_CODE_FAILURE` (`UnknownHostException` on the proxy
host — the backend's OIDC code-exchange call was being routed through a
proxy the DSS server cannot even resolve). When the DSS server has no
reachable shell, the scenario payload is the only way to see what the host
actually sees.

Same endpoint set and driver flow as
[dss_bashrc_scenario.md](dss_bashrc_scenario.md) (connect → pick project →
create/reuse scenario → payload round-trip → run → poll → print log →
offer cleanup); the new code is the payload, the cleanup offer and four
flags.

## Usage

```bash
# interactive: connect -> pick a project -> create + confirm + run + report:
DKU_API_KEY=... python3 dss_proxydiag_scenario.py

# non-interactive project choice:
DKU_API_KEY=... python3 dss_proxydiag_scenario.py --project MYPROJECT

# different proxy target for the DNS/TCP probes + an extra search phrase:
DKU_API_KEY=... python3 dss_proxydiag_scenario.py --project MYPROJECT \
    --proxy-host someproxy.example.com --proxy-port 8080 --grep "naappproxy"

# clean up after the report is shown (skip the delete question):
DKU_API_KEY=... python3 dss_proxydiag_scenario.py --project MYPROJECT --delete-scenario
```

Defaults match the incident: `--proxy-host
naappproxyhttpnopers.wlb.nam.nsroot.net`, `--proxy-port 9999`, scenario
name `SSO proxy incident diagnostics`. `--url` falls back to env `DSS_URL`;
the key comes from env `DKU_API_KEY` or `--api-key-file` — never an
`--api-key` flag. `--payload-file FILE` runs any other payload through the
same machinery. `--wait-timeout` defaults to 240 s (the payload's DNS/TCP
probes can take a while on a real network).

**After the run log is printed**, the script offers to delete the scenario
again — prompt default **NO**; `--delete-scenario` skips the question.
Cleanup uses `DELETE /projects/{projectKey}/scenarios/{scenarioId}`, the
same project write access that created the scenario. Deleting it also
removes its run log from DSS, so the printed report becomes the only copy
(and the offer says so). It is offered **only** when the report was
actually shown: `--no-run`, a declined run, a cancelled fire, an
interrupted wait or a timed-out wait all return early with no offer.

SSO being down does **not** block this tool: the public API authenticates
the API key itself and never involves the identity provider — only browser
logins go through OIDC.

## What the payload reports (10 sections, all read-only)

1. **Host identity** — host, scenario user/uid, home, working dir.
2. **Proxy variables in this process** — the scenario process is a child of
   the DSS backend, so its inherited environment **is** the backend's
   environment: a proxy variable here is set in the backend that makes SSO
   calls. (This is the `/proc/$(…backend)/environ | grep -i proxy` check
   from the incident diagnosis, without needing a shell on the host.)
   Also lists the JAVA-options variables (`DKU_BACKEND_JAVA_OPTS`,
   `JAVA_OPTS`, `JDK_JAVA_OPTIONS`, …).
3. **Backend java processes** — every `/proc/<pid>` whose executable is
   `java` and whose command line mentions dataiku/dss/dku: start time,
   command line, JVM `-D*proxy*` options, proxy/JAVA variables in its
   `/proc/<pid>/environ` (graceful "(not readable - another user?)" when
   the scenario user cannot read it). `DSS_DATADIR` is picked up here as a
   data-dir hint.
4. **Proxy lines in the shell startup files** — `~/.bashrc`,
   `~/.bash_profile`, `~/.bash_login`, `~/.profile`, `/etc/profile`,
   `/etc/bash.bashrc`, `/etc/environment`, `/etc/profile.d/*`; plus the
   mtime of `~/.bashrc` (with the backend start time from section 3 this
   answers "did DSS come up after the line was added?").
5. **Proxy lines in the DSS configuration** — under the discovered
   datadir: `bin/dss`, `bin/*.sh` (incl. `env-site.sh`), `install.ini`,
   `config/*.json|properties|ini|cfg`. Discovery order: `DSS_DATADIR` in
   the scenario env → in a backend's environ → `datadir=...` on a backend
   command line → **`-Djava.io.tmpdir=<datadir>/tmp` on a backend command
   line, accepted only if `<datadir>/config` or `<datadir>/bin/dss`
   actually exists** (every real backend carries that flag; the existence
   gate also keeps test runs from adopting a co-resident real instance
   whose config isn't readable). Lines also match the proxy **host** as a
   literal (`[proxy-host]` — a JSON `"host": "..."` value line need not
   contain the word "proxy"), and the proxy URL is excluded from the
   SSO-endpoint harvest. Secret-named files are skipped entirely.
6. **SSO endpoints discovered** — `openid / oidc / issuer / jwks /
   userinfo` lines carry the URLs out of section 5.
7. **DNS resolution** — the proxy host and the SSO hosts, plus
   `/etc/resolv.conf`; exactly what the backend's resolver sees.
8. **TCP connectivity** — proxy host:port and SSO endpoint ports, 5 s
   timeout, failures classified (DNS-FAIL / REFUSED / UNREACHABLE).
9. **Direct HTTPS to the SSO endpoint** — a GET with `ProxyHandler({})`,
   i.e. **with proxy variables deliberately ignored**: any HTTP answer
   proves SSO works again the moment the backend stops proxying. This is
   the money test for the incident.
10. **Putting it together** — backend-start-vs-bashrc-edit verdict, whether
    the backend env carries proxy variables, whether the proxy host even
    resolves; ends with the `DIAG-DONE` marker.

`--grep TEXT` additionally marks every line containing an exact phrase in
the section-4 and section-5 searches (`[phrase]` prefix) — for hunting any
other string someone may have left behind.

## Design decisions

1. **Strictly read-only, and self-describing.** The payload writes
   nothing, relaunches nothing, and prints its own plan; the driver prints
   the plan again before the run prompt. Diagnosis must be safe to run
   during an outage, and the fix belongs to
   [dss_proxyfix_scenario.py](dss_proxyfix_scenario.py) (or
   `payload_strip_all_proxy.py` for this incident — see that tool's .md).
2. **The scenario env as a backend probe.** The child inherits the
   backend's environment, so `os.environ` of the payload answers the
   "does the running backend carry the proxy var?" question directly —
   no `/proc` gymnastics needed for the common case (section 3 still does
   them for the authoritative per-process view, including JVM options on
   the command line).
3. **Every section guarded.** A diagnostics report that dies in section 2
   is worthless; each section is wrapped so a crash prints an in-band note
   and the rest continues. The payload always ends SUCCESS with
   `DIAG-DONE` (a failure means the traceback is the finding).
4. **Secrets never printed.** Config lines pass through `mask()`: URL
   userinfo (`//user:pass@` → `//***:***@`), values of keys containing
   secret/passwd/password/apikey → `***`, bearer tokens → `***`. Files with
   `secret` in the name are skipped. SSO config typically holds a client
   secret — the `[sso]` grep matches those lines, so masking is load-bearing
   (verified: `"oidcClientSecret": "***"` in the output).
5. **Raw sockets, not libraries that honor proxy variables.** DNS
   (`getaddrinfo`) and TCP (`create_connection`) ignore the environment;
   the one HTTP call (section 9) uses `ProxyHandler({})` **on purpose** —
   the whole point is to see the direct path. A naive `urlopen` would
   silently route through the very proxy under investigation.
6. **Mock-simulator awareness.** `tests/mock_dss_server.py` simulates a
   DSS self-restart for any payload containing both `Popen` and `restart`
   in its text; the diagnostics payload launches no subprocess and avoids
   the word entirely, so mock runs execute it for real instead of
   triggering the outage simulation. (Verified by substring check after
   rendering.)
7. **Match the executable, not the text.** Backend detection requires
   `java` in argv[0]'s basename, not merely anywhere in the command line —
   a shell wrapper whose text mentions "java … dss" (e.g. the test harness
   itself) must not be reported as a backend. Found the hard way during
   verification.
8. **Conventions as the rest of the repo**: stdlib only, `.format()`, key
   via env `DKU_API_KEY` / `--api-key-file`, exit codes 0/1/3/2, no
   machine-specific paths in logic (the proxy host/port are flags with
   incident defaults).
9. **Cleanup offer after the report.** Once the run log is printed the
   script asks whether to delete the scenario again (`DELETE
   /projects/{pk}/scenarios/{id}` — the same call as the official client's
   `DSSScenario.delete()`, the same project write access that created it) —
   default **NO**, `--delete-scenario` opts in without asking. Deleting
   also removes the run log from DSS, so the offer is gated on the report
   being safely in the terminal: it appears only when the run finished and
   the log was shown (`run_and_show` returns `(exit_code, finished)` and
   `main` returns early on anything else). A failed DELETE warns, points
   at the DSS UI and exits 1; on success the exit code stays that of the
   run itself.
10. **A clean environment is not proof of a clean configuration.** The
    first live run (2026-10-01, incident server) showed the backend env
    carrying only *empty* `http_proxy`/`https_proxy` after the bashrc
    cleanup + relaunch — yet SSO still failed, which means the proxy is
    configured **inside DSS itself**, exactly where that run could not
    look (real backends keep no `DSS_DATADIR` in env or cmdline, so
    section 5 was skipped). Consequences in the payload: (a) the
    `-Djava.io.tmpdir` datadir fallback above; (b) verdicts distinguish
    **valued** vs **present-but-empty** outbound variables, scoped to the
    classic names (`http_proxy`/`https_proxy`/`ftp_proxy`/`all_proxy`) —
    DSS-internal `DKU_HPROXY_*` vars contain "proxy" but are DSS's own
    frontend component and must not trip them — and the empty case
    explicitly sends the reader to section 5; (c) an unindented print
    that appended a stray "fails, the proxy configuration lives
    elsewhere" fragment to every section-2 verdict was re-indented into
    its `else` branch. Parallel fast path for the same question:
    `GET /admin/general-settings` (admin key) shows the global proxy
    section directly, no scenario needed.

## Verification

Verified 2026-09-30 offline. The payload was executed for real against a
sandbox `HOME` (planted incident `.bashrc` + fake DSS datadir with SSO JSON
config + a fake `java` backend process carrying proxy variables — nothing
on this machine was modified, since the payload is read-only by
construction), and the driver flow was verified against
`tests/mock_dss_server.py`.

| Scenario | Result |
|---|---|
| Payload, sandbox bashrc with `http_proxy`/`https_proxy`/`no_proxy` lines | all three reported with file:line, `~/.bashrc` mtime shown |
| Payload, fake datadir `config/general-settings.json` with `issuer`/`tokenUrl`/`clientSecret` | `[sso]` lines printed, both endpoint URLs discovered, secret line **not** printed (no keyword match) |
| Payload, secret on a matching line (`"oidcClientSecret": …`) | printed as `"oidcClientSecret": "***"` — no secret anywhere in the log |
| Payload, fake java process with proxy vars + `-Dhttp.proxyHost` + `DSS_DATADIR` | pid, start time, JVM proxy option, environ proxy vars all reported; `DSS_DATADIR` discovered from the *backend's* environ when absent from the payload env |
| Payload, real DSS backend running as another user | graceful "(not readable - another user?)" — command line still shown |
| Payload, unresolvable proxy + SSO hosts | DNS FAILED + TCP DNS-FAIL + direct-HTTPS failure, all classified; verdict section: "proxy host does NOT resolve … has to go" |
| Shell wrapper whose cmdline text mentions "java … dss" | **not** reported after the argv[0] fix (was reported before — found via a leaked test-harness string in the log) |
| Driver vs mock: real payload, default flags | scenario created, payload round-trip verified, run SUCCESS, all sections + `DIAG-DONE` in the printed log, exit 0 |
| Driver vs mock: `--grep ulimit` + same-name scenario | reuse prompt → payload overwritten, `[phrase] ulimit -n 4096` reported, exit 0 |
| Driver vs mock: run + `y` at the delete prompt | scenario deleted, scenario list empty afterwards, "the report above is the only copy now", exit 0 |
| Delete prompt with EOF / with `n` | default NO taken (EOF counts as no — the report is already printed), scenario kept with a re-run hint, exit 0 |
| Same-name reuse + `--delete-scenario` | payload overwritten, run, scenario deleted **without asking**, exit 0 |
| `--no-run` / declined run / unfinished run (`--run-delay 8`, `--wait-timeout 3`) even with `--delete-scenario` | no delete offer at all, scenario kept, exit 0 / 0 / 1 |
| DELETE fails (simulated 403) | warning + "delete it from the DSS UI: … Settings -> Delete" hint, exit 1, scenario kept |
| Mock `DELETE`: bogus scenario id / real id / run log of a deleted scenario | 404 DSS-style JSON / 200 `{}` / 404 — the run history really goes with the scenario |
| bashrc tool regression after the mock change (T9) | mainline unchanged, exit 0 |
| v1.2 payload, fake java backend whose only datadir marker is `-Djava.io.tmpdir=<sandbox>/tmp`, env scrubbed of `DSS_DATADIR` | datadir discovered from the backend command line (existence-validated), config searched: `env-site.sh` proxy line, general-settings `"proxy": {` block **and its `"host": "https://naappproxy…"` value line**, OIDC issuer all found; section 9 ran against the issuer; exit 0 |
| v1.2 same test on a box whose real datadir is permission-closed (`drwxr-x---`) | the real backend's `-Djava.io.tmpdir` is **refused by the existence gate** — never adopted; sandbox datadir wins |
| Empty `http_proxy`/`https_proxy` in env (v1.2) | section 2 "present but EMPTY - an empty value is inert", section 10 "the setting lives in the DSS configuration itself", and the stray "fails, the proxy configuration lives elsewhere" fragment is **gone** |
| Valued `http_proxy`/`https_proxy` in env (v1.2) | section 2 "DOES carry valued proxy variables", section 10 "sent through the proxy host", no stray fragment |
| `clientSecret` in `general-settings.json` + secret-named config file (v1.2) | neither printed (no keyword match on the value line; file skipped by name) |
| Driver vs mock after v1.2 (empty-proxy env, then valued-proxy env) | full create → payload round-trip → run → log → delete-prompt flow unchanged; `DSS data dir: … (via DSS_DATADIR in this process's environment)`; delete y → 200 + empty list, n → kept; exit 0 both |
| Built payload text (v1.2) | still contains neither "Popen" nor "restart" — the mock does not simulate an outage for it |
| `--project NOSUCH` | exit 3, lists visible projects |
| Wrong API key (401) | exit 1 |
| `--no-run` | scenario + payload set, run-later hint, exit 0 |
| `--proxy-port 99999` | usage error (exit 2) |

## History

### v1 — 2026-09-30

- Built the same day the incident landed: `http_proxy` was added to the
  DSS backend user's `~/.bashrc` (a `https_proxy` line had already been
  found there by `dss_bashrc_scenario.py`), SSO broke with
  `ERR_OPENID_IDP_EXCHANGE_CODE_FAILURE`, and the server is only reachable
  through the public API — scenario injection is the only diagnostic
  channel.
- Payload: the 10 read-only sections above; `--grep` for arbitrary phrase
  search; `--proxy-host/--proxy-port` for the network probes.
- Driver: the bashrc tool's flow verbatim plus a printed payload plan
  (read-only + outbound-probe disclosure) and a 240 s run wait.
- Fix for the incident prepared alongside: `payload_strip_all_proxy.py`
  (run via `dss_proxyfix_scenario.py --payload-file`), documented in
  [dss_proxyfix_scenario.md](dss_proxyfix_scenario.md).

### v1.1 — 2026-10-01

- Cleanup option (requested after the incident flow): once the run log is
  printed, the script offers to delete the scenario again — prompt default
  **NO**, `--delete-scenario` skips the question. `DELETE
  /projects/{pk}/scenarios/{id}` (the same call as the official client's
  `DSSScenario.delete()`, validated against the client source because the
  REST docs page truncates before the scenario privileges) removes the
  scenario **and its run log** from DSS, so the offer is gated on the
  report being shown: `run_and_show` now returns `(exit_code, finished)`
  and every path that never printed the log (`--no-run`, declined run,
  cancelled fire, interrupted or timed-out wait) returns early without
  offering. EOF at the prompt counts as NO (the report is already in the
  terminal; nothing may crash after it). Exit codes unchanged — a
  successful delete keeps the run's own exit code, a failed delete exits 1
  with a DSS-UI hint.
- Mock gained the DELETE scenario endpoint (200 `{}`, 404 DSS-style JSON
  for unknown project/scenario; a deleted scenario's run log 404s too).
- Verified: the new rows above, plus the bashrc-tool regression (the mock
  change is additive — one new route after the log route, one handler).

### v1.2 — 2026-10-01 (later the same day)

- Triggered by the incident-server run: the report showed the backend
  environment **clean** (only *empty* `http_proxy`/`https_proxy`, and the
  relaunch at 22:24 on 09-30 came seven minutes after the 22:17 bashrc
  cleanup that commented the naappproxy lines) **while SSO still failed**
  — evidence that the proxy is configured inside DSS itself, exactly where
  v1.1 could not look: real backends carry no `DSS_DATADIR` in env or
  cmdline, so section 5 was skipped ("config not searched") and sections
  6/9 had nothing to work with.
- Payload changes (driver untouched):
  - **Datadir discovery** gains a final fallback: accept
    `-Djava.io.tmpdir=<datadir>/tmp` from a backend command line, but only
    if `<datadir>/config` or `<datadir>/bin/dss` exists — the source is
    printed ("via -Djava.io.tmpdir on the command line of pid N").
  - **Valued vs empty proxy variables**: the verdicts count only
    `http_proxy`/`https_proxy`/`ftp_proxy`/`all_proxy` with a non-empty
    value (DSS-internal `DKU_HPROXY_*` no longer trip them); the
    present-but-empty case explains it is inert and points at section 5.
  - **Indentation fix**: a stray print appended "fails, the proxy
    configuration lives elsewhere - see sections 3-5)" to *every*
    section-2 verdict; re-indented into its `else` branch.
  - **Proxy-host literal matching** (`[proxy-host]`) in sections 4/5 and
    the proxy URL is excluded from the SSO-endpoint harvest so sections
    7-9 never test the proxy as if it were the IdP. (For this incident's
    host the word-match already caught it — "naapp**proxy**httpnopers"
    contains "proxy" — the literal matters for proxy hosts without the
    word in their name.)
- Verified offline: the new rows above (sandbox + fake java backend via
  tmpdir discovery, permission-closed co-resident datadir refused by the
  existence gate, empty/valued wording, secret hygiene, full driver
  regression through the mock twice, banned-substring check).
- Parallel finding: `GET /admin/general-settings` (admin key) reads the
  global proxy section directly — the fastest confirmation on the
  incident server, no scenario needed.