#!/usr/bin/env python3
"""
dss_usergroups_console.py — interactive console to edit a DSS user's group
memberships and DSS profile (userProfile) via the public REST API. Needs an
admin-privilege API key: everything here lives under /admin.

Flow:
    connect -> list users -> pick one -> user detail (groups + profile)
    -> stage edits in memory:
        add groups...      (multi-pick from the instance groups the user
                            is not in; admin groups are marked)
        remove groups...   (multi-pick from the user's current groups)
        change DSS profile (pick from the known/discovered profiles, or
                            type one)
    -> save changes: GET-round-trip PUT — the fresh GET user object is
       modified with the staged groups/profile and sent back verbatim
       (undocumented attributes pass through untouched, as the API
       requires), then re-GET and compared (save verified).
    -> discard changes: drop the staged edits.
    'b' goes back to the user picker, 'q' quits anywhere.

Nothing is written before "save changes": adds/removes and the profile
choice are staged in memory and can be reviewed (and discarded) on the
user screen first.

Target API: DSS public API 15 — https://doc.dataiku.com/dss/api/15/rest/

Endpoints used (relative to {dss-url}/public/api):
    GET /admin/users/             list users               Admin
    GET /admin/users/{login}      get one user             Admin
    PUT /admin/users/{login}      update groups/profile    Admin
    GET /admin/groups             list groups (the pool    Admin
                                   to add from)

Notes:
  - PUT .../users/{login}: "The User object given as body MUST have been
    previously obtained from a GET user call at the same URL" — hence the
    GET-round-trip on save. Real DSS answers 200 {"msg": "Edited user X"}.
  - the User object: login, sourceType, displayName, email, enabled,
    groups[], userProfile, codeAllowed, plus undocumented attributes that
    may come and go — the console never touches them.
  - the docs don't enumerate userProfile values ("DATA_ANALYST" appears
    in the create-user example); the picker offers the built-in common
    three (DATA_SCIENTIST / DATA_ANALYST / READER) plus every profile
    currently seen on the instance's users, plus a manual entry. An
    invalid profile is rejected by DSS at save time (printed, staged
    edits kept).
  - non-LOCAL users (LDAP/SAML): memberships may be re-synced from the
    directory; the API still accepts the PUT. sourceType is displayed.
  - SSO being down does not block this tool: the API key authenticates
    itself and never calls the identity provider.

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
    DKU_API_KEY=... python3 dss_usergroups_console.py
    DKU_API_KEY=... python3 dss_usergroups_console.py --url http://dss:11000
    DKU_API_KEY=... python3 dss_usergroups_console.py --login jdoe
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

PLACEHOLDER_HOST = "dss-host"  # while unchanged, the script asks for the URL
API_PREFIX = "/public/api"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NOTFOUND = 3

# Common built-in DSS profiles; the picker adds every profile currently
# seen on the instance's users on top of these.
KNOWN_PROFILES = ("DATA_SCIENTIST", "DATA_ANALYST", "READER")


class Quit(Exception):
    """Raised to leave the script cleanly (q, quit, EOF)."""


class DssApiError(Exception):
    """A call to the DSS public API failed."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class DssClient(object):
    """Thin stdlib HTTP client for the users/groups subset of the DSS API."""

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

    def list_users(self):
        payload, _ = self.call("GET", "/admin/users/")
        return payload

    def get_user(self, login):
        payload, _ = self.call("GET", "/admin/users/{}".format(q(login)))
        return payload

    def put_user(self, login, user):
        """Save a GET-round-trip user object. Real DSS: 200 {"msg": ...}."""
        payload, _ = self.call("PUT", "/admin/users/{}".format(q(login)),
                               body=user)
        return payload

    def list_groups(self):
        payload, _ = self.call("GET", "/admin/groups")
        return payload


def q(segment):
    """URL-quote one path segment."""
    return urllib.parse.quote(str(segment), safe="")


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


