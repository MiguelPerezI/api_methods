#!/usr/bin/env python3
"""
dss_codeenv_console.py — interactive terminal UI for managing the code
environments of a Dataiku DSS instance through the public REST API.

Flow: connect -> list code envs -> pick one -> status / packages / save /
update / rebuild / jupyter / delete, plus 'n' to create a new env from the
list. Plain numbered menus, no curses: works over any SSH session.

Target API: DSS public API 15 — https://doc.dataiku.com/dss/api/15/rest/

Endpoints used (relative to {dss-url}/public/api) — ALL require an
ADMIN-privileged API key (every code-env endpoint in the API does):
    GET    /admin/code-envs                                   list code envs
    POST   /admin/code-envs/{envLang}/{envName}               create code env
    GET    /admin/code-envs/{envLang}/{envName}               get code env
    PUT    /admin/code-envs/{envLang}/{envName}               update code env
    DELETE /admin/code-envs/{envLang}/{envName}               delete code env
    POST   /admin/code-envs/packages?forceRebuildEnv=<bool>   update packages
    POST   /admin/code-envs/jupyter?active=<bool>             jupyter integration

API facts that shaped the design:

- There is NO dedicated rebuild endpoint and no async future here: the
  packages action IS the installer.  POST .../packages runs "update
  dependencies" (install requested packages into the existing env) and
  POST .../packages?forceRebuildEnv=true forces a full rebuild.  Both calls
  are SYNCHRONOUS — on a real DSS they block for as long as pip/conda runs,
  so the default per-call HTTP timeout here is 300 s (see --timeout).
- The 'packages' and 'jupyter' actions take no env name in the path: the
  request body must identify the env, so the console sends the Code env
  object obtained from a fresh GET (never the locally-edited copy — unsaved
  edits reach the server only through the explicit 'save' action).
- Package lists (specPackageList = requested, actualPackageList =
  installed, mandatoryPackageList = DSS core) are newline-separated plain
  strings, not structured lists — the editor works on lines like
  'pandas==2.1.4'.
- PUT requires the exact object obtained from a previous GET (documented
  warning about undocumented attributes) — the console round-trips it.

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
    DKU_API_KEY=... python3 dss_codeenv_console.py
    DKU_API_KEY=... python3 dss_codeenv_console.py --url http://dss-host:11000
    python3 dss_codeenv_console.py --url https://dss-host:11443 --insecure
"""

import argparse
import getpass
import json
import os
import re
import ssl
import sys
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
DEFAULT_CALL_TIMEOUT = 300  # per-call HTTP timeout default (update/rebuild are
                            # synchronous and can run for minutes on real DSS)

EXIT_OK = 0
EXIT_FAIL = 1

ENV_MENU = [
    ("status", "show the definition (requested vs installed packages)"),
    ("packages", "edit the requested package list (kept in memory until saved)"),
    ("save", "save the definition on DSS (PUT)"),
    ("update", "install the requested packages into the env (no full rebuild)"),
    ("rebuild", "force a full rebuild of the env from the requested packages"),
    ("jupyter", "activate / deactivate the Jupyter notebook integration"),
    ("delete", "delete this code env (type the name to confirm)"),
]


class Quit(Exception):
    """Raised to leave the console cleanly (q, EOF, Ctrl+C)."""


