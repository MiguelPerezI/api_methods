#!/usr/bin/env python3
"""
dss_scenariocopy_console.py — two-DSS scenario console: connect to a
source DSS instance, pick a project, pick one of its existing scenarios,
then run / delete it there — or copy it onto a **second** DSS instance
(pick the target project), after which the copy on the target can be run
or deleted. All via the public REST API.

Flow:
    connect source -> list projects -> pick one -> list its scenarios
    (with last-run markers) -> pick one -> actions menu:
        run    - fire the manual trigger, poll, show outcome + run log
        copy   - connect the target DSS (lazily, only now), list its
                 projects, pick one, copy the scenario over (definition
                 + payload; the copy is created/brought **inactive** so
                 its automatic triggers cannot fire before review), then
                 post-copy menu: run / delete the copy on the target
        delete - confirm (default NO), delete from the source project
    'b' goes back to the scenario picker, 'q' quits anywhere.

Target API: DSS public API 15 — https://doc.dataiku.com/dss/api/15/rest/

Endpoints used (relative to {dss-url}/public/api):
    GET    /projects/                                          READ_CONF
    GET    /projects/{projectKey}/scenarios/                   MONITOR_JOBS
    GET    /projects/{projectKey}/scenarios/{id}/              MONITOR_JOBS
    POST   /projects/{projectKey}/scenarios/                    WRITE_CONF
    PUT    /projects/{projectKey}/scenarios/{id}                WRITE_CONF
    GET    /projects/{projectKey}/scenarios/{id}/payload       MONITOR_JOBS
    PUT    /projects/{projectKey}/scenarios/{id}/payload        WRITE_CONF
    DELETE /projects/{projectKey}/scenarios/{id}/               WRITE_CONF
    POST   /projects/{projectKey}/scenarios/{id}/run            RUN_JOBS
    GET    /projects/{projectKey}/scenarios/{id}/get-last-runs  RUN_JOBS
    GET    /projects/{projectKey}/scenarios/{id}/{runId}/log    READ_CONF

Notes:
  - GET /projects/{pk}/scenarios/{id}/ returns the full scenario object
    (name/type/active/description/params/triggers/reporters). On an old
    DSS without that endpoint (404) the copy falls back to the listing
    item (name/type/active) + payload: a step_based scenario's steps are
    then NOT copied (a loud note is printed).
  - create accepts a requested "id" in the body; when the id is already
    taken in the target project the clash menu offers overwrite (PUT
    settings + payload), keep-both (POST without id, name gets a " (copy)"
    suffix), or abort (default).
  - a manual run is an async trigger fire: POST .../run returns
    {runId, trigger, cancelled}; the activity run is done exactly when a
    matching run in get-last-runs carries a "result" key.
  - the log lives at .../scenarios/{id}/{runId}/log — no "runs" segment
    in the real API's path.
  - scenarios are addressed by internal id; names may be duplicated, so
    --scenario resolves id first and name second, and an ambiguous name
    exits 3 listing the matching ids.
  - SSO being down does not block this tool: the API keys authenticate
    themselves and never call the identity provider.

Connection (source), in order of precedence:
    1. --url http://host:port
    2. env DSS_URL
    3. the CONNECTION DEFAULTS block below (edit by hand for your host/port)
    4. if none of the above: the script asks for the URL at startup
Source API key: env DKU_API_KEY / --api-key-file / hidden prompt.

Connection (target), asked only when a copy is chosen:
    1. --target-url http://host:port     (env DSS_URL_TARGET)
    2. --target-api-key-file FILE        (env DKU_API_KEY_TARGET)
    3. interactive prompts (URL + hidden key) as the fallback
Never a --api-key flag: keys stay out of `ps` and shell history.

Examples:
    DKU_API_KEY=... python3 dss_scenariocopy_console.py
    DKU_API_KEY=... python3 dss_scenariocopy_console.py --project MYPROJECT
    DKU_API_KEY=... DSS_URL_TARGET=http://dss-b:11000 \
        DKU_API_KEY_TARGET=... python3 dss_scenariocopy_console.py
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
# CONNECTION DEFAULTS (source DSS) — edit these by hand to point at your
# DSS host, or override with --url / env DSS_URL (both take precedence).
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

    def get_scenario(self, project_key, scenario_id):
        """The full scenario object (documented with a trailing slash)."""
        payload, _ = self.call("GET", "/projects/{}/scenarios/{}/".format(
            q(project_key), q(scenario_id)))
        return payload

    def create_scenario(self, project_key, definition):
        """POST the full scenario object; the response carries 'id'."""
        payload, _ = self.call("POST", "/projects/{}/scenarios/".format(
            q(project_key)), body=definition)
        return payload

    def update_scenario(self, project_key, scenario_id, definition):
        """PUT the scenario settings (documented success: 204 no content)."""
        payload, _ = self.call("PUT", "/projects/{}/scenarios/{}".format(
            q(project_key), q(scenario_id)), body=definition)
        return payload

    def get_payload(self, project_key, scenario_id):
        payload, _ = self.call("GET", "/projects/{}/scenarios/{}/payload".format(
            q(project_key), q(scenario_id)))
        return payload

    def set_payload(self, project_key, scenario_id, script):
        payload, _ = self.call("PUT", "/projects/{}/scenarios/{}/payload".format(
            q(project_key), q(scenario_id)), body={"script": script})
        return payload

    def delete_scenario(self, project_key, scenario_id):
        payload, _ = self.call("DELETE", "/projects/{}/scenarios/{}".format(
            q(project_key), q(scenario_id)))
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


def ask_yes_no(question, default_yes=False):
    """Yes/no prompt; `default_yes` picks the empty-answer default.
    Raises Quit on EOF."""
    suffix = " [Y/n]: " if default_yes else " [y/N]: "
    while True:
        try:
            raw = input(question + suffix).strip().lower()
        except EOFError:
            print("")
            raise Quit()
        if raw == "":
            return default_yes
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  ? please answer y or n")


# ---------------------------------------------------------------------------
# listings / pickers
# ---------------------------------------------------------------------------

def pick_project(client, context=""):
    """Project picker; returns a project dict. Refresh with 'r', quit with 'q'."""
    while True:
        error = None
        try:
            projects = client.list_projects()
        except DssApiError as err:
            projects, error = [], str(err)
        projects = sorted(projects,
                          key=lambda p: str(p.get("projectKey", "")).lower())
        screen("PROJECTS{} ({} visible to this API key)".format(
            context, len(projects)))
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


def last_run_marker(client, project_key, scenario_id):
    """Short status string for one scenario from one get-last-runs(limit=1)
    call; a failing call (e.g. key without RUN_JOBS) leaves no marker."""
    try:
        runs = client.get_last_runs(project_key, scenario_id, limit=1)
    except DssApiError:
        return "(no last-run info)"
    run = runs[0] if runs else None
    if run is None:
        return "(never run)"
    if "result" not in run:
        return "(RUNNING now)"
    return "(last: {})".format((run.get("result") or {}).get("outcome",
                                                              "finished"))


def scenario_rows(client, project_key):
    """Fetch + sort the project's scenarios and build last-run markers.
    Returns (scenarios, markers); on a listing error returns ([], error)."""
    try:
        scenarios = client.list_scenarios(project_key)
    except DssApiError as err:
        return [], str(err)
    scenarios = sorted(scenarios,
                       key=lambda s: str(s.get("name", s.get("id", "?"))).lower())
    markers = {}
    for scenario in scenarios:
        scenario_id = scenario.get("id")
        if scenario_id:
            markers[scenario_id] = last_run_marker(client, project_key,
                                                   scenario_id)
    return scenarios, markers


def pick_scenario(client, project_key):
    """Scenario picker; returns a scenario dict. 'r' refreshes. Raises Quit
    on q/EOF."""
    while True:
        scenarios, markers = scenario_rows(client, project_key)
        error = markers if isinstance(markers, str) else None
        display_markers = {} if error else markers
        if error:
            scenarios = []
        screen("SCENARIOS in this project ({} listed)".format(len(scenarios)))
        if error:
            print("  error: {}".format(error))
        elif not scenarios:
            print("  (no scenarios in this project)")
        for i, scenario in enumerate(scenarios, 1):
            scenario_id = str(scenario.get("id") or "?")
            parts = []
            if scenario.get("active") is False:
                parts.append("[inactive]")
            marker = display_markers.get(scenario_id, "")
            if marker:
                parts.append(marker)
            parts.append("id {}".format(scenario_id))
            print("  {:2}) {:30} {:13} {}".format(
                i, scenario.get("name") or "?", scenario.get("type") or "?",
                "  ".join(parts)))
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


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------

def fire_and_follow(client, project_key, scenario_id, view_name, wait_timeout):
    """Fire the manual trigger, wait for our run, show outcome + log.
    Returns the run's exit code (1 for FAILED/ABORTED/timeout)."""
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
# copying
# ---------------------------------------------------------------------------