def ask_multi(count):
    """Multi-pick prompt: '1 3-4', '1,3'. Returns a sorted list of 0-based
    indices; empty input = none picked (back to the menu). Raises Quit on
    q/EOF."""
    prompt = ("choices [1-{}] (space/comma list, e.g. '1 3-4'; empty = "
              "none)/q: ".format(count))
    while True:
        try:
            raw = input(prompt).strip().lower()
        except EOFError:
            print("")
            raise Quit()
        if raw in ("q", "quit", "exit"):
            raise Quit()
        if raw == "":
            return []
        picked = set()
        bad = None
        for token in re.split(r"[\s,]+", raw):
            if not token:
                continue
            match = re.match(r"^(\d+)(?:-(\d+))?$", token)
            if not match:
                bad = token
                break
            first = int(match.group(1))
            last = int(match.group(2) or first)
            if first < 1 or last > count or first > last:
                bad = token
                break
            picked.update(range(first, last + 1))
        if bad is not None:
            print("  ? {!r} is not a number/range in 1-{}".format(bad, count))
            continue
        return sorted(i - 1 for i in picked)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sorted_users(users):
    return sorted(users, key=lambda u: str(u.get("login", "")).lower())


def sorted_groups(groups):
    return sorted(groups, key=lambda g: str(g.get("name", "")).lower())


def effective_groups(user, staged_add, staged_remove):
    """The user's groups as they would be saved: current minus staged
    removals plus staged additions (additions appended in pool order)."""
    result = [g for g in user.get("groups") or [] if g not in staged_remove]
    result.extend(g for g in staged_add if g not in result)
    return result


def group_map(groups):
    return {g.get("name"): g for g in groups}


# ---------------------------------------------------------------------------
# flow steps
# ---------------------------------------------------------------------------

def pick_user(client):
    """User picker; returns a user dict. Refresh with 'r', quit with 'q'."""
    while True:
        error = None
        try:
            users = client.list_users()
        except DssApiError as err:
            users, error = [], str(err)
        users = sorted_users(users)
        screen("USERS ({} on this DSS)".format(len(users)))
        if error:
            print("  error: {}".format(error))
        elif not users:
            print("  (no users returned - the API key is an admin key, right?)")
        for i, user in enumerate(users, 1):
            parts = []
            if user.get("enabled") is False:
                parts.append("[disabled]")
            stype = user.get("sourceType")
            if stype and stype != "LOCAL":
                parts.append("[{}]".format(stype))
            print("  {:2}) {:16} {:24} {:14} {:2} grp {}".format(
                i, user.get("login", "?"), (user.get("displayName") or "")[:24],
                user.get("userProfile") or "?", len(user.get("groups") or []),
                " ".join(parts)))
        choice = ask(len(users), ("r",))
        if choice == "r":
            continue
        return users[choice]


def render_user(client, user, staged_add, staged_remove, staged_profile):
    """The user detail screen: profile, attributes, groups with staged
    markers. `user` is the last saved state."""
    login = user.get("login", "?")
    title = "USER {} - {}".format(login, user.get("displayName") or "?")
    screen(title)
    profile = user.get("userProfile") or "?"
    if staged_profile is not None:
        print("  profile : {}   (staged -> {})".format(profile,
                                                       staged_profile))
    else:
        print("  profile : {}".format(profile))
    print("  email   : {}".format(user.get("email") or "(none)"))
    print("  source  : {}   enabled: {}   codeAllowed: {}".format(
        user.get("sourceType") or "?", user.get("enabled", True),
        user.get("codeAllowed", "?")))
    current = user.get("groups") or []
    effective = effective_groups(user, staged_add, staged_remove)
    print("  groups  : {} effective ({} saved)".format(len(effective),
                                                        len(current)))
    by_name = group_map(client.cached_groups)
    for name in current:
        marks = []
        if name in staged_remove:
            marks.append("[- remove staged]")
        info = by_name.get(name)
        if info and info.get("description"):
            marks.append("({})".format(info["description"]))
        print("    - {} {}".format(name, " ".join(marks)))
    for name in staged_add:
        info = by_name.get(name)
        suffix = "({})".format(info["description"]) if info and info.get(
            "description") else ""
        admin = "  ** admin group **" if info and info.get("admin") else ""
        print("    + {} [+ add staged] {}{}".format(name, suffix, admin))


