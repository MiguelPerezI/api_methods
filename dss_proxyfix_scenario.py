#!/usr/bin/env python3
"""
dss_proxyfix_scenario.py — create a scenario that removes a line from the
~/.bashrc of the DSS backend user and restarts DSS, then watch the restart
happen (outage) and confirm DSS comes back.

The target line is either proxy-shaped (default / --proxy) or any line
containing a given phrase (--phrase):
    default / --proxy https_proxy=host:port -> lines matching
        (export) <var> = ...<host>            (host-anchored, any quoting)
    --phrase "some text"                     -> every line containing the text

Flow: connect -> list all projects -> pick one -> create (or reuse) a
"custom_python" scenario -> set its python payload -> run it -> the payload
backs up ~/.bashrc, removes the matching line(s), verifies the rewrite and
launches `<dss-bin> restart` detached (it survives the backend that is being
killed) -> the script waits for DSS to go down, then for DSS to come back,
and reports the scenario run's fate.

Target API: DSS public API 15 — https://doc.dataiku.com/dss/api/15/rest/

Endpoints used (relative to {dss-url}/public/api):
    GET  /projects/                                                    READ_CONF
    GET  /projects/{projectKey}/scenarios/                             MONITOR_JOBS
    POST /projects/{projectKey}/scenarios/                             WRITE_CONF
    GET  /projects/{projectKey}/scenarios/{scenarioId}/payload         MONITOR_JOBS
    PUT  /projects/{projectKey}/scenarios/{scenarioId}/payload          WRITE_CONF
    POST /projects/{projectKey}/scenarios/{scenarioId}/run             RUN_JOBS
    GET  /projects/{projectKey}/scenarios/{scenarioId}/get-last-runs   RUN_JOBS
    GET  /projects/{projectKey}/scenarios/{scenarioId}/{runId}/log     READ_CONF

Two details that make the restart actually work from inside a scenario:

    - the payload launches `<dss-bin> restart` DETACHED (setsid) and returns
      immediately, so the run's log is flushed before the backend dies;
    - the payload strips the proxy variable from the environment it passes
      to the restart: the scenario process inherited the proxy from the
      current backend (started while the bashrc line existed), and the
      restart script does not re-source ~/.bashrc, so without stripping it
      the NEW backend would come up with the proxy again.

The payload writes "PROXYFIX-RESULT: ..." markers into the run log; the
script reads them to tell "no such line - nothing to do" (clean exit)
apart from "edit done + restart launched" and from failures. If the line is
not found, DSS is NOT restarted.

Connection, in order of precedence:
    1. --url http://host:port
    2. env DSS_URL
    3. the CONNECTION DEFAULTS block below (edit by hand for your host/port)
    4. if none of the above: the script asks for the URL at startup

API key, in order of precedence:
    1. env DKU_API_KEY
    2. --api-key-file FILE
    3. hidden prompt (last resort — never a --api-key flag, it would leak
       into `ps` output and shell history)

Examples:
    DKU_API_KEY=... python3 dss_proxyfix_scenario.py
    DKU_API_KEY=... python3 dss_proxyfix_scenario.py --project MYPROJECT
    DKU_API_KEY=... python3 dss_proxyfix_scenario.py --project MYPROJECT --yes
    # a different proxy line:
    DKU_API_KEY=... python3 dss_proxyfix_scenario.py \
        --project MYPROJECT --proxy http_proxy=otherproxy.example.com:8080
    # an arbitrary phrase: remove every .bashrc line containing it:
    DKU_API_KEY=... python3 dss_proxyfix_scenario.py \
        --project MYPROJECT --phrase "ulimit -n 4096"
"""

import argparse
import getpass
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# CONNECTION DEFAULTS — edit these by hand to point at your DSS host,
# or override with --url / env DSS_URL (both take precedence over these).
# ---------------------------------------------------------------------------
DSS_HOST = "dss-host"      # <- change me
DSS_PORT = 11000           # <- change me (plain-HTTP DSS default is 11000)
DSS_USE_TLS = False        # True -> https://host:port