def fetch_definition(client, project_key, scenario):
    """The full scenario object via GET .../scenarios/{id}/; on a DSS that
    lacks the endpoint (404), fall back to the listing item (name/type/
    active only — steps/triggers/reporters are then NOT reachable)."""
    scenario_id = scenario.get("id")
    try:
        return client.get_scenario(project_key, scenario_id), True
    except DssApiError as err:
        if err.status != 404:
            raise
    print("  NOTE: this DSS has no full-scenario endpoint (GET "
          ".../scenarios/{}/ answered 404) -".format(scenario_id))
    print("  falling back to the listing entry: name/type/active and the")
    print("  payload are copied, but steps, triggers and reporters are NOT.")
    return {
        "id": scenario_id,
        "name": scenario.get("name"),
        "type": scenario.get("type"),
        "active": scenario.get("active", True),
    }, False


def copy_body(definition, with_id=True, rename=False):
    """Build the create/update body from the source scenario object: the
    documented fields only, forced INACTIVE so the copy's automatic
    triggers cannot fire before review on the target."""
    body = {}
    for field in ("name", "description", "type", "params", "triggers",
                  "reporters"):
        if field in definition and definition[field] is not None:
            body[field] = definition[field]
    body["active"] = False
    if rename:
        body["name"] = "{} (copy)".format(body.get("name") or "")
    if with_id and definition.get("id"):
        body["id"] = definition["id"]
    return body