def stage_add_groups(client, user, staged_add, staged_remove):
    """Multi-pick from the instance groups not effectively assigned."""
    if not client.cached_groups:
        print("  (no group list available - GET /admin/groups failed earlier)")
        return
    effective = set(effective_groups(user, staged_add, staged_remove))
    pool = [g for g in sorted_groups(client.cached_groups)
            if g.get("name") not in effective]
    if not pool:
        print("  (the user is already in every group on this DSS)")
        return
    screen("ADD GROUPS - pick any (not currently assigned)")
    for i, group in enumerate(pool, 1):
        admin = "  ** admin group **" if group.get("admin") else ""
        print("  {:2}) {:24} {}{}".format(i, group.get("name", "?"),
                                          (group.get("description") or "")[:30],
                                          admin))
    picked = ask_multi(len(pool))
    for index in picked:
        name = pool[index].get("name")
        if name in staged_remove:
            staged_remove.remove(name)  # un-removing is the same as adding
        elif name not in staged_add:
            staged_add.append(name)
            if pool[index].get("admin"):
                print("  note: '{}' is an ADMIN group - its members get full"
                      " DSS admin rights".format(name))
    if picked:
        print("  staged +{} (save to apply)".format(len(picked)))


def stage_remove_groups(user, staged_add, staged_remove):
    """Multi-pick from the effective groups. Removing a group whose add is
    staged simply unstages the add; removing a saved group stages the
    removal."""
    effective = effective_groups(user, staged_add, staged_remove)
    if not effective:
        print("  (no groups to remove)")
        return
    saved = set(user.get("groups") or [])
    screen("REMOVE GROUPS - pick any (currently assigned)")
    for i, name in enumerate(effective, 1):
        mark = "(add was staged)" if name not in saved else ""
        print("  {:2}) {:24} {}".format(i, name, mark))
    picked = ask_multi(len(effective))
    for index in picked:
        name = effective[index]
        if name in staged_add:
            staged_add.remove(name)
        elif name in saved and name not in staged_remove:
            staged_remove.append(name)
    if picked:
        print("  staged -{} (save to apply)".format(len(picked)))


def stage_profile(users, user, staged_profile):
    """DSS profile picker: common built-ins + profiles seen on the
    instance's users; 'm' types one manually."""
    discovered = {u.get("userProfile") for u in users if u.get("userProfile")}
    profiles = sorted(set(KNOWN_PROFILES) | discovered)
    current = staged_profile if staged_profile is not None else user.get(
        "userProfile")
    screen("DSS PROFILE for {}".format(user.get("login", "?")))
    for i, profile in enumerate(profiles, 1):
        mark = "(current)" if profile == current else ""
        print("  {:2}) {:18} {}".format(i, profile, mark))
    print("   m) type a profile name manually")
    choice = ask(len(profiles), ("m",))
    if choice == "m":
        while True:
            try:
                raw = input("profile name (empty = cancel): ").strip()
            except EOFError:
                print("")
                raise Quit()
            if raw.lower() in ("q", "quit", "exit"):
                raise Quit()
            if not raw:
                return None
            if re.match(r"^[A-Za-z][A-Za-z0-9_]*$", raw):
                return raw
            print("  ? letters/digits/underscore, starting with a letter")
    profile = profiles[choice]
    if profile == user.get("userProfile"):
        return None  # re-picked the saved one: nothing to stage
    return profile


def save_user(client, login, staged_add, staged_remove, staged_profile):
    """GET-round-trip PUT: modify a FRESH GET user object with the staged
    groups/profile, PUT it back, re-GET and compare. Returns the saved
    user object. A DssApiError propagates to the caller (save failed; the
    staged edits stay up)."""
    fresh = client.get_user(login)
    fresh["groups"] = effective_groups(fresh, staged_add, staged_remove)
    if staged_profile is not None:
        fresh["userProfile"] = staged_profile
    response = client.put_user(login, fresh)
    print("  saved: {}".format(response.get("msg") or "OK"))
    check = client.get_user(login)
    ok_groups = set(check.get("groups") or []) == set(fresh["groups"])
    ok_profile = staged_profile is None or check.get(
        "userProfile") == staged_profile
    if ok_groups and ok_profile:
        print("  re-read verified ({} group(s), profile {})".format(
            len(check.get("groups") or []), check.get("userProfile") or "?"))
    else:
        print("  WARNING: the re-read state differs from what was sent:")
        print("    groups  now: {}".format(check.get("groups")))
        print("    profile now: {}".format(check.get("userProfile")))
    return check