class DssApiError(Exception):
    """A call to the DSS public API failed."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class DssClient(object):
    """Thin stdlib HTTP client for the code-env subset of the DSS public API."""

    def __init__(self, base_url, api_key, insecure=False, timeout=DEFAULT_CALL_TIMEOUT):
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
        """One API call; returns (parsed_json, response_headers)."""
        headers = {"Authorization": "Bearer " + self.api_key}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif method in ("PUT", "POST"):
            data = b""  # action calls without a body
        else:
            data = None
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

    # --- endpoints -------------------------------------------------------

    def list_code_envs(self):
        payload, _ = self.call("GET", "/admin/code-envs")
        return payload

    def get_code_env(self, env_lang, env_name):
        payload, _ = self.call("GET", "/admin/code-envs/{}/{}".format(
            q(env_lang), q(env_name)))
        return payload

    def update_code_env(self, env_lang, env_name, definition):
        # 204 No Content on success (docs); call() returns {} for empty bodies
        payload, _ = self.call("PUT", "/admin/code-envs/{}/{}".format(
            q(env_lang), q(env_name)), body=definition)
        return payload

    def create_code_env(self, env_lang, env_name, body):
        payload, _ = self.call("POST", "/admin/code-envs/{}/{}".format(
            q(env_lang), q(env_name)), body=body)
        return payload

    def delete_code_env(self, env_lang, env_name):
        payload, _ = self.call("DELETE", "/admin/code-envs/{}/{}".format(
            q(env_lang), q(env_name)))
        return payload

    def apply_packages(self, definition, force_rebuild):
        """Update code-env packages: forceRebuildEnv=true forces a full rebuild."""
        flag = "true" if force_rebuild else "false"
        payload, _ = self.call("POST", "/admin/code-envs/packages?forceRebuildEnv=" + flag,
                                body=definition)
        return payload

    def set_jupyter(self, definition, active):
        flag = "true" if active else "false"
        payload, _ = self.call("POST", "/admin/code-envs/jupyter?active=" + flag,
                               body=definition)
        return payload


def q(segment):
    """URL-quote one path segment."""
    return urllib.parse.quote(str(segment), safe="")


# ---------------------------------------------------------------------------
# code-env helpers
# ---------------------------------------------------------------------------

def package_lines(text):
    """Package lists are newline-separated strings; return the stripped lines."""
    return [line.strip() for line in (text or "").split("\n") if line.strip()]


def show_messages(payload):
    """DSS actions answer with a {messages: {...}} object; display it."""
    messages = (payload or {}).get("messages")
    if not isinstance(messages, dict):
        return  # nothing reported
    items = messages.get("messages") or []
    if not messages.get("anyMessage") or not items:
        print("  no messages reported by DSS")
        return
    for line in items:
        print("  - {}".format(line))
    if messages.get("error"):
        print("  (DSS reported ERRORS — check the messages above)")
    elif messages.get("success"):
        print("  (success)")


def env_lang_of(summary):
    """envLang for path calls (docs: 'python or r', lowercase)."""
    return str(summary.get("envLang") or "python").lower()


def up_to_date_marker(summary):
    if summary.get("isUptodate") is False:
        return " [out-of-date]"
    if summary.get("isUptodate") is True:
        return " [up-to-date]"
    return ""


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


def confirm(question):
    """y/N question; returns True only on an explicit yes."""
    try:
        raw = input("  {} [y/N]: ".format(question)).strip().lower()
    except EOFError:
        print("")
        raise Quit()
    return raw in ("y", "yes")


def prompt_text(label, default=None):
    """Free-text input; default returned on empty input."""
    suffix = " [{}]: ".format(default) if default else ": "
    try:
        raw = input("  " + label + suffix).strip()
    except EOFError:
        print("")
        raise Quit()
    if not raw and default is not None:
        return default
    return raw


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------

def pick_env(client):
    """Code env picker; returns a list-item summary dict."""
    while True:
        error = None
        try:
            envs = client.list_code_envs()
        except DssApiError as err:
            envs, error = [], str(err)
        envs = sorted(envs, key=lambda e: (env_lang_of(e),
                                          str(e.get("envName", "")).lower()))
        screen("CODE ENVS ({} on this DSS)".format(len(envs)))
        if error:
            print("  error: {}".format(error))
        elif not envs:
            print("  (no code envs visible - is the API key admin-privileged?)")
        for i, env in enumerate(envs, 1):
            markers = up_to_date_marker(env)
            if env.get("canUpdateCodeEnv") is False:
                markers += " [cannot-update]"
            print("  {:2}) {:24} {:8} {}{}".format(
                i, env.get("envName") or "?", env_lang_of(env),
                env.get("deploymentMode") or "", markers))
        choice = ask(len(envs), ("r", "n"))
        if choice == "r":
            continue
        if choice == "n":
            create_env(client)
            continue
        return envs[choice]


def create_env(client):
    """Create a new DESIGN_MANAGED code env (POST /admin/code-envs/{lang}/{name})."""
    screen("CREATE A NEW CODE ENV")
    name = prompt_text("env name (letters/digits/._- )")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name or ""):
        print("  '{}' is not a valid env name - nothing created".format(name))
        return
    print("  language:")
    print("    1) python")
    print("    2) r")
    choice = ask(2, ("b",))
    if choice == "b":
        print("  aborted")
        return
    env_lang = ("python", "r")[choice]
    body = {
        "deploymentMode": "DESIGN_MANAGED",
        "installCorePackages": True,
        "installJupyterSupport": True,
    }
    if env_lang == "python":
        interpreter = prompt_text("python interpreter", default="PYTHON3")
        if not interpreter:
            print("  aborted")
            return
        body["pythonInterpreter"] = interpreter.upper()
    print("  creating env '{}' (lang={}, deploymentMode=DESIGN_MANAGED,".format(
        name, env_lang))
    print("  installCorePackages=True, installJupyterSupport=True{}) ...".format(
        ", pythonInterpreter={}".format(body["pythonInterpreter"])
        if "pythonInterpreter" in body else ""))
    if not confirm("create it?"):
        print("  aborted")
        return
    try:
        payload = client.create_code_env(env_lang, name, body)
    except DssApiError as err:
        print("  error: {}".format(err))
        return
    print("  created: {}".format(payload.get("envName") or name))
    show_messages(payload)


def env_screen(client, summary, args):
    """Action menu for one code env; returns when going back to the list."""
    env_name = summary.get("envName")
    env_lang = env_lang_of(summary)

    session = {"summary": summary, "definition": None, "original_spec": None}
    if not fetch_definition(client, session, env_lang, env_name):
        return  # could not even read it - back to the list

    while True:
        env = session["definition"]
        spec = package_lines(env.get("specPackageList"))
        dirty = spec != package_lines(session["original_spec"])
        title = "CODE ENV '{}' - lang {} - {}".format(
            env_name, env_lang, env.get("deploymentMode") or "?")
        if dirty:
            title += "  *unsaved edits*"
        screen(title)
        print("  requested: {} package(s) | installed: {} | mandatory: {}{}".format(
            len(spec), len(package_lines(env.get("actualPackageList"))),
            len(package_lines(env.get("mandatoryPackageList"))),
            up_to_date_marker(session["summary"])))
        if session["summary"].get("canUpdateCodeEnv") is False:
            print("  (DSS cannot update this env itself - external/managed elsewhere)")
        for i, (name, description) in enumerate(ENV_MENU, 1):
            print("  {:2}) {:9} {}".format(i, name, description))
        choice = ask(len(ENV_MENU), ("b",))
        if choice == "b":
            return
        action = ENV_MENU[choice][0]
        if action == "status":
            print_definition(session)
        elif action == "packages":
            edit_packages(session, env_name)
        elif action == "save":
            act_save(client, session, env_lang, env_name)
        elif action == "update":
            act_apply(client, session, env_lang, env_name,
                      force_rebuild=False, timeout=args.timeout)
        elif action == "rebuild":
            act_apply(client, session, env_lang, env_name,
                      force_rebuild=True, timeout=args.timeout)
        elif action == "jupyter":
            act_jupyter(client, session, env_lang, env_name)
        elif action == "delete":
            if act_delete(client, session, env_lang, env_name):
                return  # deleted - back to the list


def fetch_definition(client, session, env_lang, env_name):
    """(Re)load the definition into session; False on failure."""
    try:
        session["definition"] = client.get_code_env(env_lang, env_name)
        session["original_spec"] = session["definition"].get("specPackageList")
        return True
    except DssApiError as err:
        print("  error: {}".format(err))
        return False


def refresh_summary(client, session, env_lang, env_name):
    """Update the isUptodate etc. summary from a fresh list call (best effort)."""
    try:
        for env in client.list_code_envs():
            if env.get("envName") == env_name and env_lang_of(env) == env_lang:
                session["summary"] = env
                return
    except DssApiError:
        pass  # keep the old summary


def print_definition(session):
    """The status view: identity, flags, requested vs installed packages."""
    env = session["definition"]
    summary = session["summary"]
    desc = env.get("desc") or {}
    print("  envName           : {}".format(env.get("envName")))
    print("  envLang           : {}".format(env.get("envLang") or env_lang_of(summary)))
    print("  deploymentMode    : {}".format(env.get("deploymentMode")))
    print("  owner             : {}   usableByAll: {}".format(
        env.get("owner"), env.get("usableByAll")))
    if desc.get("pythonInterpreter"):
        print("  pythonInterpreter : {}".format(desc.get("pythonInterpreter")))
    print("  core packages     : installCorePackages={}".format(
        desc.get("installCorePackages")))
    print("  jupyter support   : {}".format(desc.get("installJupyterSupport")))
    if env.get("kernelSpecName"):
        print("  kernelSpecName    : {}".format(env.get("kernelSpecName")))
    spec = package_lines(env.get("specPackageList"))
    print("  requested packages (specPackageList) - {} line(s):".format(len(spec)))
    for line in spec:
        print("    - {}".format(line))
    if not spec:
        print("    (none requested)")
    print("  installed (actualPackageList)  : {} package(s)".format(
        len(package_lines(env.get("actualPackageList")))))
    print("  mandatory (mandatoryPackageList): {} package(s)".format(
        len(package_lines(env.get("mandatoryPackageList")))))


def edit_packages(session, env_name):
    """In-memory editor for specPackageList (newline-separated string)."""
    definition = session["definition"]
    lines = package_lines(definition.get("specPackageList"))
    while True:
        screen("REQUESTED PACKAGES of env '{}' - edits stay in memory until 'save'".format(
            env_name))
        if not lines:
            print("  (empty - no packages requested)")
        for i, line in enumerate(lines, 1):
            print("  {:2}) {}".format(i, line))
        try:
            raw = input("command [a add | d N delete line N | b back]: ").strip().lower()
        except EOFError:
            print("")
            raise Quit()
        if raw in ("b", "back", ""):
            break
        if raw == "a":
            spec = prompt_text("package spec (e.g. 'pandas' or 'pandas==2.1.4')")
            if not spec:
                print("  ? empty - nothing added")
                continue
            if re.search(r"\s", spec):
                print("  ? invalid spec (no whitespace allowed) - nothing added")
                continue
            if spec in lines:
                print("  ? '{}' is already in the list - nothing added".format(spec))
                continue
            lines.append(spec)
            continue
        match = re.match(r"^d\s+(\d+)$", raw)
        if match:
            number = int(match.group(1))
            if not 1 <= number <= len(lines):
                print("  ? no such line")
                continue
            print("  removed: {}".format(lines.pop(number - 1)))
            continue
        print("  ? unknown command")
    definition["specPackageList"] = "\n".join(lines) + ("\n" if lines else "")
    if package_lines(definition["specPackageList"]) != package_lines(session["original_spec"]):
        print("  requested packages changed in memory - use 'save' to send them to DSS")
    else:
        print("  requested packages unchanged")


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

def act_save(client, session, env_lang, env_name):
    """PUT the (possibly edited) definition; DSS answers 204 No Content."""
    try:
        client.update_code_env(env_lang, env_name, session["definition"])
    except DssApiError as err:
        print("  error: {}".format(err))
        return
    print("  saved (DSS answered 204 No Content)")
    if not fetch_definition(client, session, env_lang, env_name):
        return  # saved, but the re-read failed - surface it
    print("  requested packages now stored on the server:")
    for line in package_lines(session["definition"].get("specPackageList")):
        print("    - {}".format(line))


def act_apply(client, session, env_lang, env_name, force_rebuild, timeout):
    """POST /admin/code-envs/packages — update deps, or full rebuild if forced.

    Runs on the SAVED definition on the server (fresh GET), so unsaved local
    edits are never applied silently.
    """
    label = "rebuild" if force_rebuild else "update"
    if package_lines(session["definition"].get("specPackageList")) != \
            package_lines(session["original_spec"]):
        print("  note: you have UNSAVED edits to the requested packages;")
        print("        this {} runs on the SAVED definition on the server".format(label))
        if not confirm("proceed anyway?"):
            print("  skipped")
            return
    print("  calling DSS - the {} runs synchronously (pip/conda inside DSS)".format(label))
    print("  and can take minutes on a real instance (per-call timeout {}s) ...".format(
        timeout))
    try:
        server_def = client.get_code_env(env_lang, env_name)  # identity + saved spec
        payload = client.apply_packages(server_def, force_rebuild)
        show_messages(payload)
        fetch_definition(client, session, env_lang, env_name)
        refresh_summary(client, session, env_lang, env_name)
        print("  {} finished - refetched the definition".format(label))
    except DssApiError as err:
        print("  error: {}".format(err))


def act_jupyter(client, session, env_lang, env_name):
    """POST /admin/code-envs/jupyter?active=... on the saved definition."""
    print("  Jupyter notebook integration for this env:")
    print("    1) activate")
    print("    2) deactivate")
    choice = ask(2, ("c",))
    if choice == "c":
        print("  skipped")
        return
    active = choice == 0
    try:
        server_def = client.get_code_env(env_lang, env_name)
        payload = client.set_jupyter(server_def, active)
    except DssApiError as err:
        print("  error: {}".format(err))
        return
    if payload:
        show_messages(payload)
    print("  jupyter integration {} - refetching".format(
        "activated" if active else "deactivated"))
    fetch_definition(client, session, env_lang, env_name)


def act_delete(client, session, env_lang, env_name):
    """DELETE the env; requires typing its exact name. True if deleted."""
    print("  WARNING: deleting a code env is permanent (DSS also refuses if the")
    print("  env is still in use by projects). Type the env name to confirm.")
    try:
        raw = input("  confirm delete > ").strip()
    except EOFError:
        print("")
        raise Quit()
    if raw != env_name:
        print("  name did not match - nothing deleted")
        return False
    try:
        payload = client.delete_code_env(env_lang, env_name)
    except DssApiError as err:
        print("  error: {}".format(err))
        return False
    show_messages(payload)
    print("  deleted '{}'".format(env_name))
    return True


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="dss_codeenv_console.py",
        description="Interactive terminal UI to manage DSS code envs "
                    "(status / packages / save / update / rebuild / jupyter / "
                    "delete / create) via the public REST API. Needs an "
                    "admin-privileged API key.",
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
    parser.add_argument("--timeout", type=int, default=DEFAULT_CALL_TIMEOUT,
                        metavar="SEC",
                        help="per-call HTTP timeout in seconds (default: {}; "
                             "update/rebuild are synchronous and can run long)".format(
                                 DEFAULT_CALL_TIMEOUT))
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

        # Connection check + banner (also validates the API key). Note that
        # ALL code-env endpoints require an admin-privileged key.
        envs, headers = client.call("GET", "/admin/code-envs")
        dss_version = headers.get("DSS-Version") or "?"
        print("")
        print("connected to {} - DSS version {} - {} code env(s)".format(
            root_url, dss_version, len(envs)))
        print("navigate: numbers choose | r refresh | n new env | b back | q quit")

        while True:  # env list level
            summary = pick_env(client)
            env_screen(client, summary, args)
    except Quit:
        print("bye.")
        return EXIT_OK
    except KeyboardInterrupt:
        print("\nbye.")
        return EXIT_OK
    except DssApiError as error:
        if getattr(error, "status", None) == 403:
            print("error: {} (this console needs an admin-privileged API key - "
                  "every code-env endpoint requires it)".format(error),
                  file=sys.stderr)
        else:
            print("error: {}".format(error), file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())