def describe_definition(definition):
    """One-line inventory of what a copy carries, for the report."""
    params = definition.get("params") or {}
    steps = params.get("steps") or []
    triggers = definition.get("triggers") or []
    reporters = definition.get("reporters") or []
    return ("{} step(s), {} trigger(s), {} reporter(s), active {}".format(
        len(steps), len(triggers), len(reporters),
        definition.get("active", True)))


def copy_scenario(source, source_project_key, scenario,
                  target, target_project_key):
    """Copy one scenario onto the target DSS project. Returns
    (new_scenario_id, new_name) or None when the user aborts at the
    clash menu."""
    scenario_id = scenario.get("id")
    view_name = scenario.get("name") or scenario_id or ""
    definition, full = fetch_definition(source, source_project_key, scenario)
    print("")
    print("copying scenario {!r} (id {}, type {}):".format(
        view_name, scenario_id, definition.get("type") or "?"))
    print("  source definition: " + describe_definition(definition))

    script = None
    try:
        script = source.get_payload(source_project_key, scenario_id).get(
            "script", "")
    except DssApiError:
        script = None  # not a custom scenario: no payload behind the API
    if script is None:
        print("  payload          : none (not a custom scenario)")
    else:
        print("  payload          : {} line(s) of python".format(
            script.count("\n") + (1 if script else 0)))

    existing = None
    try:
        target_scenarios = target.list_scenarios(target_project_key)
    except DssApiError:
        target_scenarios = []
    for candidate in target_scenarios:
        if candidate.get("id") == scenario_id:
            existing = candidate
            break

    overwrite = False
    if existing is None:
        response = target.create_scenario(target_project_key,
                                          copy_body(definition, with_id=True))
        new_id = response.get("id") or scenario_id
        print("  created on target: id {} (inactive)".format(new_id))
    else:
        print("  the target project already has a scenario with id {}:".format(
            scenario_id))
        print("  existing: {!r} type {}".format(
            existing.get("name") or "?", existing.get("type") or "?"))
        can_overwrite = existing.get("type") == definition.get("type")
        if can_overwrite:
            print("   1) overwrite it - settings + payload replaced by the"
                  " source's")
            print("   2) keep both    - create the copy as a new scenario"
                  " (new id,")
            print("                     name {!r})".format(
                (definition.get("name") or "") + " (copy)"))
            print("   3) abort        - change nothing on the target")
            choice = ask(3, default=2)
        else:
            print("  (its type differs from the source's - overwrite is not"
                  " offered)")
            print("   1) keep both    - create the copy as a new scenario"
                  " (new id,")
            print("                     name {!r})".format(
                (definition.get("name") or "") + " (copy)"))
            print("   2) abort        - change nothing on the target")
            choice = ask(2, default=1)
            if choice == 0:
                choice = 1
            else:
                choice = 2
        if choice == 2:
            print("  aborted - nothing was changed on the target")
            return None
        if choice == 0:
            overwrite = True
            body = copy_body(definition, with_id=True)
            body.pop("id", None)  # the id is fixed by the path on update
            target.update_scenario(target_project_key, scenario_id, body)
            new_id = scenario_id
            print("  overwritten on target: id {} (inactive)".format(new_id))
        else:
            response = target.create_scenario(
                target_project_key,
                copy_body(definition, with_id=False, rename=True))
            new_id = response.get("id")
            if not new_id:
                raise DssApiError(
                    "DSS created the scenario but returned no id: {}".format(
                        response))
            print("  created on target: id {} (inactive, name {!r})".format(
                new_id, (definition.get("name") or "") + " (copy)"))

    if script is not None or overwrite:
        # create path: only copy a non-empty script; overwrite path: always
        # PUT the payload (even empty) so a stale target script cannot
        # survive under a mirrored definition.
        target.set_payload(target_project_key, new_id, script or "")
        echo = target.get_payload(target_project_key, new_id).get("script", "")
        if echo != (script or ""):
            print("  WARNING: stored payload differs from the source's")
        else:
            print("  payload round-trip verified on target" if script else
                  "  payload cleared on target (source has none)")
    return new_id, view_name