PLACEHOLDER_HOST = "dss-host"  # while unchanged, the script asks for the URL
API_PREFIX = "/public/api"
POLL_INTERVAL = 2          # seconds between run/outage polls
BACK_POLL_INTERVAL = 3      # seconds between back-online probes
PROBE_TIMEOUT = 5          # per-call timeout for outage probes (short: a hung
                           # socket during a restart must read as "down")
START_SKEW_MS = 5000       # clock-skew allowance when matching "our" run

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NOTFOUND = 3

DEFAULT_SCENARIO_NAME = "Remove https_proxy line and restart DSS"
PHRASE_SCENARIO_NAME = "Remove bashrc phrase and restart DSS"
DEFAULT_PROXY = "https_proxy=naappproxyhttpnopers.wlb.nam.nsroot.net:9999"
DEFAULT_DSS_BIN = "/dataiku/dss_data/bin/dss"
RESTART_LOG_NAME = "dss-restart-scenario.log"  # on the DSS host, in the
                                               # backend user's home

# The scenario payload. TARGET_DESC / PATTERN_SRC / STRIP_ENV / DSS_BIN are
# prepended as literals (built from the flags); the body below uses them.
# Raw string so the regex escapes reach DSS verbatim.
# Pure stdlib, so it runs in the default DSS backend python. The
# PROXYFIX-RESULT markers are matched by the driving script's scan_marker -
# keep the literal texts in sync with it.
SNIPPET_BODY = r'''\
import getpass
import os
import platform
import re
import shutil
import subprocess
import sys
import time

bashrc = os.path.join(os.path.expanduser("~"), ".bashrc")
restart_log = os.path.join(os.path.expanduser("~"), "dss-restart-scenario.log")

print("DSS host         : " + platform.node())
try:
    user = getpass.getuser()
except Exception:
    user = os.environ.get("USER") or "(unknown)"
print("scenario runs as : " + user)
print("bashrc           : " + bashrc)
print("target           : " + TARGET_DESC)
print("restart command  : " + DSS_BIN + " restart")
sys.stdout.flush()

pattern = re.compile(PATTERN_SRC)
try:
    with open(bashrc, "r", encoding="utf-8", errors="replace") as handle:
        content = handle.read()
except OSError as err:
    print("PROXYFIX-RESULT: error - cannot read " + bashrc + ": " + str(err))
    sys.exit(1)
lines = content.split("\n")
hits = [i for i, line in enumerate(lines) if pattern.search(line)]
if not hits:
    print("no matching line - nothing to change")
    print("PROXYFIX-RESULT: no-line-found (nothing changed, DSS NOT restarted)")
    sys.exit(0)
for i in hits:
    print("removing line {:4}: {}".format(i + 1, lines[i].strip()))
backup = "{}.bak-proxyfix-{}".format(bashrc, time.strftime("%Y%m%d-%H%M%S"))
shutil.copy2(bashrc, backup)
print("backup written   : " + backup)
kept = [line for i, line in enumerate(lines) if i not in set(hits)]
tmp = bashrc + ".proxyfix-tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    handle.write("\n".join(kept))
shutil.copymode(bashrc, tmp)
os.replace(tmp, bashrc)
with open(bashrc, "r", encoding="utf-8", errors="replace") as handle:
    residue = [line for line in handle.read().split("\n") if pattern.search(line)]
if residue:
    print("PROXYFIX-RESULT: error - target line still present after rewrite")
    print("NOT restarting DSS; restore from " + backup + " if needed")
    sys.exit(1)
print(".bashrc updated  : {} line(s) removed ({} -> {} lines)".format(
    len(hits), len(lines), len(kept)))
sys.stdout.flush()

# The scenario process inherited its environment from the CURRENT backend
# (started while the bashrc line existed); bin/dss does not re-source
# ~/.bashrc, so the stripped var must be removed here or the NEW backend
# inherits it again. STRIP_ENV is the implied proxy var (--proxy) or the
# --strip-env value ('' = strip nothing).
env = dict(os.environ)
stripped = []
if STRIP_ENV:
    for name in list(env):
        if name.lower() == STRIP_ENV.lower():
            stripped.append(name + "=" + env.pop(name))
if stripped:
    print("restart env     : stripped " + ", ".join(stripped))
elif STRIP_ENV:
    print("restart env     : no " + STRIP_ENV + " inherited")
else:
    print("restart env     : unchanged (no var stripped)")
sys.stdout.flush()

print("restarting DSS  : " + DSS_BIN + " restart")
with open(restart_log, "w", encoding="utf-8") as out:
    subprocess.Popen([DSS_BIN, "restart"], stdout=out, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True, env=env)
print("restart launched (detached) - output -> " + restart_log)
print("NOTE: the backend running this scenario dies with the restart - expected.")
sys.stdout.flush()
print("PROXYFIX-RESULT: line-removed, restart launched")
sys.stdout.flush()
'''


