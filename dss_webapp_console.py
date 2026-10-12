#!/usr/bin/env python3
"""
dss_webapp_console.py — interactive terminal UI for managing the webapp
backends of a Dataiku DSS instance through the public REST API.

Flow: connect -> pick a project -> pick a webapp -> status / start / stop /
restart, then navigate back out. Plain numbered menus, no curses: works over
any SSH session.

Target API: DSS public API 15 — https://doc.dataiku.com/dss/api/15/rest/

Endpoints used (relative to {dss-url}/public/api):
    GET  /projects/                                                  READ_CONF
    GET  /projects/{projectKey}/webapps/                             READ_CONF
    GET  /projects/{projectKey}/webapps/{webappId}/backend/state     READ_CONF
    PUT  /projects/{projectKey}/webapps/{webappId}/backend/actions/restart  WRITE_CONF
    PUT  /projects/{projectKey}/webapps/{webappId}/backend/actions/stop     WRITE_CONF

There is no dedicated "start" endpoint in the API: START here uses the
restart action, which per the docs launches the backend when it is not
running ("If the backend was running, stop it and relaunch it").

Connection, in order of precedence:
    1. --url http://host:port
    2. env DSS_URL
    3. the CONNECTION DEFAULTS block below (edit by hand for your host/port)
    4. if none of the above: the console asks for the URL at startup

API key, in order of precedence:
    1. env DKU_API_KEY
    2. --api-key-file FILE
    3. hidden prompt (last resort — never a --api-key flag, it would leak
       into `ps` output and shell history)

Examples:
    DKU_API_KEY=... python3 dss_webapp_console.py
    DKU_API_KEY=... python3 dss_webapp_console.py --url http://dss-host:11000
    python3 dss_webapp_console.py --url https://dss-host:11443 --insecure
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

PLACEHOLDER_HOST = "dss-host"  # while unchanged, the console asks for the URL
API_PREFIX = "/public/api"
POLL_INTERVAL = 2          # seconds between backend-state polls (start/restart)

EXIT_OK = 0
EXIT_FAIL = 1

ACTION_MENU = [
    ("status", "show backend state"),
    ("start", "start the backend (no-op if already running)"),
    ("stop", "stop the backend (no-op if already stopped)"),
    ("restart", "stop and relaunch the backend"),
]


class Quit(Exception):
    """Raised to leave the console cleanly (q, EOF, Ctrl+C)."""


class DssApiError(Exception):
    """A call to the DSS public API failed."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class DssClient(object):
    """Thin stdlib HTTP client for the webapp subset of the DSS public API."""

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

    def call(self, method, path):
        """One API call; returns (parsed_json, response_headers)."""
        headers = {"Authorization": "Bearer " + self.api_key}
        data = b"" if method in ("PUT", "POST") else None  # actions take no body
        request = urllib.request.Request(self.base_url + path, data=data,
                                         method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace").strip()
                payload = json.loads(raw) if raw else {}
                return payload, response.headers
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")
            try:
                message = json.loads(body).get("errorMessage", body)
            except ValueError:
                message = body
            raise DssApiError("HTTP {} {} {}: {}".format(
                err.code, method, path, str(message)[:500]),
                status=err.code) from None
        except urllib.error.URLError as err:
            raise DssApiError("cannot reach DSS ({} {}): {}".format(
                method, path, err.reason)) from None

    def list_projects(self):
        payload, _ = self.call("GET", "/projects/")
        return payload

    def list_webapps(self, project_key):
        payload, _ = self.call("GET", "/projects/{}/webapps/".format(
            q(project_key)))
        return payload

    def backend_state(self, project_key, webapp_id):
        payload, _ = self.call("GET", "/projects/{}/webapps/{}/backend/state".format(
            q(project_key), q(webapp_id)))
        return payload

    def restart_backend(self, project_key, webapp_id):
        payload, _ = self.call("PUT", "/projects/{}/webapps/{}/backend/"
                                "actions/restart".format(q(project_key), q(webapp_id)))
        return payload

    def stop_backend(self, project_key, webapp_id):
        payload, _ = self.call("PUT", "/projects/{}/webapps/{}/backend/"
                                "actions/stop".format(q(project_key), q(webapp_id)))
        return payload


def q(segment):
    """URL-quote one path segment."""
    return urllib.parse.quote(str(segment), safe="")


def backend_up(state):
    """Down backends return only {projectKey, webAppId}; a futureId means up."""
    return bool(state) and "futureId" in state


# ---------------------------------------------------------------------------
# terminal UI helpers
# ---------------------------------------------------------------------------

def screen(title):
    print("")
    print("-" * 64)
    print(title)
    print("-" * 64)


def ask(count, extras=()):
    """Numbered-choice prompt. Returns a 0-based index or an extras key.
    Raises Quit on q/quit, EOF. Ctrl+C is handled by main()."""
    parts = []
    if count:
        parts.append("[1-{}]".format(count))
    parts.extend(extras)
    parts.append("q")
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
        try:
            number = int(raw)
        except ValueError:
            number = None
        if number is not None and 1 <= number <= count:
            return number - 1
        print("  ? invalid choice, try again")


def fmt_ms(ms):
    if ms >= 1000:
        return "{:.1f}s".format(ms / 1000.0)
    return "{}ms".format(ms)


def describe_state(state):
    if state is None:
        return "UNKNOWN (last fetch failed)"
    if backend_up(state):
        info = state.get("futureInfo") or {}
        return "RUNNING (jobId={}, alive={}, uptime {})".format(
            info.get("jobId"), info.get("alive"),
            fmt_ms(info.get("runningTime") or 0))
    return "STOPPED"


def print_state_detail(state):
    if state is None:
        print("  state unavailable - see the error above")
        return
    if backend_up(state):
        info = state.get("futureInfo") or {}
        print("  backend RUNNING")
        print("    jobId  : {}".format(info.get("jobId")))
        print("    alive  : {}".format(info.get("alive")))
        print("    uptime : {}".format(fmt_ms(info.get("runningTime") or 0)))
        if info.get("jobDisplayName"):
            print("    job    : {}".format(info.get("jobDisplayName")))
    else:
        print("  backend STOPPED (no future registered for this webapp)")


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------

def pick_project(client):
    """Project picker; returns a project dict, or None to signal nothing."""
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


def pick_webapp(client, project):
    """Webapp picker; returns a webapp dict, or None to go back to projects."""
    project_key = project.get("projectKey", "?")
    while True:
        error = None
        try:
            webapps = client.list_webapps(project_key)
        except DssApiError as err:
            webapps, error = [], str(err)
        webapps = sorted(webapps,
                         key=lambda w: str(w.get("name", "")).lower())
        screen("WEBAPPS in project {} ({})".format(project_key,
                                                   project.get("name") or ""))
        if error:
            print("  error: {}".format(error))
        elif not webapps:
            print("  (no webapps in this project)")
        for i, webapp in enumerate(webapps, 1):
            marker = "up" if webapp.get("backendRunning") else "down"
            print("  {:2}) {:24} {:8} [{}]".format(
                i, webapp.get("name") or webapp.get("id"),
                webapp.get("type") or "?", marker))
        choice = ask(len(webapps), ("r", "b"))
        if choice == "r":
            continue
        if choice == "b":
            return None
        return webapps[choice]


def webapp_screen(client, project_key, webapp, wait_timeout):
    """Action menu for one webapp; returns 'webapps' or 'projects'."""
    webapp_id = webapp.get("id")
    while True:
        error = None
        try:
            state = client.backend_state(project_key, webapp_id)
        except DssApiError as err:
            state, error = None, str(err)
        screen("WEBAPP '{}' - id {} - type {} - project {}".format(
            webapp.get("name"), webapp_id, webapp.get("type"), project_key))
        if error:
            print("  error: {}".format(error))
        else:
            print("  backend: {}".format(describe_state(state)))
        for i, (name, description) in enumerate(ACTION_MENU, 1):
            print("  {:2}) {:8} {}".format(i, name, description))
        choice = ask(len(ACTION_MENU), ("b", "p"))
        if choice == "b":
            return "webapps"
        if choice == "p":
            return "projects"
        action = ACTION_MENU[choice][0]
        if action == "status":
            print_state_detail(state)
        elif action == "start":
            act_start(client, project_key, webapp_id, wait_timeout)
        elif action == "stop":
            act_stop(client, project_key, webapp_id, wait_timeout)
        elif action == "restart":
            act_restart(client, project_key, webapp_id, wait_timeout)


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

def poll_backend(client, project_key, webapp_id, want_up, timeout, interval):
    """Poll backend/state until it matches want_up; False on timeout/abort."""
    deadline = time.monotonic() + timeout
    try:
        while True:
            time.sleep(interval)
            state = client.backend_state(project_key, webapp_id)
            if backend_up(state) == want_up:
                return True
            if time.monotonic() >= deadline:
                return False
    except KeyboardInterrupt:
        print("\n  (wait interrupted - the request itself stands)")
        return False


def confirm_transition(client, project_key, webapp_id, want_up, timeout,
                        interval=POLL_INTERVAL):
    label = "come up" if want_up else "stop"
    print("  waiting for the backend to {} (Ctrl+C skips the wait)...".format(label))
    if poll_backend(client, project_key, webapp_id, want_up, timeout, interval):
        state = client.backend_state(project_key, webapp_id)
        print("  confirmed: {}".format(describe_state(state)))
    else:
        print("  WARNING: backend did not {} within {}s - the request stands, "
              "check DSS".format(label, timeout))


def act_start(client, project_key, webapp_id, wait_timeout):
    try:
        if backend_up(client.backend_state(project_key, webapp_id)):
            print("  backend is already running - use 'restart' to relaunch it")
            return
        future = client.restart_backend(project_key, webapp_id)
        print("  start accepted (via the restart action): jobId={}".format(
            future.get("jobId")))
    except DssApiError as err:
        print("  error: {}".format(err))
        return
    confirm_transition(client, project_key, webapp_id, want_up=True,
                       timeout=wait_timeout)


def act_stop(client, project_key, webapp_id, wait_timeout):
    try:
        if not backend_up(client.backend_state(project_key, webapp_id)):
            print("  backend is already stopped")
            return
        client.stop_backend(project_key, webapp_id)
        print("  stop requested")
    except DssApiError as err:
        print("  error: {}".format(err))
        return
    confirm_transition(client, project_key, webapp_id, want_up=False,
                       timeout=min(wait_timeout, 30), interval=1)


def act_restart(client, project_key, webapp_id, wait_timeout):
    try:
        future = client.restart_backend(project_key, webapp_id)
        print("  restart accepted: jobId={}".format(future.get("jobId")))
    except DssApiError as err:
        print("  error: {}".format(err))
        return
    confirm_transition(client, project_key, webapp_id, want_up=True,
                       timeout=wait_timeout)


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="dss_webapp_console.py",
        description="Interactive terminal UI to manage DSS webapp backends "
                    "(status / start / stop / restart) via the public REST API.",
        epilog="Connection: --url, env DSS_URL, or the CONNECTION DEFAULTS at "
               "the top of this file; the console prompts for the URL if none "
               "is set. Auth: env DKU_API_KEY / --api-key-file / hidden prompt. "
               "API docs: https://doc.dataiku.com/dss/api/15/rest/")
    parser.add_argument("--url",
                        help="DSS base URL, e.g. http://dss-host:11000 "
                             "(default: env DSS_URL, then built-in defaults)")
    parser.add_argument("--api-key-file", metavar="FILE",
                        help="file holding the DSS API key (default: env "
                             "DKU_API_KEY, then a hidden prompt)")
    parser.add_argument("--wait-timeout", type=int, default=60, metavar="SEC",
                        help="seconds to wait for a backend start/stop to "
                             "confirm (default: 60)")
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
        print("navigate: numbers choose | r refresh | b back | p project list | q quit")

        while True:  # project list level
            project = pick_project(client)
            while True:  # webapp list level
                webapp = pick_webapp(client, project)
                if webapp is None:
                    break  # back -> project list
                result = webapp_screen(client, project.get("projectKey"),
                                       webapp, args.wait_timeout)
                if result == "projects":
                    break
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