# ---------------------------------------------------------------------------
# flow steps
# ---------------------------------------------------------------------------

def ensure_target(state, args):
    """Lazily connect the target DSS (first copy only). Fills
    state['target'] with (client, root_url)."""
    if state.get("target"):
        return state["target"]
    root_url = resolve_target_url(args)
    api_key = read_api_key(args.target_api_key_file, "DKU_API_KEY_TARGET",
                           "Target DSS API key (input hidden): ")
    client = DssClient(root_url + API_PREFIX, api_key,
                       insecure=args.insecure, timeout=args.timeout)
    projects, headers = client.call("GET", "/projects/")
    dss_version = headers.get("DSS-Version") or "?"
    print("")
    print("connected to target {} - DSS version {} - {} project(s) visible"
          .format(root_url, dss_version, len(projects)))
    if root_url == state.get("source_url"):
        print("(note: the target URL is the source instance - this copies")
        print(" within the same DSS, e.g. from one project to another)")
    state["target"] = (client, root_url)
    return state["target"]


def pick_target_project(target_client, args):
    """--target-project resolution (exit 3 when not visible) or picker."""
    if not args.target_project:
        return pick_project(target_client, context=" on the target DSS")
    projects = target_client.list_projects()
    for candidate in projects:
        if candidate.get("projectKey") == args.target_project:
            return candidate
    print("error: project '{}' not found on the target DSS (or not visible "
          "to the API key)".format(args.target_project), file=sys.stderr)
    print("visible projects on the target:", file=sys.stderr)
    for candidate in projects[:15]:
        print("  {:24} {}".format(candidate.get("projectKey", "?"),
                                   candidate.get("name") or ""),
              file=sys.stderr)
    sys.exit(EXIT_NOTFOUND)


def post_copy_menu(state, target_client, target_project_key,
                   new_scenario_id, scenario_name, args):
    """After a copy: run / delete the copy on the target, or go back."""
    while True:
        screen("COPIED to the target DSS - project {} - id {}".format(
            target_project_key, new_scenario_id))
        print("   1) run     run the copied scenario now on the target")
        print("   2) delete  delete the copied scenario from the target")
        print("   3) back    return to the source scenario's actions")
        choice = ask(3)
        if choice == 0:
            state["last_run_code"] = fire_and_follow(
                target_client, target_project_key, new_scenario_id,
                scenario_name, args.wait_timeout)
        elif choice == 1:
            if delete_confirmed("target", target_project_key, scenario_name,
                                new_scenario_id, args.yes):
                target_client.delete_scenario(target_project_key,
                                              new_scenario_id)
                print("  deleted from the target project")
                return  # the copy is gone - nothing more to offer on it
        else:
            return


