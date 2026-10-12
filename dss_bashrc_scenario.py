#!/usr/bin/env python3
"""
dss_bashrc_scenario.py — create a scenario in a Dataiku DSS project that
prints the ~/.bashrc of the DSS host, run it once, and show the output.

Flow: connect -> list all projects -> pick one -> create (or reuse) a
"custom_python" scenario -> set its python payload -> run it -> wait for
the run to finish -> print the run log, which contains the .bashrc.
Plain numbered menus, no curses: works over any SSH session.

Target API: DSS public API 15 — https://doc.dataiku.com/dss/api/15/rest/

Endpoints used (relative to {dss-url}/public/api):
    GET  /projects/                                                    READ_CONF
    GET  /projects/{projectKey}/scenarios/                             MONITOR_JOBS
    POST /projects/{projectKey}/scenarios/                              WRITE_CONF
    GET  /projects/{projectKey}/scenarios/{scenarioId}/payload          MONITOR_JOBS
    PUT  /projects/{projectKey}/scenarios/{scenarioId}/payload          WRITE_CONF
    POST /projects/{projectKey}/scenarios/{scenarioId}/run             RUN_JOBS
    GET  /projects/{projectKey}/scenarios/{scenarioId}/get-last-runs   RUN_JOBS
    GET  /projects/{projectKey}/scenarios/{scenarioId}/{runId}/log     READ_CONF

What runs on DSS: the scenario's python payload (pure stdlib, so it runs
in any code env) prints the DSS host name, the user the scenario runs as
and that user's ~/.bashrc. Note this is the .bashrc of the user running
the DSS backend — not of the API key that created the scenario.

POST …/run does not return a run but a *trigger fire*; the scenario run
appears asynchronously in get-last-runs, where a run is finished exactly
when it carries a "result" object. The script matches its own run by the
trigger-fire runId (with a start-time fallback) and then fetches the
plain-text log from …/scenarios/{scenarioId}/{runId}/log.

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
    DKU_API_KEY=... python3 dss_bashrc_scenario.py
    DKU_API_KEY=... python3 dss_bashrc_scenario.py --project MYPROJECT
    DKU_API_KEY=... python3 dss_bashrc_scenario.py --url https://dss-host:11443 \
        --insecure --no-run
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
POLL_INTERVAL = 2          # seconds between get-last-runs polls
START_SKEW_MS = 5000       # clock-skew allowance when matching "our" run

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NOTFOUND = 3

DEFAULT_SCENARIO_NAME = "Show bashrc"

# The scenario payload. Pure stdlib so it runs in the default DSS backend
# python (no code env needed). A missing .bashrc is a finding, not a
# failure: it is reported and the run still ends SUCCESS.
BASHRC_SNIPPET = '''\
# Prints the ~/.bashrc visible to the DSS backend.
import getpass
import os
import platform

print("DSS host         : " + platform.node())
try:
    user = getpass.getuser()
except Exception:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "(unknown)"
home = os.path.expanduser("~")
print("scenario runs as : " + user)
print("home directory   : " + home)
path = os.path.join(home, ".bashrc")
print("reading          : " + path)
print("=" * 64)
try:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        print(handle.read(), end="")
except OSError as err:
    print("(no .bashrc visible to this scenario: [{}] {})".format(
        err.errno, err.strerror))
print()
print("=" * 64)
print("end of " + path)
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


def ask_run_now():
    """Yes/no prompt, default yes. Raises Quit on EOF."""
    while True:
        try:
            raw = input("Run the scenario now and show the output? [Y/n]: "
                        ).strip().lower()
        except EOFError:
            print("")
            raise Quit()
        if raw in ("", "y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
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


def run_and_show(client, project_key, scenario_id, scenario_name, wait_timeout):
    """Fire the manual trigger, wait for our run, print its log. Exit code."""
    fire_ms = now_ms()
    fire = client.run_scenario(project_key, scenario_id)
    fire_run_id = fire.get("runId")
    print("  trigger fire accepted (trigger run {}, trigger '{}')".format(
        fire_run_id, (fire.get("trigger") or {}).get("id")))
    if fire.get("cancelled"):
        print("  WARNING: the manual trigger fire was cancelled - the scenario")
        print("  did not run (it may already be running). Check the DSS UI:")
        print("  project {} -> Scenarios -> {} -> History".format(
            project_key, scenario_name))
        return EXIT_FAIL
    print("  waiting for the run to finish (Ctrl+C skips the wait)...")
    deadline = time.monotonic() + wait_timeout
    our_run = None
    try:
        while our_run is None:
            time.sleep(POLL_INTERVAL)
            runs = client.get_last_runs(project_key, scenario_id)
            candidates = [r for r in runs
                          if r.get("start", 0) >= fire_ms - START_SKEW_MS]
            match = None
            for run in candidates:
                if (run.get("trigger") or {}).get("runId") == fire_run_id:
                    match = run
                    break
            if match is None and candidates:
                match = candidates[0]  # the listing is newest-first
            if match is not None and "result" in match:
                our_run = match
            elif time.monotonic() >= deadline:
                break
    except KeyboardInterrupt:
        print("\n  (wait interrupted - the run itself stands; check the DSS UI)")
        return EXIT_OK
    if our_run is None:
        print("  WARNING: the run did not finish within {}s - it may still be".format(
            wait_timeout))
        print("  running on DSS. Check the log later: project {} -> Scenarios".format(
            project_key) + " -> {} -> History".format(scenario_name))
        return EXIT_FAIL
    run_id = our_run.get("runId")
    if not run_id:
        raise DssApiError("finished run without a runId: {}".format(our_run))
    outcome = (our_run.get("result") or {}).get("outcome", "?")
    print("  run finished: {}".format(outcome))
    log = client.get_run_log(project_key, scenario_id, run_id)
    screen("SCENARIO RUN LOG - {} - run {}".format(scenario_name, run_id))
    print(log.rstrip("\n"))
    print("")
    print("(the same log lives in DSS: project {} -> Scenarios -> {} -> History)".format(
        project_key, scenario_name))
    if outcome in ("FAILED", "ABORTED"):
        return EXIT_FAIL
    return EXIT_OK


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="dss_bashrc_scenario.py",
        description="Create a DSS scenario that prints the DSS host's "
                    "~/.bashrc, run it, and show its output, via the public "
                    "REST API.",
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
    parser.add_argument("--scenario-name", default=DEFAULT_SCENARIO_NAME,
                        metavar="NAME",
                        help="name of the scenario to create/reuse "
                             "(default: {!r}; a same-name scenario is reused, "
                             "not duplicated)".format(DEFAULT_SCENARIO_NAME))
    parser.add_argument("--payload-file", metavar="FILE",
                        help="run this file's python as the scenario payload "
                             "instead of the built-in show-~/.bashrc snippet")
    parser.add_argument("--no-run", action="store_true",
                        help="create/set the scenario but do not run it")
    parser.add_argument("--wait-timeout", type=int, default=90, metavar="SEC",
                        help="seconds to wait for the scenario run to finish "
                             "(default: 90)")
    parser.add_argument("--timeout", type=int, default=30, metavar="SEC",
                        help="per-call HTTP timeout in seconds (default: 30)")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS certificate verification (self-signed DSS)")
    return parser.parse_args(argv)


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
        return BASHRC_SNIPPET
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
        snippet = read_payload(args.payload_file)
        client = DssClient(root_url + API_PREFIX, api_key,
                           insecure=args.insecure, timeout=args.timeout)

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

        screen("SCENARIO {!r} in project {}".format(args.scenario_name,
                                                    project_key))
        scenario_id, created = ensure_scenario(client, project_key,
                                               args.scenario_name)
        if scenario_id is None:
            print("  aborted - nothing was changed")
            return EXIT_OK
        set_and_verify_payload(client, project_key, scenario_id, snippet)

        if args.no_run:
            print("")
            print("scenario ready. Run it later from the DSS UI:")
            print("  project {} -> Scenarios -> {} -> 'Run now'; the output".format(
                project_key, args.scenario_name) + " is in the run's History log.")
            return EXIT_OK

        if not ask_run_now():
            print("")
            print("scenario ready. Run it later from the DSS UI:")
            print("  project {} -> Scenarios -> {} -> 'Run now'; the output".format(
                project_key, args.scenario_name) + " is in the run's History log.")
            return EXIT_OK

        return run_and_show(client, project_key, scenario_id,
                            args.scenario_name, args.wait_timeout)
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