class Quit(Exception):
    """Raised to leave the script cleanly (q, EOF, Ctrl+C)."""


class DssApiError(Exception):
    """A call to the DSS public API failed."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class DssClient(object):
    """Thin stdlib HTTP client for the scenario subset of the DSS public API."""

    def __init__(self, base_url, api_key, insecure=False, timeout=30):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        context = ssl.create_default_context()
        if insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context))

    def call(self, method, path, body=None):
        """One API call with an optional JSON body; returns (json, headers)."""
        headers = {"Authorization": "Bearer " + self.api_key}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data,
                                         method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace").strip()
                payload = json.loads(raw) if raw else {}
                return payload, response.headers
        except urllib.error.HTTPError as err:
            body_text = err.read().decode("utf-8", "replace")
            try:
                message = json.loads(body_text).get("errorMessage", body_text)
            except ValueError:
                message = body_text
            raise DssApiError("HTTP {} {} {}: {}".format(
                err.code, method, path, str(message)[:500]),
                status=err.code) from None
        except urllib.error.URLError as err:
            raise DssApiError("cannot reach DSS ({} {}): {}".format(
                method, path, err.reason)) from None

    def call_text(self, method, path):
        """One API call whose response is plain text; returns the text."""
        headers = {"Authorization": "Bearer " + self.api_key}
        request = urllib.request.Request(self.base_url + path, data=None,
                                         method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            body_text = err.read().decode("utf-8", "replace")
            try:
                message = json.loads(body_text).get("errorMessage", body_text)
            except ValueError:
                message = body_text
            raise DssApiError("HTTP {} {} {}: {}".format(
                err.code, method, path, str(message)[:500]),
                status=err.code) from None
        except urllib.error.URLError as err:
            raise DssApiError("cannot reach DSS ({} {}): {}".format(
                method, path, err.reason)) from None

    def list_projects(self):
        payload, _ = self.call("GET", "/projects/")
        return payload

    def list_scenarios(self, project_key):
        payload, _ = self.call("GET", "/projects/{}/scenarios/".format(
            q(project_key)))
        return payload

    def create_scenario(self, project_key, name, scenario_type="custom_python"):
        # Minimal body validated against Dataiku's own client
        # (dataikuapi.dss.project.DSSProject.create_scenario):
        # definition {'params': {}} + type + name; the response carries 'id'.
        payload, _ = self.call("POST", "/projects/{}/scenarios/".format(
            q(project_key)),
            body={"name": name, "type": scenario_type, "params": {}})
        return payload

    def get_payload(self, project_key, scenario_id):
        payload, _ = self.call("GET", "/projects/{}/scenarios/{}/payload".format(
            q(project_key), q(scenario_id)))
        return payload

    def set_payload(self, project_key, scenario_id, script):
        payload, _ = self.call("PUT", "/projects/{}/scenarios/{}/payload".format(
            q(project_key), q(scenario_id)), body={"script": script})
        return payload

    def run_scenario(self, project_key, scenario_id):
        # Body = trigger params; {} fires the manual trigger.
        payload, _ = self.call("POST", "/projects/{}/scenarios/{}/run".format(
            q(project_key), q(scenario_id)), body={})
        return payload

    def get_last_runs(self, project_key, scenario_id, limit=10):
        payload, _ = self.call(
            "GET", "/projects/{}/scenarios/{}/get-last-runs?limit={}".format(
                q(project_key), q(scenario_id), int(limit)))
        return payload

    def get_run_log(self, project_key, scenario_id, run_id):
        # NOTE: no "runs" segment in the real API: .../scenarios/{id}/{runId}/log
        return self.call_text("GET", "/projects/{}/scenarios/{}/{}/log".format(
            q(project_key), q(scenario_id), q(run_id)))


def q(segment):
    """URL-quote one path segment."""
    return urllib.parse.quote(str(segment), safe="")


def now_ms():
    return int(time.time() * 1000)


def build_target(args):
    """Resolve the target from --proxy / --phrase into the payload
    constants; returns (scenario_name, target_desc, pattern_src, strip_env).
    pattern_src is a plain regex-source string the payload re.compile()s —
    composing it here (against the flags) keeps the payload mode-agnostic."""
    if args.phrase is not None:
        # literal containment: re.escape makes the phrase match exactly as
        # typed; the whole line gets removed, as with the proxy target
        return (args.scenario_name or PHRASE_SCENARIO_NAME,
                "any line containing {!r}".format(args.phrase),
                re.escape(args.phrase),
                args.strip_env or "")
    return (args.scenario_name or DEFAULT_SCENARIO_NAME,
            "{} lines pointing at {}".format(args.proxy_var, args.proxy_host),
            r"^\s*(export\s+)?{}\s*=.*{}".format(args.proxy_var,
                                                 re.escape(args.proxy_host)),
            args.strip_env or args.proxy_var)


def build_snippet(target_desc, pattern_src, strip_env, dss_bin):
    """The scenario payload: target constants prepended to the snippet body."""
    header = ("TARGET_DESC = {!r}\n"
              "PATTERN_SRC = {!r}\n"
              "STRIP_ENV = {!r}\n"
              "DSS_BIN = {!r}\n").format(
        target_desc, pattern_src, strip_env, dss_bin)
    return header + SNIPPET_BODY


# ---------------------------------------------------------------------------
# terminal UI helpers
# ---------------------------------------------------------------------------

def screen(title):
    print("")
    print("-" * 64)
    print(title)
    print("-" * 64)


def ask(count, extras=(), default=None):
    """Numbered-choice prompt. Returns a 0-based index or an extras key.
    Empty input selects `default` (a 0-based index) when one is given.
    Raises Quit on q/quit, EOF. Ctrl+C is handled by main()."""
    parts = []
    if count:
        parts.append("[1-{}]".format(count))
    parts.extend(extras)
    parts.append("q")
    if default is not None:
        parts.append("default {}".format(default + 1))
    prompt = "choice " + "/".join(parts) + ": "
    while True:
        try:
            raw = input(prompt).strip().lower()
        except EOFError:
            print("")
            raise Quit()
        if raw in ("q", "quit", "exit"):
            raise Quit()
        if raw in extras:
            return raw
        if raw == "" and default is not None:
            return default
        try:
            number = int(raw)
        except ValueError:
            number = None
        if number is not None and 1 <= number <= count:
            return number - 1
        print("  ? invalid choice, try again")


def ask_run_now(scenario_name, target_desc, dss_bin):
    """The gate before the destructive run. Default NO."""
    print("")
    print("Running the scenario will:")
    print("  1. back up and edit ~/.bashrc of the user the scenario runs as")
    print("     (removing lines: {})".format(target_desc))
    print("  2. then execute '{} restart' - ALL running activities on the".format(dss_bin))
    print("     DSS instance are interrupted.")
    while True:
        try:
            raw = input("Proceed and run it now? [y/N]: ").strip().lower()
        except EOFError:
            print("")
            raise Quit()
        if raw in ("n", "no", ""):
            return False
        if raw in ("y", "yes"):
            return True
        print("  ? please answer y or n")


# ---------------------------------------------------------------------------
# flow steps
# ---------------------------------------------------------------------------

def pick_project(client):
    """Project picker; returns a project dict. Refresh with 'r', quit with 'q'."""
    while True:
        error = None
        try:
            projects = client.list_projects()
        except DssApiError as err:
            projects, error = [], str(err)
        projects = sorted(projects,
                          key=lambda p: str(p.get("projectKey", "")).lower())
        screen("PROJECTS ({} visible to this API key)".format(len(projects)))
        if error:
            print("  error: {}".format(error))
        elif not projects:
            print("  (no projects visible - check the API key's permissions)")
        for i, project in enumerate(projects, 1):
            print("  {:2}) {:24} {}".format(i, project.get("projectKey", "?"),
                                            project.get("name") or ""))
        choice = ask(len(projects), ("r",))
        if choice == "r":
            continue
        return projects[choice]


def ensure_scenario(client, project_key, name):
    """Create the scenario, or reuse an existing one with the same name.
    Returns (scenario_id, created); (None, None) when the user aborts."""
    scenarios = client.list_scenarios(project_key)
    existing = None
    for scenario in scenarios:
        if scenario.get("name") == name:
            existing = scenario
            break
    if existing is None:
        print("  no scenario named {!r} in this project - creating one...".format(
            name))
        response = client.create_scenario(project_key, name)
        scenario_id = response.get("id")
        if not scenario_id:
            raise DssApiError(
                "DSS created the scenario but returned no id: {}".format(response))
        print("  created: id {} (custom_python)".format(scenario_id))
        return scenario_id, True
    print("  a scenario named {!r} already exists (id {})".format(
        name, existing.get("id")))
    print("   1) reuse it - overwrite its python payload")
    print("   2) create another scenario with the same name")
    print("   3) abort, change nothing")
    choice = ask(3, default=0)
    if choice == 0:
        return existing.get("id"), False
    if choice == 1:
        response = client.create_scenario(project_key, name)
        scenario_id = response.get("id")
        if not scenario_id:
            raise DssApiError(
                "DSS created the scenario but returned no id: {}".format(response))
        print("  created: id {} (custom_python; DSS allows duplicate names)".format(
            scenario_id))
        return scenario_id, True
    return None, None


def set_and_verify_payload(client, project_key, scenario_id, snippet):
    """PUT the payload, then GET it back and compare (write verification)."""
    client.set_payload(project_key, scenario_id, snippet)
    echo = client.get_payload(project_key, scenario_id).get("script", "")
    print("  payload set: {} line(s) of python".format(snippet.count("\n") + 1))
    if echo != snippet:
        print("  WARNING: stored payload differs from what was sent")
    else:
        print("  payload round-trip verified")


def match_our_run(runs, fire, fire_ms):
    """Find the run this trigger fire launched (trigger-runId, start fallback)."""
    fire_run_id = fire.get("runId")
    candidates = [r for r in runs if r.get("start", 0) >= fire_ms - START_SKEW_MS]
    for run in candidates:
        if (run.get("trigger") or {}).get("runId") == fire_run_id:
            return run
    return candidates[0] if candidates else None


def scan_marker(log_text):
    """The PROXYFIX-RESULT marker the payload writes into its log.
    Literal texts must stay in sync with SNIPPET_BODY."""
    full_log = log_text or ""
    for marker in ("no-line-found", "line-removed", "error"):
        if "PROXYFIX-RESULT: " + marker in full_log:
            if marker == "line-removed":
                return "line-removed"  # full text: 'line-removed, restart launched'
            return marker
    return None


def show_log(client, project_key, scenario_id, run, scenario_name):
    """Fetch and print a finished run's log; tolerant of a missing log."""
    run_id = run.get("runId")
    if not run_id:
        print("  (run has no runId - log not fetched)")
        return
    try:
        log = client.get_run_log(project_key, scenario_id, run_id)
    except DssApiError as err:
        print("  (log unavailable: {})".format(err))
        return
    screen("SCENARIO RUN LOG - {} - run {}".format(scenario_name, run_id))
    print(log.rstrip("\n"))
    print("")


