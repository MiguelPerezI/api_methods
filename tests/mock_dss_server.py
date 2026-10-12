#!/usr/bin/env python3
"""
mock_dss_server.py — minimal mock of the DSS public REST API (webapp +
code-env + scenario subsets) for offline testing of the scripts in this repo.

Serves, under /public/api, with Bearer auth (token: --token, default "testkey"):

    GET    /projects/
    GET    /projects/{pk}/webapps/
    GET    /projects/{pk}/webapps/{webappId}/backend/state
    PUT    /projects/{pk}/webapps/{webappId}/backend/actions/restart
    PUT    /projects/{pk}/webapps/{webappId}/backend/actions/stop

    GET    /admin/code-envs
    POST   /admin/code-envs/{envLang}/{envName}        create
    GET    /admin/code-envs/{envLang}/{envName}        get definition
    PUT    /admin/code-envs/{envLang}/{envName}        save definition (204)
    DELETE /admin/code-envs/{envLang}/{envName}        delete
    POST   /admin/code-envs/packages{?forceRebuildEnv} update/rebuild packages
    POST   /admin/code-envs/jupyter{?active}          jupyter integration

    GET    /projects/{pk}/scenarios/                   list scenarios
    POST   /projects/{pk}/scenarios/                   create scenario
    GET    /projects/{pk}/scenarios/{id}               full scenario object
    PUT    /projects/{pk}/scenarios/{id}               update settings (204)
    GET    /projects/{pk}/scenarios/{id}/payload       read the python script
    PUT    /projects/{pk}/scenarios/{id}/payload        set the python script
    POST   /projects/{pk}/scenarios/{id}/run            fire the manual trigger
    GET    /projects/{pk}/scenarios/{id}/get-last-runs{?limit&onlyFinishedRuns}
    GET    /projects/{pk}/scenarios/{id}/{runId}/log    run log (text/plain)
    DELETE /projects/{pk}/scenarios/{id}               delete scenario (its runs too)

    GET    /admin/users/                               list users
    GET    /admin/users/{login}                        get user
    PUT    /admin/users/{login}                        update user (200 {"msg"})
    GET    /admin/groups                               list groups

Simulated world:
    Webapps:
      EMPTY  "Empty project"   - no webapps (exercises the empty-project path)
      PROD   "Production"      - P1 "scoring_console" (BOKEH, down)
      TEST   "Test project"    - W1 "my_webapp"     (BOKEH, down)
                                  W2 "data_monitor"  (SHINY, up since ~1h)

    Code envs (all instance-level, owner "admin"):
      basic_external  PYTHON  EXTERNAL_CONDA_NAMED  canUpdateCodeEnv=False
                      (DSS itself cannot update it - exercises the guard)
      py3_core        PYTHON  DESIGN_MANAGED  spec pandas==2.1.4 + requests,
                      actual lacks requests -> out of date
      py_webapp       PYTHON  DESIGN_MANAGED  spec flask + openpyxl==3.1.2,
                      actual has only flask -> out of date
      r_stats         R       DESIGN_MANAGED  spec ggplot2, nothing installed

    Scenarios: every project starts with none; they are created through the
    API (ids like custom_python_1, or the "id" the create body requests -
    a duplicate requested id is a 400). A run executes the scenario's script with
    `sys.executable -c` once --run-delay seconds have passed (the run is
    finalised lazily on the next listing/log call): the log is the script's
    stdout (+ stderr), the outcome SUCCESS when it exits 0, FAILED otherwise
    (traceback lands in the log). get-last-runs is newest-first and a run is
    finished exactly when it carries a "result" object, like the real API.
    Create accepts the full documented scenario object and stores
    active/description/params/triggers/reporters; GET .../scenarios/{id}
    returns them (the full definition, for copy tools) and PUT replaces
    them (204). --legacy-scenarios makes both answer 404, exercising the
    copy console's old-DSS fallback.

    Users/groups (admin security subset): a seeded group pool (incl. an
    admin group and an LDAP-sourced one) and seeded users (incl. a
    disabled one and an LDAP one). PUT /admin/users/{login} merges the
    body over the stored record (login untouched) like a GET-round-trip,
    rejects unknown group names with 400, and answers 200 {"msg": ...}
    like real DSS.

    Self-restart simulation: when a run is fired for a scenario whose
    payload Popen-launches a restart (as the payload built by
    dss_proxyfix_scenario.py does), the payload is NOT executed - instead
    the run is recorded with a truncated log and no result (killed mid-run
    by the restart) and the mock answers every request with HTTP 503 for
    --restart-delay seconds (the outage while DSS is down), then serves
    normally again. Scripts that watch for "DSS went down and came back"
    can thus be tested end to end.

A webapp restart reports running only after --boot-delay seconds (default 4);
a code-env packages/rebuild call sleeps --build-delay seconds (default 2)
before installing, so waiting logic is exercised. isUptodate is computed from
the spec vs actual package lists. Unknown project/webapp/env -> 404 with a
DSS-style {"errorMessage": ...} body; wrong token -> 401. Every response
carries DSS-Version / DSS-API-Version headers like the real API.

Usage:
    python3 tests/mock_dss_server.py [--port 8765] [--token testkey]
                                      [--boot-delay 4] [--build-delay 2]
                                      [--run-delay 2] [--legacy-scenarios]
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer


def initial_state():
    return {
        "token": "testkey",
        "boot_delay": 4,
        "build_delay": 2,
        "run_delay": 2,
        "restart_delay": 3,
        "down_until": 0.0,
        "scenario_counter": 0,
        "trigger_counter": 0,
        "legacy_scenarios": False,
        "core_mandatory": "six==1.16.0\npython-dateutil==2.8.2\n",
        "projects": [
            {"key": "EMPTY", "name": "Empty project", "webapps": [],
             "scenarios": []},
            {"key": "PROD", "name": "Production", "webapps": [
                {"id": "P1", "name": "scoring_console", "type": "BOKEH",
                 "running": False, "ready_at": 0.0},
            ], "scenarios": []},
            {"key": "TEST", "name": "Test project", "webapps": [
                {"id": "W1", "name": "my_webapp", "type": "BOKEH",
                 "running": False, "ready_at": 0.0},
                {"id": "W2", "name": "data_monitor", "type": "SHINY",
                 "running": True, "ready_at": time.time() - 3600.0},
            ], "scenarios": []},
        ],
        "users": [
            {"login": "admin", "sourceType": "LOCAL",
             "displayName": "Administrator", "email": "admin@example.corp",
             "enabled": True, "userProfile": "DATA_SCIENTIST",
             "groups": ["administrators"], "codeAllowed": True},
            {"login": "bob.smith", "sourceType": "LOCAL",
             "displayName": "Bob Smith", "email": "",
             "enabled": True, "userProfile": "READER",
             "groups": [], "codeAllowed": False},
            {"login": "jane.doe", "sourceType": "LOCAL",
             "displayName": "Jane Doe", "email": "jane.doe@example.corp",
             "enabled": True, "userProfile": "DATA_ANALYST",
             "groups": ["analysts", "marketing"], "codeAllowed": False},
            {"login": "lea.extern", "sourceType": "LDAP",
             "displayName": "Lea Extern", "email": "lea.extern@example.corp",
             "enabled": True,
             "groups": ["ldap_imported"], "codeAllowed": False},
            {"login": "svc.etl", "sourceType": "LOCAL",
             "displayName": "ETL service account", "email": "",
             "enabled": False, "userProfile": "DATA_SCIENTIST",
             "groups": ["data_scientists"], "codeAllowed": True},
        ],
        "groups": [
            {"name": "administrators", "description": "DSS administrators",
             "admin": True, "sourceType": "LOCAL"},
            {"name": "analysts", "description": "Business analysts",
             "admin": False, "sourceType": "LOCAL"},
            {"name": "data_scientists", "description": "Data science team",
             "admin": False, "sourceType": "LOCAL"},
            {"name": "ldap_imported", "description": "Synced from LDAP",
             "admin": False, "sourceType": "LDAP"},
            {"name": "marketing", "description": "Marketing department",
             "admin": False, "sourceType": "LOCAL"},
        ],
        "code_envs": [
            {"name": "basic_external", "lang": "PYTHON",
             "deployment_mode": "EXTERNAL_CONDA_NAMED", "owner": "admin",
             "usable_by_all": True, "can_update": False, "install_core": False,
             "jupyter": False, "interpreter": None,
             "spec": "", "actual": "", "mandatory": ""},
            {"name": "py3_core", "lang": "PYTHON",
             "deployment_mode": "DESIGN_MANAGED", "owner": "admin",
             "usable_by_all": True, "can_update": True, "install_core": True,
             "jupyter": True, "interpreter": "PYTHON311",
             "spec": "pandas==2.1.4\nrequests\n",
             "actual": "certifi\npandas==2.1.4\n",
             "mandatory": "six==1.16.0\npython-dateutil==2.8.2\n"},
            {"name": "py_webapp", "lang": "PYTHON",
             "deployment_mode": "DESIGN_MANAGED", "owner": "admin",
             "usable_by_all": True, "can_update": True, "install_core": True,
             "jupyter": False, "interpreter": "PYTHON311",
             "spec": "flask\nopenpyxl==3.1.2\n",
             "actual": "flask\n",
             "mandatory": "six==1.16.0\npython-dateutil==2.8.2\n"},
            {"name": "r_stats", "lang": "R",
             "deployment_mode": "DESIGN_MANAGED", "owner": "admin",
             "usable_by_all": True, "can_update": True, "install_core": True,
             "jupyter": False, "interpreter": None,
             "spec": "ggplot2\n", "actual": "",
             "mandatory": ""},
        ],
    }


STATE = initial_state()


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def now_ms():
    return int(time.time() * 1000)


def find_project(key):
    for project in STATE["projects"]:
        if project["key"] == key:
            return project
    return None


def find_webapp(project, webapp_id):
    for webapp in project["webapps"]:
        if webapp["id"] == webapp_id:
            return webapp
    return None


def backend_running(webapp):
    return webapp["running"] and time.time() >= webapp["ready_at"]


def webapp_listing_item(webapp, project_key):
    return {
        "backendRunning": backend_running(webapp),
        "type": webapp["type"],
        "projectKey": project_key,
        "id": webapp["id"],
        "name": webapp["name"],
        "tags": [],
    }


def future_payload(webapp, has_result=False):
    return {
        "hasResult": has_result,
        "aborted": False,
        "alive": not has_result,
        "startTime": now_ms(),
        "runningTime": int(max(0.0, time.time() - webapp["ready_at"]) * 1000),
        "unknown": False,
        "jobId": "MOCKJOB-" + webapp["id"],
    }


# ---------------------------------------------------------------------------
# scenario helpers
# ---------------------------------------------------------------------------

def find_scenario(project, scenario_id):
    for scenario in project["scenarios"]:
        if scenario["id"] == scenario_id:
            return scenario
    return None


def find_user(login):
    for user in STATE["users"]:
        if user["login"] == login:
            return user
    return None


def find_run(scenario, run_id):
    for run in scenario["runs"]:
        if run["runId"] == run_id:
            return run
    return None


def finalize_run(run):
    """Lazily finish a run past its delay: execute the payload, build the log."""
    if run.get("restart") or "result" in run or time.time() < run["finishes_at"]:
        return run  # restart-sim runs are never finalised (backend died)
    try:
        proc = subprocess.run([sys.executable, "-c", run["script"]],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60)
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        outcome = "SUCCESS" if proc.returncode == 0 else "FAILED"
    except subprocess.TimeoutExpired:
        stdout, stderr, outcome = "", "payload did not finish within 60s", "FAILED"
    log = stdout
    if stderr:
        log += ("\n--- stderr ---\n" + stderr) if stdout else stderr
    run["log"] = log
    run["end"] = run["start"] + int(STATE["run_delay"] * 1000)
    run["result"] = {"outcome": outcome, "aborted": False}
    return run


def run_public(run):
    """A run dict as the real API returns it (internal keys stripped)."""
    return {key: run[key] for key in
            ("runId", "scenario", "trigger", "start", "end", "result")
            if key in run}


def scenario_listing_item(scenario, project_key):
    running = False
    for run in scenario["runs"]:
        finalize_run(run)
        if "result" not in run:
            running = True
    return {
        "id": scenario["id"],
        "name": scenario["name"],
        "type": scenario["type"],
        "active": scenario["active"],
        "running": running,
        "projectKey": project_key,
        "tags": [],
    }


# ---------------------------------------------------------------------------
# code env helpers
# ---------------------------------------------------------------------------

def parse_pkg_line(line):
    """'pandas==2.1.4' -> ('pandas', '2.1.4'); 'requests' -> ('requests', None)."""
    name, _, version = line.partition("==")
    return name.strip().lower(), (version.strip() or None)


def spec_satisfied(env):
    """True when every requested spec line is present in the actual list."""
    actual = dict(parse_pkg_line(line) for line in env["actual"].split("\n") if line.strip())
    for spec_line in env["spec"].split("\n"):
        if not spec_line.strip():
            continue
        name, version = parse_pkg_line(spec_line)
        if name not in actual:
            return False
        if version is not None and actual[name] != version:
            return False
    return True


def find_env(env_lang, env_name):
    lang = (env_lang or "").upper()
    for env in STATE["code_envs"]:
        if env["lang"] == lang and env["name"] == env_name:
            return env
    return None


def env_summary(env):
    prefix = "py-dku-venv-" if env["lang"] == "PYTHON" else "r-dku-"
    return {
        "envName": env["name"],
        "envLang": env["lang"],
        "kernelSpecName": prefix + env["name"],
        "canUpdateCodeEnv": env["can_update"],
        "owner": env["owner"],
        "isUptodate": spec_satisfied(env),
        "unknownKernelSpecStatus": False,
        "deploymentMode": env["deployment_mode"],
    }


def env_definition(env):
    summary = env_summary(env)
    return {
        "deploymentMode": env["deployment_mode"],
        "envName": env["name"],
        "envLang": env["lang"],
        "kernelSpecName": summary["kernelSpecName"],
        "owner": env["owner"],
        "permissions": [],
        "usableByAll": env["usable_by_all"],
        "canUpdateCodeEnv": env["can_update"],
        "specPackageList": env["spec"],
        "specCondaEnvironment": "",
        "actualCondaEnvironment": "",
        "mandatoryCondaEnvironment": "",
        "actualPackageList": env["actual"],
        "mandatoryPackageList": env["mandatory"],
        "desc": {
            "usableByAll": env["usable_by_all"],
            "owner": env["owner"],
            "permissions": [],
            "deploymentMode": env["deployment_mode"],
            "installCorePackages": env["install_core"],
            "installJupyterSupport": env["jupyter"],
            "conda": False,
            "pythonInterpreter": env.get("interpreter"),
            "yarnPythonBin": "",
            "basePackagesInstallMethod": "PRE_BUILT",
        },
    }


def messages_payload(ok, lines):
    return {
        "messages": {
            "anyMessage": bool(lines),
            "error": not ok,
            "messages": lines,
            "success": ok,
            "warning": not ok,
        }
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("DSS-Version", "15.0.0")
        self.send_header("DSS-API-Version", "15.0.0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, code):
        self.send_response(code)
        self.send_header("DSS-Version", "15.0.0")
        self.send_header("DSS-API-Version", "15.0.0")
        self.end_headers()

    def _send_text(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("DSS-Version", "15.0.0")
        self.send_header("DSS-API-Version", "15.0.0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if self.headers.get("Authorization") != "Bearer " + STATE["token"]:
            self._send(401, {"errorMessage": "Bad API key"})
            return False
        return True

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return None

    def _route(self, method, body):
        path, _, query_string = self.path.partition("?")
        query = urllib.parse.parse_qs(query_string)
        if time.time() < STATE["down_until"]:
            # simulated DSS restart outage: nothing answers
            self._send(503, {"errorMessage": "DSS is restarting (mock outage)"})
            return
        if not self._authorized():
            return

        # ------------------------------------------------------------------
        # projects / webapps
        # ------------------------------------------------------------------
        if path == "/public/api/projects/" and method == "GET":
            self._send(200, [{"projectKey": p["key"], "name": p["name"],
                              "tags": []} for p in STATE["projects"]])
            return

        match = re.match(r"^/public/api/projects/([^/]+)/webapps/$", path)
        if match and method == "GET":
            project = find_project(match.group(1))
            if project is None:
                self._send(404, {"errorMessage": "Project '{}' does not exist".format(match.group(1))})
                return
            self._send(200, [webapp_listing_item(w, project["key"])
                             for w in project["webapps"]])
            return

        match = re.match(r"^/public/api/projects/([^/]+)/webapps/([^/]+)/"
                        r"backend/(state|actions/restart|actions/stop)$", path)
        if match:
            project = find_project(match.group(1))
            if project is None:
                self._send(404, {"errorMessage": "Project '{}' does not exist".format(match.group(1))})
                return
            webapp = find_webapp(project, match.group(2))
            if webapp is None:
                self._send(404, {"errorMessage": "Webapp '{}' does not exist".format(match.group(2))})
                return
            project_key, webapp_id, action = match.group(1), match.group(2), match.group(3)

            if action == "state" and method == "GET":
                if backend_running(webapp):
                    self._send(200, {
                        "projectKey": project_key,
                        "webAppId": webapp_id,
                        "futureId": "MOCKFUT-" + webapp_id,
                        "futureInfo": dict(future_payload(webapp),
                                           jobDisplayName="Backend for Web app"),
                    })
                else:
                    self._send(200, {"projectKey": project_key,
                                     "webAppId": webapp_id})
                return
            if action == "actions/restart" and method == "PUT":
                webapp["running"] = True
                webapp["ready_at"] = time.time() + STATE["boot_delay"]
                self._send(200, future_payload(webapp))
                return
            if action == "actions/stop" and method == "PUT":
                webapp["running"] = False
                self._send(200, future_payload(webapp, has_result=True))
                return

        # ------------------------------------------------------------------
        # scenarios
        # ------------------------------------------------------------------
        match = re.match(r"^/public/api/projects/([^/]+)/scenarios/$", path)
        if match and method in ("GET", "POST"):
            project = find_project(match.group(1))
            if project is None:
                self._send(404, {"errorMessage": "Project '{}' does not exist".format(match.group(1))})
                return
            if method == "GET":
                self._send(200, [scenario_listing_item(s, project["key"])
                                 for s in project["scenarios"]])
            else:
                self.scenario_create(project, body)
            return

        match = re.match(r"^/public/api/projects/([^/]+)/scenarios/([^/]+)"
                        r"/(payload|run|get-last-runs)$", path)
        if match:
            self.scenario_route(method, match.group(1), match.group(2),
                                match.group(3), body, query)
            return

        match = re.match(r"^/public/api/projects/([^/]+)/scenarios/([^/]+)/"
                        r"([^/]+)/log$", path)
        if match and method == "GET":
            self.scenario_log(match.group(1), match.group(2), match.group(3))
            return

        match = re.match(r"^/public/api/projects/([^/]+)/scenarios/([^/]+)/?$",
                         path)
        if match and method in ("GET", "PUT", "DELETE"):
            self.scenario_one(method, match.group(1), match.group(2), body)
            return

        # ------------------------------------------------------------------
        # users / groups (admin security API)
        # ------------------------------------------------------------------
        if path == "/public/api/admin/users/" and method == "GET":
            self._send(200, [dict(user) for user in STATE["users"]])
            return

        match = re.match(r"^/public/api/admin/users/([^/]+)$", path)
        if match and method in ("GET", "PUT"):
            self.user_route(method, match.group(1), body)
            return

        if path == "/public/api/admin/groups" and method == "GET":
            self._send(200, [dict(group) for group in STATE["groups"]])
            return

        # ------------------------------------------------------------------
        # code envs (admin API) - action routes BEFORE {envLang}/{envName}
        # ------------------------------------------------------------------
        if path == "/public/api/admin/code-envs" and method == "GET":
            self._send(200, [env_summary(env) for env in STATE["code_envs"]])
            return

        if path == "/public/api/admin/code-envs/packages" and method == "POST":
            self.code_env_action(body, query)
            return

        if path == "/public/api/admin/code-envs/jupyter" and method == "POST":
            self.code_env_jupyter(body, query)
            return

        match = re.match(r"^/public/api/admin/code-envs/([^/]+)/([^/]+)$", path)
        if match:
            self.code_env_route(method, match.group(1), match.group(2), body)
            return

        self._send(404, {"errorMessage": "Unknown route {} {}".format(method, path)})

    def scenario_create(self, project, body):
        """POST /projects/{pk}/scenarios/ (custom_python or step_based).

        Accepts the full documented scenario object: a requested "id" is
        honored when free (a duplicate requested id is a 400), and
        active/description/params/triggers/reporters are stored."""
        if not isinstance(body, dict) or not body.get("name"):
            self._send(400, {"errorMessage": "Body must be a scenario object with a 'name'"})
            return
        scenario_type = body.get("type")
        if scenario_type not in ("step_based", "custom_python"):
            self._send(400, {"errorMessage": "Scenario 'type' must be 'step_based' or 'custom_python'"})
            return
        wanted_id = body.get("id")
        if wanted_id:
            if find_scenario(project, wanted_id) is not None:
                self._send(400, {"errorMessage": "Scenario '{}' already exists in project '{}'".format(
                    wanted_id, project["key"])})
                return
            scenario_id = wanted_id
        else:
            STATE["scenario_counter"] += 1
            scenario_id = "{}_{}".format(scenario_type, STATE["scenario_counter"])
        scenario = {
            "id": scenario_id,
            "name": body["name"],
            "type": scenario_type,
            "active": bool(body.get("active", True)),
            "description": body.get("description") or "",
            "params": body.get("params") if isinstance(body.get("params"), dict) else {},
            "triggers": list(body.get("triggers") or []),
            "reporters": list(body.get("reporters") or []),
            "script": "",
            "runs": [],
        }
        project["scenarios"].append(scenario)
        self._send(200, {"id": scenario["id"], "name": scenario["name"],
                         "type": scenario["type"], "projectKey": project["key"]})

    def scenario_one(self, method, project_key, scenario_id, body):
        """GET (full scenario) / PUT (settings, 204) / DELETE for one scenario.

        A trailing slash on the path is accepted either way, like real DSS.
        --legacy-scenarios makes GET and PUT answer 404 (an old DSS without
        those endpoints), so copy-tool fallbacks can be exercised."""
        project = find_project(project_key)
        if project is None:
            self._send(404, {"errorMessage": "Project '{}' does not exist".format(project_key)})
            return
        scenario = find_scenario(project, scenario_id)
        if scenario is None:
            self._send(404, {"errorMessage": "Scenario '{}' does not exist".format(scenario_id)})
            return
        if method == "GET":
            if STATE["legacy_scenarios"]:
                self._send(404, {"errorMessage": "Unknown route GET {}".format(self.path)})
                return
            self._send(200, {
                "id": scenario["id"],
                "name": scenario["name"],
                "type": scenario["type"],
                "active": scenario["active"],
                "description": scenario["description"],
                "params": scenario["params"],
                "triggers": scenario["triggers"],
                "reporters": scenario["reporters"],
            })
            return
        if method == "PUT":
            if STATE["legacy_scenarios"]:
                self._send(404, {"errorMessage": "Unknown route PUT {}".format(self.path)})
                return
            if not isinstance(body, dict):
                self._send(400, {"errorMessage": "Missing or invalid JSON body"})
                return
            if "type" in body and body["type"] not in ("step_based", "custom_python"):
                self._send(400, {"errorMessage": "Scenario 'type' must be 'step_based' or 'custom_python'"})
                return
            for field in ("name", "type", "active", "description", "params",
                          "triggers", "reporters"):
                if field in body:
                    scenario[field] = body[field]
            self._send_empty(204)  # documented success response for PUT
            return
        # DELETE — the run history goes with the scenario
        project["scenarios"].remove(scenario)
        self._send(200, {})

    def scenario_route(self, method, project_key, scenario_id, action, body, query):
        """payload GET/PUT · run POST · get-last-runs GET for one scenario."""
        project = find_project(project_key)
        if project is None:
            self._send(404, {"errorMessage": "Project '{}' does not exist".format(project_key)})
            return
        scenario = find_scenario(project, scenario_id)
        if scenario is None:
            self._send(404, {"errorMessage": "Scenario '{}' does not exist".format(scenario_id)})
            return
        if action == "payload" and method == "GET":
            self._send(200, {"script": scenario["script"]})
            return
        if action == "payload" and method == "PUT":
            if not isinstance(body, dict) or not isinstance(body.get("script"), str):
                self._send(400, {"errorMessage": "Body must be {'script': <python code>}"})
                return
            scenario["script"] = body["script"]
            self._send(200, {"script": scenario["script"]})
            return
        if action == "run" and method == "POST":
            STATE["trigger_counter"] += 1
            trigger_fire = {
                "runId": "trigrun-{}".format(STATE["trigger_counter"]),
                "trigger": {"id": "manual", "type": "manual_trigger",
                            "name": "Run now"},
                "cancelled": False,
            }
            run = {
                "runId": "{}-{:03d}".format(time.strftime("%Y-%m-%d-%H-%M-%S"),
                                            STATE["trigger_counter"]),
                "scenario": {"projectKey": project["key"], "id": scenario["id"]},
                "trigger": trigger_fire,
                "start": now_ms(),
                "finishes_at": time.time() + STATE["run_delay"],
                "script": scenario["script"],  # snapshot at fire time
            }
            if "Popen" in scenario["script"] and "restart" in scenario["script"]:
                # self-restart payload: the run is killed mid-flight by the
                # restart it launches and the mock goes dark for a while
                run["restart"] = True
                run["log"] = ("PROXYFIX-RESULT: line-removed, restart launched\n"
                              "(backend killed by the restart - log truncated)")
                STATE["down_until"] = time.time() + STATE["restart_delay"]
            scenario["runs"].insert(0, run)
            self._send(200, trigger_fire)
            return
        if action == "get-last-runs" and method == "GET":
            try:
                limit = int((query.get("limit") or ["10"])[0])
            except ValueError:
                limit = 10
            only_finished = (query.get("onlyFinishedRuns")
                             or ["false"])[0].lower() == "true"
            runs = []
            for run in scenario["runs"][:max(1, limit)]:
                finalize_run(run)
                if only_finished and "result" not in run:
                    continue
                runs.append(run_public(run))
            self._send(200, runs)
            return
        self._send(404, {"errorMessage": "Unknown route {} {}".format(method, self.path)})

    def scenario_log(self, project_key, scenario_id, run_id):
        """GET /projects/{pk}/scenarios/{id}/{runId}/log (text/plain)."""
        project = find_project(project_key)
        if project is None:
            self._send(404, {"errorMessage": "Project '{}' does not exist".format(project_key)})
            return
        scenario = find_scenario(project, scenario_id)
        if scenario is None:
            self._send(404, {"errorMessage": "Scenario '{}' does not exist".format(scenario_id)})
            return
        run = find_run(scenario, run_id)
        if run is None:
            self._send(404, {"errorMessage": "Run '{}' does not exist".format(run_id)})
            return
        finalize_run(run)
        # restart-sim runs carry a preset truncated log without a result
        text = run.get("log") if "log" in run else "(run still in progress)"
        self._send_text(200, text or "")

    def user_route(self, method, login, body):
        """GET one user / PUT user (GET-round-trip merge, unknown group 400)."""
        user = find_user(login)
        if user is None:
            self._send(404, {"errorMessage": "User '{}' does not exist".format(login)})
            return
        if method == "GET":
            self._send(200, dict(user))
            return
        if not isinstance(body, dict):
            self._send(400, {"errorMessage": "Missing or invalid JSON body"})
            return
        if "groups" in body:
            if not isinstance(body["groups"], list) or not all(
                    isinstance(g, str) for g in body["groups"]):
                self._send(400, {"errorMessage": "'groups' must be a list of group names"})
                return
            known = {g["name"] for g in STATE["groups"]}
            unknown = [g for g in body["groups"] if g not in known]
            if unknown:
                self._send(400, {"errorMessage": "Unknown group(s): {}".format(
                    ", ".join(unknown))})
                return
        if "userProfile" in body and not (isinstance(body["userProfile"], str)
                                          and body["userProfile"].strip()):
            self._send(400, {"errorMessage": "'userProfile' must be a non-empty string"})
            return
        for key, value in body.items():
            if key != "login":
                user[key] = value
        self._send(200, {"msg": "Edited user {}".format(login)})

    def code_env_route(self, method, env_lang, env_name, body):
        env = find_env(env_lang, env_name)
        if method == "GET":
            if env is None:
                self._send(404, {"errorMessage": "Code env '{}_{}' does not exist".format(
                    env_lang, env_name)})
            else:
                self._send(200, env_definition(env))
            return
        if method == "PUT":
            if env is None:
                self._send(404, {"errorMessage": "Code env '{}_{}' does not exist".format(
                    env_lang, env_name)})
                return
            if body is None:
                self._send(400, {"errorMessage": "Missing or invalid JSON body"})
                return
            # round-trip the GET object: keep only fields the mock stores
            if "specPackageList" in body:
                env["spec"] = body["specPackageList"] or ""
            desc = body.get("desc")
            if isinstance(desc, dict) and "installJupyterSupport" in desc:
                env["jupyter"] = bool(desc["installJupyterSupport"])
            self._send_empty(204)  # documented success response for PUT
            return
        if method == "POST":  # create
            if (env_lang or "").upper() not in ("PYTHON", "R"):
                self._send(400, {"errorMessage": "envLang must be 'python' or 'r'"})
                return
            if env is not None:
                self._send(400, {"errorMessage": "Code env '{}_{}' already exists".format(
                    env_lang, env_name)})
                return
            if body is None:
                self._send(400, {"errorMessage": "Missing or invalid JSON body"})
                return
            desc = body if isinstance(body, dict) else {}
            install_core = bool(desc.get("installCorePackages", True))
            mandatory = STATE["core_mandatory"] if install_core else ""
            STATE["code_envs"].append({
                "name": env_name,
                "lang": (env_lang or "").upper(),
                "deployment_mode": desc.get("deploymentMode", "DESIGN_MANAGED"),
                "owner": "admin",
                "usable_by_all": True,
                "can_update": True,
                "install_core": install_core,
                "jupyter": bool(desc.get("installJupyterSupport", False)),
                "interpreter": desc.get("pythonInterpreter"),
                "spec": "",
                "actual": mandatory,
                "mandatory": mandatory,
            })
            self._send(200, {
                "envName": env_name,
                "messages": {"anyMessage": False, "error": False, "messages": [],
                             "success": False, "warning": False},
            })
            return
        if method == "DELETE":
            if env is None:
                self._send(404, {"errorMessage": "Code env '{}_{}' does not exist".format(
                    env_lang, env_name)})
                return
            if not env["can_update"]:
                self._send(200, messages_payload(False, [
                    "Env '{}' is external - DSS cannot delete it".format(env_name)]))
                return
            STATE["code_envs"].remove(env)
            self._send(200, messages_payload(True,
                        ["Deleted code env '{}'".format(env_name)]))
            return

    def code_env_action(self, body, query):
        """POST /admin/code-envs/packages{?forceRebuildEnv} (sync install)."""
        if not isinstance(body, dict) or not body.get("envName"):
            self._send(400, {"errorMessage": "Body must be a code env object"})
            return
        env = find_env(body.get("envLang"), body["envName"])
        if env is None:
            self._send(404, {"errorMessage": "Code env '{}_{}' does not exist".format(
                body.get("envLang"), body["envName"])})
            return
        if not env["can_update"]:
            self._send(200, messages_payload(False, [
                "Env '{}' is external - DSS cannot update its packages".format(env["name"])]))
            return
        force = (query.get("forceRebuildEnv", ["false"])[0].lower() == "true")
        spec = body.get("specPackageList", env["spec"]) or ""
        time.sleep(STATE["build_delay"])  # synchronous install window
        if force:
            merged = []
            for line in (env["mandatory"] + spec).split("\n"):
                if line.strip() and line not in merged:
                    merged.append(line)
            env["actual"] = "\n".join(merged) + "\n"
            message = "Rebuilt env '{}' from the requested packages".format(env["name"])
        else:
            installed = [line for line in env["actual"].split("\n") if line.strip()]
            for line in spec.split("\n"):
                if line.strip() and line not in installed:
                    installed.append(line)
            env["actual"] = "\n".join(installed) + ("\n" if installed else "")
            message = "Updated packages of env '{}'".format(env["name"])
        self._send(200, messages_payload(True, [message]))

    def code_env_jupyter(self, body, query):
        """POST /admin/code-envs/jupyter{?active}."""
        active_values = query.get("active")
        if not active_values or active_values[0] not in ("true", "false"):
            self._send(400, {"errorMessage": "Missing or invalid 'active' query parameter"})
            return
        if not isinstance(body, dict) or not body.get("envName"):
            self._send(400, {"errorMessage": "Body must be a code env object"})
            return
        env = find_env(body.get("envLang"), body["envName"])
        if env is None:
            self._send(404, {"errorMessage": "Code env '{}_{}' does not exist".format(
                body.get("envLang"), body["envName"])})
            return
        if not env["can_update"]:
            self._send(200, messages_payload(False, [
                "Env '{}' is external - DSS cannot manage its Jupyter integration".format(
                    env["name"])]))
            return
        env["jupyter"] = active_values[0] == "true"
        self._send(200, messages_payload(True, [
            "Jupyter integration {}d for env '{}'".format(
                "activate" if env["jupyter"] else "deactivate", env["name"])]))

    def do_GET(self):
        self._route("GET", None)

    def do_PUT(self):
        self._route("PUT", self._read_body())

    def do_POST(self):
        self._route("POST", self._read_body())

    def do_DELETE(self):
        self._route("DELETE", self._read_body())


def main():
    parser = argparse.ArgumentParser(
        description="Mock DSS public API (webapp + code-env + scenario subsets)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default="testkey")
    parser.add_argument("--boot-delay", type=float, default=4,
                        help="seconds after restart before the mock backend reports running")
    parser.add_argument("--build-delay", type=float, default=2,
                        help="seconds a code-env packages/rebuild call blocks")
    parser.add_argument("--run-delay", type=float, default=2,
                        help="seconds before a scenario run is finalised "
                             "(payload executed for real)")
    parser.add_argument("--restart-delay", type=float, default=3,
                        help="seconds the mock stays dark (503) after a run "
                             "whose payload mentions '/bin/dss restart'")
    parser.add_argument("--legacy-scenarios", action="store_true",
                        help="answer 404 to GET/PUT /projects/{pk}/scenarios/"
                             "{id} (an old DSS without those endpoints), so "
                             "copy-tool fallbacks can be exercised")
    args = parser.parse_args()
    STATE["token"] = args.token
    STATE["boot_delay"] = args.boot_delay
    STATE["build_delay"] = args.build_delay
    STATE["run_delay"] = args.run_delay
    STATE["restart_delay"] = args.restart_delay
    STATE["legacy_scenarios"] = args.legacy_scenarios
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print("mock DSS on http://127.0.0.1:{} (token {!r}; projects EMPTY/PROD/TEST; "
          "code envs basic_external/py3_core/py_webapp/r_stats; scenarios "
          "created at runtime)".format(args.port, args.token))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()