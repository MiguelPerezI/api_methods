#!/usr/bin/env python3
"""
dss_webappctl.py — one-shot, scriptable control of a Dataiku DSS project
webapp **backend** via the public REST API: status / start / stop /
restart, with state pre-checks so start/stop are no-ops when the backend
is already in the wanted state, and a post-action wait that confirms the
backend reached it.

This is the non-interactive counterpart of dss_webapp_console.py (and the
generalization of dss_restart_webapp.py): same API mechanics, packaged
for runbooks, cron and CI — exit codes carry the outcome, no prompts, no
hidden state.

Target API: DSS public API 15 — https://doc.dataiku.com/dss/api/15/rest/

Endpoints used (all relative to {--url}/public/api):
    GET  /projects/{projectKey}/webapps/                                   READ_CONF
    GET  /projects/{projectKey}/webapps/{webappId}/backend/state            READ_CONF
    PUT  /projects/{projectKey}/webapps/{webappId}/backend/actions/restart  WRITE_CONF
    PUT  /projects/{projectKey}/webapps/{webappId}/backend/actions/stop     WRITE_CONF

The API key must hold READ_CONF + WRITE_CONF on the target project; a key
with admin privileges satisfies both.

Semantics (see dss_webapp_console.md for the research):
  - there is **no dedicated start endpoint** in the API: START is the
    restart action guarded by a state pre-check. The docs' restart is
    "launch the backend (stop+relaunch if it was running)", so from a
    down state restart == start.
  - backend/state answers {projectKey, webAppId} ONLY when the backend is
    down; an "futureId" means up — that asymmetry is the liveness signal.
  - start and stop are idempotent: already-up start and already-down stop
    print "nothing to do" and exit 0 without calling DSS actions.
  - restart always fires the restart action, whatever the current state.
  - after an action, the state is polled until the wanted state shows;
    hitting --wait-timeout only means "accepted but unconfirmed" (the
    action stands on DSS) — a warning, exit 0.

Exit codes:
    0  success (incl. no-op, and accepted-but-unconfirmed after timeout)
    1  API or other failure
    2  usage error (argparse default)
    3  project or webapp not found
    130 on Ctrl+C

Examples:
    DKU_API_KEY=... python3 dss_webappctl.py \
        --url http://dss-host:11000 --project MYPROJECT --webapp my_webapp \
        --action restart

    # idempotent start, don't wait for boot; report-only status:
    DKU_API_KEY=... python3 dss_webappctl.py \
        --url http://dss-host:11000 --project MYPROJECT --webapp W1 \
        --action start --no-wait
    DKU_API_KEY=... python3 dss_webappctl.py \
        --url http://dss-host:11000 --project MYPROJECT --webapp W1 \
        --action status

    # a maintenance-window stop of every backend:
    for w in $(list_webapps); do
        DKU_API_KEY=... python3 dss_webappctl.py --url ... \
            --project MYPROJECT --webapp "$w" --action stop
    done
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_PREFIX = "/public/api"
POLL_INTERVAL = 2  # seconds between backend-state polls after an action

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3


class DssApiError(Exception):
    """A call to the DSS public API failed."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="dss_webappctl.py",
        description="One-shot status / start / stop / restart of a Dataiku DSS "
                    "project webapp backend via the public REST API. Start and "
                    "stop are idempotent (state pre-check); the wanted state "
                    "is confirmed by polling afterwards unless --no-wait.",
        epilog="Auth: env DKU_API_KEY (or --api-key-file), never an argv flag. "
               "Needs READ_CONF + WRITE_CONF on the project (an admin key "
               "satisfies both). API docs: "
               "https://doc.dataiku.com/dss/api/15/rest/")
    parser.add_argument("--url", default=os.environ.get("DSS_URL"),
                        help="DSS base URL, e.g. http://dss-host:11000 (default: env DSS_URL)")
    parser.add_argument("--project", required=True,
                        help="DSS project key")
    parser.add_argument("--webapp", required=True,
                        help="webapp id or name (name is resolved via the list endpoint)")
    parser.add_argument("--action", required=True,
                        choices=("status", "start", "stop", "restart"),
                        help="status: report backend state only; start/stop: "
                             "idempotent (no-op when already in that state); "
                             "restart: always stop+relaunch (also starts a "
                             "down backend)")
    parser.add_argument("--api-key-file", metavar="FILE",
                        help="file holding the DSS API key (default: env DKU_API_KEY)")
    parser.add_argument("--no-wait", action="store_true",
                        help="do not poll for the wanted state after start/stop/"
                             "restart (status never waits)")
    parser.add_argument("--wait-timeout", type=int, default=60, metavar="SEC",
                        help="seconds to wait for the wanted state (default: 60)")
    parser.add_argument("--timeout", type=int, default=30, metavar="SEC",
                        help="per-call HTTP timeout in seconds (default: 30)")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS certificate verification (self-signed DSS)")
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("--url is required (or set env DSS_URL)")
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
    raise DssApiError("no API key found: set env DKU_API_KEY or pass --api-key-file")


def build_opener(insecure):
    context = ssl.create_default_context()
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))


def q(segment):
    """URL-quote one path segment."""
    return urllib.parse.quote(str(segment), safe="")


def api_call(opener, base_url, api_key, method, path, timeout):
    """One HTTP call to the DSS public API; returns the parsed JSON response."""
    headers = {"Authorization": "Bearer " + api_key}
    data = b"" if method in ("PUT", "POST") else None  # actions take no body
    request = urllib.request.Request(base_url + path, data=data, method=method,
                                     headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace").strip()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")
        try:
            message = json.loads(body).get("errorMessage", body)
        except ValueError:
            message = body
        raise DssApiError("HTTP {} {} {}: {}".format(
            err.code, method, path, str(message)[:500]), status=err.code) from None
    except urllib.error.URLError as err:
        raise DssApiError("cannot reach DSS ({} {}): {}".format(
            method, path, err.reason)) from None