def delete_confirmed(side, project_key, scenario_name, scenario_id,
                     assume_yes):
    """Default-NO delete gate (scenario run history goes with it)."""
    if assume_yes:
        return True
    return ask_yes_no(
        "Delete scenario {!r} (id {}) from the {} project {}? Its run "
        "history goes with it.".format(scenario_name, scenario_id, side,
                                       project_key),
        default_yes=False)


def actions_loop(state, source, source_project_key, scenario, args):
    """The run / copy / delete menu for one source scenario. Returns to the
    scenario picker when 'b' is chosen or the scenario was deleted."""
    scenario_id = scenario.get("id")
    view_name = scenario.get("name") or scenario_id or ""
    while True:
        marker = last_run_marker(source, source_project_key, scenario_id)
        screen("SCENARIO {!r} - project {} on {}".format(
            view_name, source_project_key, state["source_url"]))
        print("  id {} - type {} - active {} - {}".format(
            scenario_id, scenario.get("type") or "?",
            scenario.get("active", True), marker or "no run info"))
        print("")
        print("   1) run     fire the manual trigger here, show the run log")
        print("   2) copy    copy this scenario to the target DSS")
        print("   3) delete  delete it from this project (runs history too)")
        choice = ask(3, ("b",))
        if choice == "b":
            return
        if choice == 0:
            state["last_run_code"] = fire_and_follow(
                source, source_project_key, scenario_id, view_name,
                args.wait_timeout)
        elif choice == 1:
            target_client, _ = ensure_target(state, args)
            target_project = pick_target_project(target_client, args)
            target_project_key = target_project.get("projectKey")
            copied = copy_scenario(source, source_project_key, scenario,
                                   target_client, target_project_key)
            if copied is not None:
                new_id, new_name = copied
                post_copy_menu(state, target_client, target_project_key,
                               new_id, new_name, args)
        else:
            if delete_confirmed("source", source_project_key, view_name,
                                scenario_id, args.yes):
                source.delete_scenario(source_project_key, scenario_id)
                print("  deleted from project {}".format(source_project_key))
                return  # the scenario is gone - back to the picker
            print("  not deleted - nothing changed")


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="dss_scenariocopy_console.py",
        description="Two-DSS scenario console: pick a project on the source "
                    "DSS, pick one of its scenarios, then run / delete it "
                    "there or copy it to a project on a second (target) DSS "
                    "(definition + payload, created inactive), with run / "
                    "delete of the copy on the target afterwards.",
        epilog="Source connection: --url, env DSS_URL, or the CONNECTION "
               "DEFAULTS at the top of this file; the script prompts for the "
               "URL if none is set. Target connection (asked only when "
               "copying): --target-url / env DSS_URL_TARGET / prompt. Keys: "
               "env DKU_API_KEY / --api-key-file (source), env "
               "DKU_API_KEY_TARGET / --target-api-key-file (target), or "
               "hidden prompts. API docs: "
               "https://doc.dataiku.com/dss/api/15/rest/")
    parser.add_argument("--url",
                        help="source DSS base URL, e.g. http://dss-host:11000 "
                             "(default: env DSS_URL, then built-in defaults)")
    parser.add_argument("--target-url",
                        help="target DSS base URL (default: env "
                             "DSS_URL_TARGET, then an interactive prompt - "
                             "asked only when a copy is chosen)")
    parser.add_argument("--api-key-file", metavar="FILE",
                        help="file holding the source DSS API key (default: "
                             "env DKU_API_KEY, then a hidden prompt)")
    parser.add_argument("--target-api-key-file", metavar="FILE",
                        help="file holding the target DSS API key (default: "
                             "env DKU_API_KEY_TARGET, then a hidden prompt - "
                             "asked only when a copy is chosen)")
    parser.add_argument("--project", metavar="KEY",
                        help="source project key (skips the interactive "
                             "picker; exit 3 when the key is not visible)")
    parser.add_argument("--scenario", metavar="ID|NAME",
                        help="scenario id or name (skips the interactive "
                             "scenario picker; exit 3 when not found; an "
                             "ambiguous name exits 3 and lists the matching "
                             "ids)")
    parser.add_argument("--target-project", metavar="KEY",
                        help="target project key for copies (skips the "
                             "interactive target picker; exit 3 when the key "
                             "is not visible on the target DSS)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the delete confirmations (deleting a "
                             "scenario also deletes its run history)")
    parser.add_argument("--wait-timeout", type=int, default=DEFAULT_WAIT_TIMEOUT,
                        metavar="SEC",
                        help="seconds to wait for a run to finish (default "
                             "{}; longer runs keep going in DSS)".format(
                                 DEFAULT_WAIT_TIMEOUT))
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