def run_and_watch(client, probe, project_key, scenario_id, scenario_name,
                  down_window, wait_timeout):
    """Fire the run, wait for the restart outage, wait for DSS back."""
    fire_ms = now_ms()
    fire = client.run_scenario(project_key, scenario_id)
    print("  trigger fire accepted (trigger run {}, trigger '{}')".format(
        fire.get("runId"), (fire.get("trigger") or {}).get("id")))
    if fire.get("cancelled"):
        print("  WARNING: the manual trigger fire was cancelled - the scenario")
        print("  did not run (it may already be running). Check the DSS UI:")
        print("  project {} -> Scenarios -> {} -> History".format(
            project_key, scenario_name))
        return EXIT_FAIL

    # Phase 1: wait for the run to edit the .bashrc and for DSS to go down.
    print("  waiting for the run + restart (Ctrl+C skips the wait)...")
    deadline = time.monotonic() + down_window
    saw_outage = False
    finished_run = None
    marker = None
    try:
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            try:
                runs = probe.get_last_runs(project_key, scenario_id)
            except DssApiError as err:
                saw_outage = True
                print("  DSS is down (as expected): {}".format(str(err)[:120]))
                break
            our = match_our_run(runs, fire, fire_ms)
            if our is None or "result" not in our:
                continue  # still running (or not visible yet)
            finished_run = our
            outcome = (our.get("result") or {}).get("outcome", "?")
            if marker is None:
                try:
                    log = client.get_run_log(project_key, scenario_id,
                                              our.get("runId") or "")
                    marker = scan_marker(log)
                except DssApiError:
                    marker = None
                if marker == "no-line-found":
                    print("  run finished: {} - the target line is NOT in the".format(outcome))
                    print("  .bashrc; nothing was changed and DSS was NOT restarted.")
                    show_log(client, project_key, scenario_id, our, scenario_name)
                    return EXIT_OK
                if marker == "error" or outcome in ("FAILED", "ABORTED"):
                    print("  run finished: {} - the edit or the restart launch".format(outcome))
                    print("  FAILED. DSS was NOT restarted; details in the log below.")
                    show_log(client, project_key, scenario_id, our, scenario_name)
                    return EXIT_FAIL
                if marker == "line-removed":
                    print("  .bashrc edited, restart launched - waiting for the")
                    print("  outage...")
            # restart already launched (or run done): keep waiting for outage
    except KeyboardInterrupt:
        print("\n  (wait interrupted - the run itself stands; check the DSS UI)")
        return EXIT_OK
    if not saw_outage:
        print("  WARNING: DSS never went down within {}s - the restart did".format(down_window))
        print("  not happen. Check ~/{} on the DSS host.".format(RESTART_LOG_NAME))
        if finished_run is not None:
            show_log(client, project_key, scenario_id, finished_run, scenario_name)
        return EXIT_FAIL

    # Phase 2: wait for DSS to come back.
    print("  waiting for DSS to come back (Ctrl+C skips the wait)...")
    deadline = time.monotonic() + wait_timeout
    back = False
    try:
        while time.monotonic() < deadline:
            time.sleep(BACK_POLL_INTERVAL)
            try:
                payload, headers = probe.call("GET", "/projects/")
                print("  DSS is back online - version {} - {} project(s) visible".format(
                    headers.get("DSS-Version") or "?", len(payload)))
                back = True
                break
            except DssApiError:
                continue
    except KeyboardInterrupt:
        print("\n  (wait interrupted - DSS may still be starting; probe with:")
        print("   curl -H 'Authorization: Bearer $DKU_API_KEY' <url>/public/api/projects/)")
        return EXIT_OK
    if not back:
        print("  WARNING: DSS did not come back within {}s. Probe it manually".format(wait_timeout))
        print("  (curl <url>/public/api/projects/) and check ~/{} on the host.".format(
            RESTART_LOG_NAME))
        return EXIT_FAIL

    # Phase 3: report the fate of the scenario run + best-effort log.
    our = None
    try:
        runs = client.get_last_runs(project_key, scenario_id)
        our = match_our_run(runs, fire, fire_ms)
    except DssApiError as err:
        print("  (could not read the run list back: {})".format(str(err)[:120]))
    if our is not None:
        if "result" in our:
            print("  run finished before the outage: outcome {}".format(
                (our.get("result") or {}).get("outcome", "?")))
        else:
            print("  scenario run: killed by the restart (no result) - expected")
        show_log(client, project_key, scenario_id, our, scenario_name)
    print("  the .bashrc backup (if the edit happened): ~/.bashrc.bak-proxyfix-*")
    print("  restart output on the DSS host: ~/" + RESTART_LOG_NAME)
    print("  verify the line is gone by re-running dss_bashrc_scenario.py")
    return EXIT_OK


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="dss_proxyfix_scenario.py",
        description="Create a DSS scenario that removes the proxy line from "
                    "the backend user's ~/.bashrc and restarts DSS, run it, "
                    "and watch DSS go down and come back.",
        epilog="Connection: --url, env DSS_URL, or the CONNECTION DEFAULTS at "
               "the top of this file; the script prompts for the URL if none "
               "is set. Auth: env DKU_API_KEY / --api-key-file / hidden prompt. "
               "API docs: https://doc.dataiku.com/dss/api/15/rest/")
    parser.add_argument("--url",
                        help="DSS base URL, e.g. http://dss-host:11000 "
                             "(default: env DSS_URL, then built-in defaults)")
    parser.add_argument("--api-key-file", metavar="FILE",
                        help="file holding the DSS API key (default: env "
                             "DKU_API_KEY, then a hidden prompt)")
    parser.add_argument("--project", metavar="KEY",
                        help="project key (skips the interactive picker; "
                             "exit 3 when the key is not visible)")
    parser.add_argument("--scenario-name", default=None, metavar="NAME",
                        help="name of the scenario to create/reuse (default: "
                             "{!r} for --proxy targets, {!r} for --phrase; a "
                             "same-name scenario is reused, not "
                             "duplicated)".format(DEFAULT_SCENARIO_NAME,
                                                  PHRASE_SCENARIO_NAME))
    parser.add_argument("--proxy", default=None, metavar="VAR=HOST:PORT",
                        help="proxy line to remove: env var and the proxy "
                             "address it points at (default when neither "
                             "--proxy nor --phrase is given: "
                             "{})".format(DEFAULT_PROXY))
    parser.add_argument("--phrase", default=None, metavar="TEXT",
                        help="instead of --proxy: remove every .bashrc line "
                             "containing this exact text (matched literally, "
                             "regex-escaped)")
    parser.add_argument("--strip-env", default=None, metavar="VAR",
                        help="strip VAR (any case) from the restart's "
                             "environment (implied by --proxy; with --phrase "
                             "the restart env is left untouched unless given)")
    parser.add_argument("--dss-bin", default=DEFAULT_DSS_BIN, metavar="PATH",
                        help="the dss administration command to restart with "
                             "(default: {})".format(DEFAULT_DSS_BIN))
    parser.add_argument("--payload-file", metavar="FILE",
                        help="run this file's python as the scenario payload "
                             "instead of the built-in proxy-fix snippet")
    parser.add_argument("--yes", action="store_true",
                        help="do not ask for confirmation before running")
    parser.add_argument("--no-run", action="store_true",
                        help="create/set the scenario but do not run it")
    parser.add_argument("--down-window", type=int, default=90, metavar="SEC",
                        help="seconds to wait for DSS to go down after the "
                             "restart is launched (default: 90)")
    parser.add_argument("--wait-timeout", type=int, default=300, metavar="SEC",
                        help="seconds to wait for DSS to come back (default: 300)")
    parser.add_argument("--timeout", type=int, default=30, metavar="SEC",
                        help="per-call HTTP timeout in seconds (default: 30; "
                             "outage probes always use a short timeout)")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS certificate verification (self-signed DSS)")
    args = parser.parse_args(argv)
    if args.proxy is None and args.phrase is None:
        args.proxy = DEFAULT_PROXY
    if args.phrase is not None and args.proxy is not None:
        parser.error("--proxy and --phrase are mutually exclusive")
    if args.proxy is not None:
        proxy_var, _, proxy_host = args.proxy.partition("=")
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", proxy_var) or not proxy_host.strip():
            parser.error("--proxy must look like VAR=host:port, got {!r}".format(args.proxy))
        args.proxy_var, args.proxy_host = proxy_var, proxy_host.strip()
    if args.strip_env is not None and not re.match(
            r"^[A-Za-z_][A-Za-z0-9_]*$", args.strip_env):
        parser.error("--strip-env must be a plain env var name, got {!r}".format(
            args.strip_env))
    return args