def user_actions_loop(client, users, login):
    """The user screen: stage add/remove/profile edits, save, discard.
    Returns to the user picker on 'b'. Raises Quit on q/EOF."""
    user = client.get_user(login)
    try:
        client.cached_groups = client.list_groups()
    except DssApiError:
        client.cached_groups = []  # add picker will report an empty pool
    staged_add = []
    staged_remove = []
    staged_profile = None
    while True:
        render_user(client, user, staged_add, staged_remove, staged_profile)
        dirty = bool(staged_add or staged_remove or staged_profile is not None)
        print("")
        print("   1) add groups...")
        print("   2) remove groups...")
        print("   3) change DSS profile...")
        if dirty:
            print("   4) save changes")
            print("   5) discard staged changes")
        choice = ask(5 if dirty else 3, ("b",))
        if choice == "b":
            return
        if choice == 0:
            stage_add_groups(client, user, staged_add, staged_remove)
        elif choice == 1:
            stage_remove_groups(user, staged_add, staged_remove)
        elif choice == 2:
            result = stage_profile(users, user, staged_profile)
            staged_profile = result  # None clears a previously staged pick
            if result is not None:
                print("  staged profile {} (save to apply)".format(result))
        elif choice == 3 and dirty:
            try:
                user = save_user(client, login, staged_add, staged_remove,
                                 staged_profile)
            except DssApiError as err:
                print("  save FAILED (nothing applied; staged edits kept): "
                      "{}".format(err))
                continue
            staged_add, staged_remove, staged_profile = [], [], None
        elif choice == 4 and dirty:
            staged_add, staged_remove, staged_profile = [], [], None
            print("  staged changes discarded ({} group(s), profile {} "
                  "unchanged on DSS)".format(len(user.get("groups") or []),
                                             user.get("userProfile") or "?"))


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="dss_usergroups_console.py",
        description="Interactive console to edit a DSS user's group "
                    "memberships and DSS profile via the public REST API "
                    "(admin-privilege API key required). Edits are staged "
                    "in memory and applied with 'save changes' (a "
                    "GET-round-trip PUT of the user object).",
        epilog="Connection: --url, env DSS_URL, or the CONNECTION DEFAULTS at "
               "the top of this file; the script prompts for the URL if none "
               "is set. Auth: env DKU_API_KEY / --api-key-file / hidden prompt; "
               "the key must have DSS admin privileges (all calls are under "
               "/admin). API docs: https://doc.dataiku.com/dss/api/15/rest/")
    parser.add_argument("--url",
                        help="DSS base URL, e.g. http://dss-host:11000 "
                             "(default: env DSS_URL, then built-in defaults)")
    parser.add_argument("--api-key-file", metavar="FILE",
                        help="file holding the DSS API key (default: env "
                             "DKU_API_KEY, then a hidden prompt)")
    parser.add_argument("--login", metavar="LOGIN",
                        help="user login to edit (skips the interactive "
                             "picker; exit 3 when the user is not found)")
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
        client.cached_groups = []

        # Connection check + banner (also validates the API key's admin
        # privileges: every endpoint here needs them).
        users, headers = client.call("GET", "/admin/users/")
        dss_version = headers.get("DSS-Version") or "?"
        print("")
        print("connected to {} - DSS version {} - {} user(s) on this "
              "instance".format(root_url, dss_version, len(users)))

        if args.login:
            logins = [str(u.get("login", "")) for u in users]
            if args.login not in logins:
                print("error: user '{}' not found on this DSS".format(
                    args.login), file=sys.stderr)
                print("visible logins: {}".format(", ".join(
                    sorted(logins)[:15])), file=sys.stderr)
                return EXIT_NOTFOUND
            login = args.login
        else:
            login = None

        while True:
            if login is None:
                login = pick_user(client).get("login")
            user_actions_loop(client, users, login)
            login = None  # back from the user screen: re-pick
            users = client.list_users()  # refresh for the next pass
    except Quit:
        print("bye.")
        return EXIT_OK
    except KeyboardInterrupt:
        print("\nbye.")
        return EXIT_OK
    except DssApiError as error:
        print("error: {}".format(error), file=sys.stderr)
        if getattr(error, "status", None) == 401:
            print("(the API key was rejected - check DKU_API_KEY)",
                  file=sys.stderr)
        elif getattr(error, "status", None) == 403:
            print("(403 on an /admin endpoint - the API key has no DSS "
                  "admin privileges)", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