def read_api_key(api_key_file, env_name, prompt_label):
    if api_key_file:
        with open(api_key_file, "r", encoding="utf-8") as handle:
            key = handle.read().strip()
        if not key:
            raise DssApiError("API key file {!r} is empty".format(api_key_file))
        return key
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    key = getpass.getpass(prompt_label).strip()
    if not key:
        raise DssApiError("no API key provided ({})".format(env_name))
    return key


def resolve_base_url(args):
    """Source URL: --url > env DSS_URL > hand-edited defaults > prompt."""
    if args.url:
        return args.url.rstrip("/")
    env_url = os.environ.get("DSS_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    if DSS_HOST and DSS_HOST != PLACEHOLDER_HOST:
        scheme = "https" if DSS_USE_TLS else "http"
        return "{}://{}:{}".format(scheme, DSS_HOST, DSS_PORT)
    print("No source DSS URL configured. Options: --url, env DSS_URL, or")
    print("edit the CONNECTION DEFAULTS at the top of this file.")
    while True:
        try:
            raw = input("Source DSS URL [http://host:port]: ").strip()
        except EOFError:
            print("")
            raise Quit()
        if re.match(r"^https?://\S+$", raw):
            return raw.rstrip("/")


def resolve_target_url(args):
    """Target URL: --target-url > env DSS_URL_TARGET > prompt."""
    if args.target_url:
        return args.target_url.rstrip("/")
    env_url = os.environ.get("DSS_URL_TARGET", "").strip()
    if env_url:
        return env_url.rstrip("/")
    print("No target DSS URL configured. Options: --target-url or env")
    print("DSS_URL_TARGET.")
    while True:
        try:
            raw = input("Target DSS URL [http://host:port]: ").strip()
        except EOFError:
            print("")
            raise Quit()
        if re.match(r"^https?://\S+$", raw):
            return raw.rstrip("/")


def main(argv=None):
    args = parse_args(argv)
    state = None
    try:
        api_key = read_api_key(args.api_key_file, "DKU_API_KEY",
                               "DSS API key (input hidden): ")
        root_url = resolve_base_url(args)
        client = DssClient(root_url + API_PREFIX, api_key,
                           insecure=args.insecure, timeout=args.timeout)

        # Connection check + banner (also validates the API key).
        projects, headers = client.call("GET", "/projects/")
        dss_version = headers.get("DSS-Version") or "?"
        print("")
        print("connected to source {} - DSS version {} - {} project(s) "
              "visible".format(root_url, dss_version, len(projects)))

        state = {"source_url": root_url, "target": None,
                 "last_run_code": EXIT_OK}

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

        spec = args.scenario
        while True:
            if spec is not None:
                scenarios, markers = scenario_rows(client, project_key)
                chosen, ambiguous = resolve_scenario(scenarios, spec)
                if ambiguous:
                    print("  '{}' matches {} scenarios by name - pick one by "
                          "id:".format(spec, len(ambiguous)))
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
            actions_loop(state, client, project_key, chosen, args)
    except Quit:
        print("bye.")
        # leaving after a completed run returns that run's own exit code,
        # not a blanket "clean exit" (as dss_scenariorun_console.py does)
        return (state or {}).get("last_run_code", EXIT_OK)
    except KeyboardInterrupt:
        print("\nbye.")
        return EXIT_OK
    except DssApiError as error:
        print("error: {}".format(error), file=sys.stderr)
        if getattr(error, "status", None) == 401:
            print("(the API key was rejected - check DKU_API_KEY / "
                  "DKU_API_KEY_TARGET)", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