def read_api_key(api_key_file):
    if api_key_file:
        with open(api_key_file, "r", encoding="utf-8") as handle:
            key = handle.read().strip()
        if not key:
            raise DssApiError("API key file {!r} is empty".format(api_key_file))
        return key
    key = os.environ.get("DKU_API_KEY", "").strip()
    if key:
        return key
    key = getpass.getpass("DSS API key (input hidden): ").strip()
    if not key:
        raise DssApiError("no API key provided")
    return key


def read_payload(payload_file):
    """The scenario's python payload: --payload-file or the built-in snippet."""
    if not payload_file:
        return None  # built from flags later, after argparse validated them
    try:
        with open(payload_file, "r", encoding="utf-8") as handle:
            script = handle.read()
    except OSError as err:
        raise DssApiError("cannot read payload file {}: {}".format(
            payload_file, err)) from None
    if not script.strip():
        raise DssApiError("payload file {!r} is empty".format(payload_file))
    return script


def resolve_base_url(args):
    """--url > env DSS_URL > hand-edited defaults > interactive prompt."""
    if args.url:
        return args.url.rstrip("/")
    env_url = os.environ.get("DSS_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    if DSS_HOST and DSS_HOST != PLACEHOLDER_HOST:
        scheme = "https" if DSS_USE_TLS else "http"
        return "{}://{}:{}".format(scheme, DSS_HOST, DSS_PORT)
    print("No DSS URL configured. Options: --url, env DSS_URL, or edit the")
    print("CONNECTION DEFAULTS at the top of this file.")
    while True:
        try:
            raw = input("DSS URL [http://host:port]: ").strip()
        except EOFError:
            print("")
            raise Quit()
        if re.match(r"^https?://\S+$", raw):
            return raw.rstrip("/")


def main(argv=None):
    args = parse_args(argv)
    try:
        api_key = read_api_key(args.api_key_file)
        root_url = resolve_base_url(args)
        scenario_name, target_desc, pattern_src, strip_env = build_target(args)
        snippet = read_payload(args.payload_file) or build_snippet(
            target_desc, pattern_src, strip_env, args.dss_bin)
        client = DssClient(root_url + API_PREFIX, api_key,
                           insecure=args.insecure, timeout=args.timeout)
        # Short-timeout twin for outage/back-online polls: a hung socket
        # during a restart must read as "down" quickly.
        probe = DssClient(root_url + API_PREFIX, api_key,
                          insecure=args.insecure, timeout=PROBE_TIMEOUT)

        # Connection check + banner (also validates the API key).
        projects, headers = client.call("GET", "/projects/")
        dss_version = headers.get("DSS-Version") or "?"
        print("")
        print("connected to {} - DSS version {} - {} project(s) visible".format(
            root_url, dss_version, len(projects)))

        if args.project:
            project = None
            for candidate in projects:
                if candidate.get("projectKey") == args.project:
                    project = candidate
                    break
            if project is None:
                print("error: project '{}' not found (or not visible to this "
                      "API key)".format(args.project), file=sys.stderr)
                print("visible projects:", file=sys.stderr)
                for candidate in projects[:15]:
                    print("  {:24} {}".format(
                        candidate.get("projectKey", "?"),
                        candidate.get("name") or ""), file=sys.stderr)
                return EXIT_NOTFOUND
        else:
            project = pick_project(client)
        project_key = project.get("projectKey")

        screen("SCENARIO {!r} in project {}".format(scenario_name, project_key))
        scenario_id, created = ensure_scenario(client, project_key,
                                               scenario_name)
        if scenario_id is None:
            print("  aborted - nothing was changed")
            return EXIT_OK
        set_and_verify_payload(client, project_key, scenario_id, snippet)
        print("  target line    : {} (in the scenario user's ~/.bashrc)".format(
            target_desc))
        print("  restart command: {} restart (when the line is found)".format(
            args.dss_bin))

        if args.no_run:
            print("")
            print("scenario ready. Run it later from the DSS UI:")
            print("  project {} -> Scenarios -> {} -> 'Run now'.".format(
                project_key, scenario_name))
            print("  (the run backs up ~/.bashrc, removes the line, restarts DSS;")
            print("   ~/{} on the host holds the restart output)".format(
                RESTART_LOG_NAME))
            return EXIT_OK

        if not args.yes:
            if not ask_run_now(scenario_name, target_desc, args.dss_bin):
                print("")
                print("scenario ready - NOT run. Run it later from the DSS UI or")
                print("re-run this script (same-name scenarios are reused).")
                return EXIT_OK

        return run_and_watch(client, probe, project_key, scenario_id,
                             scenario_name, args.down_window,
                             args.wait_timeout)
    except Quit:
        print("bye.")
        return EXIT_OK
    except KeyboardInterrupt:
        print("\nbye.")
        return EXIT_OK
    except DssApiError as error:
        print("error: {}".format(error), file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())