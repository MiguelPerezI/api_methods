#!/usr/bin/env python3
"""
dss_scenariorun_console.py — pick a DSS project, pick one of its existing
scenarios, run it, and show the run's status and log, via the public REST
API. Nothing is created, modified or deleted: this RUNS what already
exists in DSS.

Flow:
    connect -> list projects -> pick one -> list its scenarios (with
    last-run status markers) -> pick one -> confirm -> fire the manual
    trigger -> poll get-last-runs until the run finishes -> show the
    outcome and the run log. Then optionally loop back to the scenario
    picker ("run another one from this project?").

Endpoints used (relative to {dss-url}/public/api):
    GET  /projects/                                           READ_CONF
    GET  /projects/{projectKey}/scenarios/                    MONITOR_JOBS
    GET  /projects/{projectKey}/scenarios/{id}/get-last-runs  RUN_JOBS
    POST /projects/{projectKey}/scenarios/{id}/run             RUN_JOBS
    GET  /projects/{projectKey}/scenarios/{id}/{runId}/log     READ_CONF

Notes:
  - a manual run is an async trigger fire: POST .../run returns the
    trigger fire {runId, trigger, cancelled}; the activity run is done
    exactly when a matching run in get-last-runs carries a "result" key.
  - the log lives at .../scenarios/{id}/{runId}/log — no "runs" segment
    in the real API's path.
  - scenarios are addressed by internal id; the picker resolves names,
    and --scenario accepts id or name. DSS allows duplicate names, so an
    ambiguous name asks for the id.
  - the last-run markers need one extra get-last-runs call per scenario;
    a key with MONITOR_JOBS but not RUN_JOBS sees the list without them.
  - SSO being down does not block this tool: the API key authenticates
    itself and never calls the identity provider.

Examples:
    DKU_API_KEY=... python3 dss_scenariorun_console.py
    DKU_API_KEY=... python3 dss_scenariorun_console.py --project MYPROJECT
    DKU_API_KEY=... python3 dss_scenariorun_console.py --project MYPROJECT --list
    DKU_API_KEY=... python3 dss_scenariorun_console.py --project MYPROJECT \
        --scenario "Nightly rebuild" --yes
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

DEFAULT_WAIT_TIMEOUT = 240  # seconds to wait for a run to finish

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NOTFOUND = 3


class Quit(Exception):
    """Raised to leave the script cleanly (q, quit, EOF)."""


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

    def run_scenario(self, project_key, scenario_id):
        # Body = trigger params; {} fires the manual trigger. The answer is
        # the async trigger fire, not a finished run.
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
            raw = input("Run this scenario now? [Y/n]: ").strip().lower()
        except EOFError:
            print("")
            raise Quit()
        if raw in ("", "y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  ? please answer y or n")


def ask_run_another():
    """Yes/no prompt, default no. Raises Quit on EOF/None. A Ctrl+C return
    is handled by main(); EOF answers no (one run completed, be gentle)."""
    while True:
        try:
            raw = input("Run another scenario from this project? [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            print("")
            raise Quit()
        raw = raw.strip().lower()
        if raw in ("", "n", "no"):
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


def scenario_rows(client, project_key):
    """Fetch + sort the project's scenarios and build last-run markers.
    Returns (scenarios, markers); markers[id] is a short status string.
    One cheap get-last-runs(limit=1) call per scenario; a call that fails
    (e.g. a key with MONITOR_JOBS but not RUN_JOBS) leaves no marker."""
    try:
        scenarios = client.list_scenarios(project_key)
    except DssApiError as err:
        return [], str(err)
    scenarios = sorted(scenarios,
                       key=lambda s: str(s.get("name", s.get("id", "?"))).lower())
    markers = {}
    for scenario in scenarios:
        scenario_id = scenario.get("id")
        if not scenario_id:
            continue
        try:
            runs = client.get_last_runs(project_key, scenario_id, limit=1)
        except DssApiError:
            markers[scenario_id] = "(no last-run info)"
            continue
        run = runs[0] if runs else None
        if run is None:
            markers[scenario_id] = "(never run)"
        elif "result" not in run:
            markers[scenario_id] = "(RUNNING now)"
        else:
            markers[scenario_id] = "(last: {})".format(
                (run.get("result") or {}).get("outcome", "finished"))
    return scenarios, markers


def render_scenarios(scenarios, markers, error=None):
    """Print the scenario listing (no prompt) — shared by the picker and
    --list mode."""
    screen("SCENARIOS in this project ({} listed)".format(len(scenarios)))
    if error:
        print("  error: {}".format(error))
    elif not scenarios:
        print("  (no scenarios in this project - create one in the DSS UI,")
        print("   or with one of the scenario tools in this repo)")
    for i, scenario in enumerate(scenarios, 1):
        scenario_id = str(scenario.get("id") or "?")
        parts = []
        if scenario.get("active") is False:
            parts.append("[inactive]")
        marker = markers.get(scenario_id, "")
        if marker:
            parts.append(marker)
        parts.append("id {}".format(scenario_id))
        print("  {:2}) {:30} {:13} {}".format(
            i, scenario.get("name") or "?", scenario.get("type") or "?",
            "  ".join(parts)))


def pick_scenario(client, project_key):
    """Scenario picker; returns a scenario dict. 'r' refreshes (and the
    last-run markers are rebuilt every pass). Raises Quit on q/EOF."""
    while True:
        scenarios, markers = scenario_rows(client, project_key)
        error = markers if isinstance(markers, str) else None
        display_markers = {} if error else markers
        if error:
            scenarios = []
        render_scenarios(scenarios, display_markers, error)
        choice = ask(len(scenarios), ("r",))
        if choice == "r":
            continue
        return scenarios[choice]


def resolve_scenario(scenarios, spec):
    """Match a --scenario spec against the listing: id first, then name
    (case-insensitive; DSS allows duplicate names). Returns
    (scenario, None) on a unique match, or (None, ambiguous_matches) —
    ambiguous_matches is None when nothing matched, a list when a NAME
    matched more than one scenario."""
    spec_l = str(spec).strip().lower()
    by_id = [s for s in scenarios if str(s.get("id", "")).lower() == spec_l]
    if len(by_id) == 1:
        return by_id[0], None
    by_name = [s for s in scenarios
               if str(s.get("name", "")).lower() == spec_l]
    if len(by_name) == 1:
        return by_name[0], None
    if len(by_name) > 1:
        return None, by_name
    return None, None


def run_scenario_now(client, project_key, scenario, wait_timeout, assume_yes):
    """Confirm (unless --yes), fire the manual trigger, wait for the run,
    show the outcome and the run log. Returns the run's exit code."""
    scenario_id = scenario.get("id")
    view_name = scenario.get("name") or scenario_id or ""
    print("")
    print("scenario: {!r}  id {}  type {}  active {}".format(
        view_name, scenario_id, scenario.get("type") or "?",
        scenario.get("active", True)))
    if not assume_yes and not ask_run_now():
        print("  not run - nothing changed")
        return EXIT_OK

    fire_ms = now_ms()
    fire = client.run_scenario(project_key, scenario_id)
    fire_run_id = fire.get("runId")
    print("  trigger fire accepted (trigger run {}, trigger '{}')".format(
        fire_run_id, (fire.get("trigger") or {}).get("id")))
    if fire.get("cancelled"):
        print("  WARNING: the manual trigger fire was cancelled - the scenario")
        print("  did not start (it may already be running, or a trigger beat")
        print("  us to it). Check the DSS UI: project {} -> Scenarios ->".format(
            project_key) + " {} -> History".format(view_name))
        return EXIT_FAIL
    print("  waiting for the run to finish (Ctrl+C skips the wait; the run")
    print("  itself keeps going in DSS)")
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
        print("\n  (wait interrupted - outcome unknown; the run keeps going.")
        print("  Check it with another pass of this script, or in the DSS UI:")
        print("  project {} -> Scenarios -> {} -> History)".format(
            project_key, view_name))
        return EXIT_OK
    if our_run is None:
        print("  WARNING: the run did not finish within {}s - it may still be".format(
            wait_timeout))
        print("  running on DSS. Check later: project {} -> Scenarios ->".format(
            project_key) + " {} -> History".format(view_name))
        return EXIT_FAIL

    run_id = our_run.get("runId")
    if not run_id:
        raise DssApiError("finished run without a runId: {}".format(our_run))
    outcome = (our_run.get("result") or {}).get("outcome", "?")
    end_ms = our_run.get("end")
    start_ms = our_run.get("start")
    if end_ms and start_ms:
        print("  run finished: {} (in {:.1f}s)".format(outcome,
                                                       (end_ms - start_ms) / 1000.0))
    else:
        print("  run finished: {}".format(outcome))
    try:
        log = client.get_run_log(project_key, scenario_id, run_id)
    except DssApiError as err:
        print("  (no log available for run {}: {})".format(run_id, err))
        log = ""
    screen("RUN LOG - {} - run {}".format(view_name, run_id))
    if log.strip():
        print(log.rstrip("\n"))
    else:
        print("(the run produced no log output)")
    print("")
    print("(the same run lives in DSS: project {} -> Scenarios -> {} ->".format(
        project_key, view_name) + " History).")
    if outcome in ("FAILED", "ABORTED"):
        return EXIT_FAIL
    return EXIT_OK


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="dss_scenariorun_console.py",
        description="Pick a DSS project, pick one of its existing scenarios, "
                    "run it, and show the run's status and log, via the "
                    "public REST API. Nothing is created or modified.",
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
    parser.add_argument("--scenario", metavar="ID|NAME",
                        help="scenario id or name to run (skips the "
                             "interactive scenario picker; exit 3 when not "
                             "found; an ambiguous name exits 3 and lists "
                             "the matching ids)")
    parser.add_argument("--list", action="store_true",
                        help="list the project's scenarios with last-run "
                             "markers and exit (nothing is run)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the run confirmation (the scenario may "
                             "change data - know what you are running)")
    parser.add_argument("--wait-timeout", type=int, default=DEFAULT_WAIT_TIMEOUT,
                        metavar="SEC",
                        help="seconds to wait for the run to finish (default "
                             "{}; longer runs can exceed this - the run keeps ".format(
                                 DEFAULT_WAIT_TIMEOUT) +
                             "going in DSS and can be checked with --list or "
                             "in the UI)")
    parser.add_argument("--timeout", type=int, default=30, metavar="SEC",
                        help="per-call HTTP timeout in seconds (default: 30)")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS certificate verification (self-signed DSS)")
    args = parser.parse_args(argv)
    if args.scenario is not None and not args.scenario.strip():
        parser.error("--scenario must not be empty")
    args.scenario = args.scenario.strip() if args.scenario is not None else None
    if args.wait_timeout < 1:
        parser.error("--wait-timeout must be >= 1")
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

        if args.list:
            scenarios, markers = scenario_rows(client, project_key)
            render_scenarios(scenarios, markers,
                             error=markers if isinstance(markers, str) else None)
            return EXIT_OK

        spec = args.scenario
        exit_code = EXIT_OK
        while True:
            if spec is not None:
                scenarios, markers = scenario_rows(client, project_key)
                chosen, ambiguous = resolve_scenario(scenarios, spec)
                if ambiguous:
                    render_scenarios(scenarios, markers)
                    print("  '{}' matches {} scenarios by name - run one by id:"
                          .format(spec, len(ambiguous)))
                    for match in ambiguous:
                        print("    id {}  {!r}".format(
                            match.get("id"), match.get("name") or "?"))
                    return EXIT_NOTFOUND
                if chosen is None:
                    print("error: scenario '{}' not found in project {} (or "
                          "not visible to this API key)".format(
                              spec, project_key), file=sys.stderr)
                    print("visible scenarios:", file=sys.stderr)
                    for candidate in scenarios[:15]:
                        print("  {:26} id {}".format(
                            candidate.get("name") or "?",
                            candidate.get("id") or "?"), file=sys.stderr)
                    return EXIT_NOTFOUND
                spec = None  # only the first pass is non-interactive
            else:
                chosen = pick_scenario(client, project_key)
            exit_code = run_scenario_now(client, project_key, chosen,
                                         args.wait_timeout, args.yes)
            try:
                again = ask_run_another()
            except Quit:
                # leaving right after a run: the loop returns the run's own
                # exit code, not a blanket "clean exit"
                return exit_code
            if not again:
                break
        if exit_code == EXIT_OK:
            print("")
            print("done. Re-check any run later with --list, or in the DSS")
            print("UI: project {} -> Scenarios -> History.".format(project_key))
        return exit_code
    except Quit:
        print("bye.")
        return EXIT_OK
    except KeyboardInterrupt:
        print("\nbye.")
        return EXIT_OK
    except DssApiError as error:
        print("error: {}".format(error), file=sys.stderr)
        if getattr(error, "status", None) == 401:
            print("(the API key was rejected - check DKU_API_KEY)", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())