def resolve_webapp(opener, base_url, api_key, project, ref, timeout):
    """Resolve --webapp (id or name) to a listing item via the list endpoint."""
    items = api_call(opener, base_url, api_key, "GET",
                     "/projects/{}/webapps/".format(q(project)), timeout)
    if not isinstance(items, list):
        raise DssApiError("unexpected response when listing webapps: {!r}".format(items))
    for item in items:  # an exact id match always wins
        if item.get("id") == ref:
            return item
    by_name = [item for item in items if item.get("name") == ref]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        raise DssApiError("webapp name {!r} matches several webapps: {}".format(
            ref, ", ".join("{} ({})".format(i.get("id"), i.get("type"))
                           for i in by_name)))
    available = ", ".join("{} '{}'".format(i.get("id"), i.get("name"))
                          for i in items[:15])
    if len(items) > 15:
        available += ", …"
    detail = ". available: " + available if available else ". project has no webapps"
    raise DssApiError("no webapp with id or name {!r} in project {!r}{}".format(
        ref, project, detail), status=404)


def backend_state(opener, base_url, api_key, project, webapp_id, timeout):
    return api_call(opener, base_url, api_key, "GET",
                    "/projects/{}/webapps/{}/backend/state".format(
                        q(project), q(webapp_id)), timeout)


def restart_backend(opener, base_url, api_key, project, webapp_id, timeout):
    return api_call(opener, base_url, api_key, "PUT",
                    "/projects/{}/webapps/{}/backend/actions/restart".format(
                        q(project), q(webapp_id)), timeout)


def stop_backend(opener, base_url, api_key, project, webapp_id, timeout):
    return api_call(opener, base_url, api_key, "PUT",
                    "/projects/{}/webapps/{}/backend/actions/stop".format(
                        q(project), q(webapp_id)), timeout)


def backend_up(state):
    """Down backends return only {projectKey, webAppId}; a futureId means up."""
    return "futureId" in state


def describe_state(state):
    info = state.get("futureInfo") or {}
    return "jobId={}, alive={}, runningTime={}ms".format(
        info.get("jobId"), info.get("alive"), info.get("runningTime"))


def report_state(state):
    if backend_up(state):
        print("backend RUNNING ({})".format(describe_state(state)))
    else:
        print("backend NOT running")


def wait_for_state(opener, base_url, api_key, project, webapp_id, want_up,
                   wait_timeout, timeout):
    """Poll backend/state until the wanted state shows. Returns EXIT_OK on
    confirmation; on timeout the action still stands on DSS — warn and
    return EXIT_OK anyway (accepted but unconfirmed)."""
    deadline = time.monotonic() + wait_timeout
    while True:
        time.sleep(POLL_INTERVAL)
        state = backend_state(opener, base_url, api_key, project, webapp_id,
                              timeout)
        up = backend_up(state)
        if up == want_up:
            if up:
                print("backend is up: {}".format(describe_state(state)))
            else:
                print("backend is down")
            return EXIT_OK
        if time.monotonic() >= deadline:
            print("warning: the action was accepted but the backend did not "
                  "confirm the wanted state within {}s; check DSS".format(
                      wait_timeout), file=sys.stderr)
            return EXIT_OK


def main(argv=None):
    args = parse_args(argv)
    try:
        api_key = read_api_key(args.api_key_file)
        base_url = args.url.rstrip("/") + API_PREFIX
        opener = build_opener(args.insecure)

        webapp = resolve_webapp(opener, base_url, api_key, args.project,
                                args.webapp, args.timeout)
        webapp_id = webapp.get("id")
        print("project {} → webapp '{}' (id {}, type {}), backendRunning={}".format(
            args.project, webapp.get("name"), webapp_id, webapp.get("type"),
            webapp.get("backendRunning")))

        state = backend_state(opener, base_url, api_key, args.project,
                              webapp_id, args.timeout)
        up = backend_up(state)

        if args.action == "status":
            report_state(state)
            return EXIT_OK

        if args.action == "start":
            if up:
                print("already running ({}) - nothing to do".format(
                    describe_state(state)))
                return EXIT_OK
        if args.action == "stop":
            if not up:
                print("already stopped - nothing to do")
                return EXIT_OK

        if args.action in ("start", "restart"):
            # no dedicated start endpoint: restart launches a down backend
            future = restart_backend(opener, base_url, api_key, args.project,
                                     webapp_id, args.timeout)
            print("{} accepted: jobId={}, alive={}".format(
                args.action, future.get("jobId"), future.get("alive")))
            if future.get("alive") is False:
                print("warning: DSS reports the backend future as not alive "
                      "right after {}".format(args.action), file=sys.stderr)
            if args.no_wait:
                return EXIT_OK
            return wait_for_state(opener, base_url, api_key, args.project,
                                  webapp_id, True, args.wait_timeout,
                                  args.timeout)

        # action == "stop"
        future = stop_backend(opener, base_url, api_key, args.project,
                              webapp_id, args.timeout)
        print("stop accepted: jobId={}, hasResult={}".format(
            future.get("jobId"), future.get("hasResult")))
        if args.no_wait:
            return EXIT_OK
        return wait_for_state(opener, base_url, api_key, args.project,
                              webapp_id, False, args.wait_timeout,
                              args.timeout)
    except DssApiError as error:
        print("error: {}".format(error), file=sys.stderr)
        return EXIT_NOT_FOUND if getattr(error, "status", None) == 404 else EXIT_FAIL


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
