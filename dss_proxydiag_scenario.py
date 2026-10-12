#!/usr/bin/env python3
"""
dss_proxydiag_scenario.py — create a scenario in a Dataiku DSS project that
runs READ-ONLY incident diagnostics on the DSS host, and show the output.

Built for the SSO/proxy breakage: after proxy lines (http_proxy /
https_proxy) were added to the DSS backend user's ~/.bashrc, browser login
fails with ERR_OPENID_IDP_EXCHANGE_CODE_FAILURE (UnknownHostException on the
proxy host). When the server is only reachable through the public API,
this is the way to see what the host actually sees. The payload answers,
in one run:

    1.  host / user the scenario runs as
    2.  proxy variables in the scenario process — a child of the DSS
        backend, so its inherited environment IS the backend's environment
    3.  the backend java processes: start time, JVM proxy options on the
        command line, proxy variables in their /proc/<pid>/environ
    4.  every 'proxy' line in the shell startup files (~/.bashrc,
        ~/.bash_profile, ~/.profile, /etc/profile, /etc/profile.d/*, ...)
        + when ~/.bashrc was last modified
    5.  every 'proxy' / proxy-host / SSO line in the DSS configuration
        files (bin/env-site.sh, bin/dss, install.ini,
        config/*.json|properties) — the datadir is discovered from the
        environment or the backend's -Djava.io.tmpdir; secret-named files
        are never printed and credential-looking values are masked
    6.  the SSO (openid/issuer/jwks/userinfo) endpoints found there
    7.  DNS resolution of the proxy host and the SSO hosts, as seen from
        the DSS host itself
    8.  TCP connectivity to the proxy (host:port) and to the SSO endpoints
    9.  a direct HTTPS GET to the SSO endpoint with proxy variables
        deliberately ignored — proving the endpoint is reachable the
        moment the backend stops sending its traffic through a proxy
    10. a verdict: did the backend come up after the ~/.bashrc edit, is
        the proxy in its environment, does the proxy host even resolve

Nothing in the payload writes, deletes or relaunches anything. --grep TEXT
additionally reports every line containing an arbitrary phrase (any place
someone may have left the proxy string). --payload-file runs any other
payload instead, through the same create → run → log machinery.

Flow: connect -> list all projects -> pick one -> create (or reuse) a
"custom_python" scenario -> set its python payload -> run it -> wait for
the run to finish -> print the run log -> offer to delete the scenario
again (default NO; --delete-scenario skips the question). Cleanup uses
DELETE /projects/{projectKey}/scenarios/{scenarioId} — the same project
write access that created the scenario; deleting it also removes its run
log from DSS, so the report exists only in this terminal afterwards.

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
    DELETE /projects/{projectKey}/scenarios/{scenarioId}               cleanup (same project write access that created it)

Note: SSO being down does not block this tool — the public API authenticates
the API key itself and never calls the identity provider; only browser
logins go through OIDC.

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
    DKU_API_KEY=... python3 dss_proxydiag_scenario.py
    DKU_API_KEY=... python3 dss_proxydiag_scenario.py --project MYPROJECT
    DKU_API_KEY=... python3 dss_proxydiag_scenario.py --url http://dss-host:11000 \
        --project MYPROJECT --grep "naappproxy"
    DKU_API_KEY=... python3 dss_proxydiag_scenario.py --project MYPROJECT \
        --delete-scenario        # diagnose, then remove the scenario, no prompt
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

DEFAULT_SCENARIO_NAME = "SSO proxy incident diagnostics"
DEFAULT_PROXY_HOST = "naappproxyhttpnopers.wlb.nam.nsroot.net"
DEFAULT_PROXY_PORT = 9999

# The scenario payload. Pure stdlib so it runs in the default DSS backend
# python (no code env needed). Strictly read-only; every section is guarded
# so a crash in one cannot hide the rest of the report.
#
# NOTE (offline-testing constraint): this text must not contain the two
# substrings that make tests/mock_dss_server.py simulate a self-restart
# ("Popen" + "restart"); the payload launches no subprocess at all, and the
# word restart is deliberately avoided below (hence "relaunch").
SNIPPET_DIAG = r'''\
# READ-ONLY incident diagnostics for the SSO / proxy breakage: where proxy
# configuration lives on this host, whether the DSS backend carries it, and
# whether the proxy and the SSO endpoints are reachable from here. This
# payload never writes, deletes or relaunches anything.
import glob
import os
import platform
import re
import socket
import sys
import time
import urllib.error
import urllib.request

try:
    import pwd
except ImportError:
    pwd = None

JAVA_VAR_NAMES = ("DKU_BACKEND_JAVA_OPTS", "JAVA_OPTS", "JDK_JAVA_OPTIONS",
                  "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS")

bashrc_path = os.path.join(os.path.expanduser("~"), ".bashrc")
bashrc_mtime = None
backend_start = None
datadir_hint = None
datadir_src = None
env_has_proxy = False
env_proxy_empty_only = False
proxy_dns = None
idp_urls = []


def section(title):
    print("")
    print("=" * 64)
    print(title)
    print("=" * 64)
    sys.stdout.flush()


def mask(text):
    """Hide credentials that may share a line with a proxy/SSO setting."""
    text = re.sub(r"//[A-Za-z0-9._%+-]+:[^@/\s]+@", "//***:***@", text)
    text = re.sub(r"(?i)([A-Za-z0-9_-]*(?:secret|passwd|password|apikey)"
                  r"[A-Za-z0-9_-]*[\"']?\s*[=:]\s*[\"']?)[^,\"\s}]+",
                  r"\1***", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1***", text)
    return text


def proxy_env_entries(environ):
    """(name, value) pairs for env vars a proxy misconfiguration hides in."""
    entries = []
    for name in sorted(environ):
        if "proxy" in name.lower() or name in JAVA_VAR_NAMES:
            entries.append((name, environ[name]))
    return entries


def read_bytes(path, limit=1048576):
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except OSError:
        return None


section("1. HOST IDENTITY")
print("DSS host         : " + platform.node())
if pwd is not None:
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        user = os.environ.get("USER") or "(unknown)"
else:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "(unknown)"
print("scenario runs as : {} (uid {})".format(user, os.getuid()))
print("home directory   : " + os.path.expanduser("~"))
print("working dir     : " + os.getcwd())
print("time            : " + time.strftime("%Y-%m-%d %H:%M:%S %Z"))
print("note            : READ-ONLY diagnostics - nothing is modified")
sys.stdout.flush()

section("2. PROXY VARIABLES IN THIS PROCESS (inherited from the DSS backend)")
print("This scenario process is a child of the DSS backend, so the")
print("environment it inherited is the backend's environment: a proxy")
print("variable listed here IS set in the backend that makes SSO calls.")
mine = proxy_env_entries(os.environ)
# the verdicts key off the classic outbound variables only: DKU_HPROXY_* and
# friends contain "proxy" but are DSS's own frontend component, and a
# present-but-EMPTY value proxies nothing at all
outbound_names = ("http_proxy", "https_proxy", "ftp_proxy", "all_proxy")
proxy_set = [(n, v) for n, v in mine if n.lower() in outbound_names and v]
proxy_empty = [(n, v) for n, v in mine
               if n.lower() in outbound_names and not v]
env_has_proxy = bool(proxy_set)
env_proxy_empty_only = not proxy_set and bool(proxy_empty)
if not mine:
    print("(none: no proxy variables and no JAVA options variables)")
else:
    for name, value in mine:
        print("  {}={}".format(name, mask(value)))
if env_has_proxy:
    print("=> the backend environment DOES carry valued proxy variables:")
    print("   outbound calls that honor them go through the proxy")
elif env_proxy_empty_only:
    print("=> proxy variables are present but EMPTY - an empty value is inert")
    print("   (nothing proxies on it, and the JVM ignores *_proxy env vars")
    print("   entirely; only -Dhttp.proxyHost-style options would apply)")
else:
    print("=> no proxy variable in the backend environment (if SSO still")
    print("   fails, the proxy configuration lives elsewhere - see sections 3-5)")
sys.stdout.flush()

section("3. DSS BACKEND JAVA PROCESSES (/proc)")


def proc_args(pid):
    raw = read_bytes("/proc/" + pid + "/cmdline")
    if not raw:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def proc_environ(pid):
    raw = read_bytes("/proc/" + pid + "/environ")
    if raw is None:
        return None
    environ = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            environ[key.decode("utf-8", "replace")] = \
                value.decode("utf-8", "replace")
    return environ


def proc_start_epoch(pid):
    stat = read_bytes("/proc/" + pid + "/stat")
    uptime = read_bytes("/proc/uptime")
    if not stat or uptime is None:
        return None
    try:
        fields = stat.decode("utf-8", "replace").rsplit(")", 1)[1].split()
        ticks = int(fields[19])
        up = float(uptime.decode("utf-8", "replace").split()[0])
        return time.time() - up + ticks / float(os.sysconf("SC_CLK_TCK"))
    except Exception:
        return None


try:
    backends = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        args = proc_args(entry)
        if not args:
            continue
        # match the executable (argv[0]), not any mention of java in the
        # arguments — a shell wrapper whose text merely says "java" and
        # "dss" must not be reported as a backend
        if "java" not in os.path.basename(args[0]).lower():
            continue
        joined = " ".join(args).lower()
        if not any(tag in joined for tag in ("dataiku", "dss", "dku")):
            continue
        backends.append((entry, args))
    if not backends:
        print("(no java process with dataiku/dss/dku on its command line was")
        print(" found - the backend may run as a different user, or with an")
        print(" unrecognised command line)")
    for pid, args in backends[:8]:
        started = proc_start_epoch(pid)
        if started is not None and (backend_start is None
                                    or started < backend_start):
            backend_start = started
        if started:
            print("pid {} - started {}".format(
                pid, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))))
        else:
            print("pid {} - started (unknown)".format(pid))
        print("  command : " + mask(" ".join(args))[:400])
        jvm_args = [arg for arg in args if "proxy" in arg.lower()]
        if jvm_args:
            print("  JVM proxy options on the command line:")
            for arg in jvm_args:
                print("    " + mask(arg))
        else:
            print("  JVM proxy options on the command line: (none)")
        environ = proc_environ(pid)
        if environ is None:
            print("  process environment: (not readable - another user?)")
            continue
        entries = proxy_env_entries(environ)
        if entries:
            print("  proxy / JAVA variables in the process environment:")
            for name, value in entries:
                print("    {}={}".format(name, mask(value)))
        else:
            print("  proxy / JAVA variables in the process environment: (none)")
        if datadir_hint is None:
            datadir_hint = environ.get("DSS_DATADIR")
            if datadir_hint is not None:
                datadir_src = "DSS_DATADIR in the environment of pid " + pid
        if datadir_hint is None:
            m = re.search(r"(?i)datadir[=:](/\S+)", " ".join(args))
            if m:
                datadir_hint = m.group(1)
                datadir_src = "the command line of pid " + pid
        if datadir_hint is None:
            # real DSS backends carry no datadir marker in env or cmdline,
            # but every one of them gets -Djava.io.tmpdir=<datadir>/tmp
            for arg in args:
                m = re.match(r"-Djava\.io\.tmpdir=(\S+)", arg)
                if not m:
                    continue
                candidate = m.group(1)
                if candidate.endswith("/tmp"):
                    candidate = candidate[:-4]
                if os.path.isdir(os.path.join(candidate, "config")) or \
                        os.path.exists(os.path.join(candidate, "bin", "dss")):
                    datadir_hint = candidate
                    datadir_src = "-Djava.io.tmpdir on the command line of pid " \
                        + pid
                    break
except Exception as err:
    print("(this section crashed: [{}] {})".format(type(err).__name__, err))
sys.stdout.flush()

section("4. PROXY LINES IN THE SHELL STARTUP FILES")
home = os.path.expanduser("~")
shell_files = [os.path.join(home, ".bashrc"), os.path.join(home, ".bash_profile"),
               os.path.join(home, ".bash_login"), os.path.join(home, ".profile"),
               "/etc/profile", "/etc/bash.bashrc", "/etc/environment"]
shell_files += sorted(glob.glob("/etc/profile.d/*"))
try:
    try:
        bashrc_mtime = os.path.getmtime(bashrc_path)
    except OSError:
        bashrc_mtime = None
    searched = 0
    hits = 0
    for path in shell_files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().split("\n")
        except OSError:
            continue
        searched += 1
        for index, line in enumerate(lines, 1):
            kind = None
            if "proxy" in line.lower():
                kind = ""
            elif PROXY_HOST and PROXY_HOST in line:
                kind = "[proxy-host] "
            elif GREP_PHRASE and GREP_PHRASE in line:
                kind = "[phrase] "
            if kind is None:
                continue
            hits += 1
            print("  {}:{}: {}{}".format(path, index, kind, mask(line.strip())))
    if bashrc_mtime is not None:
        print("~/.bashrc last modified: " + time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(bashrc_mtime)))
    print("({} file(s) searched, {} matching line(s))".format(searched, hits))
except Exception as err:
    print("(this section crashed: [{}] {})".format(type(err).__name__, err))
sys.stdout.flush()

section("5. PROXY LINES IN THE DSS CONFIGURATION FILES")
datadir = os.environ.get("DSS_DATADIR") or datadir_hint
if datadir and os.environ.get("DSS_DATADIR"):
    datadir_src = datadir_src or "DSS_DATADIR in this process's environment"
if not datadir:
    print("DSS data dir: (not found - DSS_DATADIR is in neither this")
    print("process's nor the backend's environment, and no datadir could be")
    print("derived from the backend command lines; config not searched)")
else:
    if datadir_src:
        print("DSS data dir: {} (via {})".format(datadir, datadir_src))
    else:
        print("DSS data dir: " + datadir)
    try:
        paths = [os.path.join(datadir, "bin", "dss"),
                 os.path.join(datadir, "install.ini")]
        for pattern in ("bin/*.sh", "config/*.json", "config/*.properties",
                        "config/*.ini", "config/*.cfg"):
            paths.extend(sorted(glob.glob(os.path.join(datadir, pattern))))
        searched = 0
        hits = 0
        for path in paths:
            base = os.path.basename(path).lower()
            if "secret" in base:
                continue  # keystores / secret-named files are never printed
            try:
                if os.path.getsize(path) > 2 * 1024 * 1024:
                    print("  (skipped {}: larger than 2 MB)".format(path))
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    lines = handle.read().split("\n")
            except OSError:
                continue
            searched += 1
            for index, line in enumerate(lines, 1):
                kind = None
                if "proxy" in line.lower():
                    kind = ""
                elif PROXY_HOST and PROXY_HOST in line:
                    # the host string on its own line (e.g. a JSON "host":
                    # "..." value) does not contain the word "proxy"
                    kind = "[proxy-host] "
                elif GREP_PHRASE and GREP_PHRASE in line:
                    kind = "[phrase] "
                elif re.search(r"(?i)\b(openid|oidc|issuer|jwks|userinfo)", line):
                    kind = "[sso] "
                if kind is None:
                    continue
                hits += 1
                print("  {}:{}: {}{}".format(
                    path, index, kind, mask(line.strip())))
                if kind:
                    for url in re.findall(r"https?://[^\"'\s,]+", line):
                        if PROXY_HOST and PROXY_HOST in url:
                            continue  # the proxy URL is not an SSO endpoint
                        if url not in idp_urls:
                            idp_urls.append(url)
        print("({} file(s) searched, {} matching line(s))".format(searched, hits))
    except Exception as err:
        print("(this section crashed: [{}] {})".format(type(err).__name__, err))
sys.stdout.flush()

section("6. SSO ENDPOINTS DISCOVERED IN THE CONFIG")
if not idp_urls:
    print("(no openid / issuer / jwks / userinfo URLs found - either SSO is")
    print(" configured outside the files searched, or under names this")
    print(" search did not match)")
else:
    for url in idp_urls[:6]:
        print("  " + mask(url))
sys.stdout.flush()

section("7. DNS RESOLUTION AS SEEN FROM THIS HOST")


def dns_check(host):
    try:
        infos = socket.getaddrinfo(host, None)
        addresses = sorted(set(str(info[4][0]) for info in infos))
        return "OK     " + ", ".join(addresses[:4])
    except Exception as err:
        return "FAILED " + str(err)[:140]


try:
    targets = [PROXY_HOST]
    for url in idp_urls[:3]:
        m = re.match(r"https?://([^/:]+)", url)
        if m and m.group(1) not in targets:
            targets.append(m.group(1))
    print("/etc/resolv.conf:")
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8",
                  errors="replace") as handle:
            for line in handle.read().split("\n")[:8]:
                if line.strip():
                    print("  " + line)
    except OSError:
        print("  (unreadable)")
    for host in targets:
        verdict = dns_check(host)
        if host == PROXY_HOST:
            proxy_dns = verdict.startswith("OK")
        print("  {:44} {}".format(host, verdict))
except Exception as err:
    print("(this section crashed: [{}] {})".format(type(err).__name__, err))
sys.stdout.flush()

section("8. TCP CONNECTIVITY")


def tcp_check(host, port):
    try:
        conn = socket.create_connection((host, int(port)), timeout=5)
        conn.close()
        return "OPEN               connection succeeded"
    except socket.gaierror as err:
        return "DNS-FAIL          " + str(err)[:120]
    except OSError as err:
        return "REFUSED/UNREACHABLE " + str(err)[:120]


try:
    pairs = [(PROXY_HOST, PROXY_PORT)]
    for url in idp_urls[:3]:
        m = re.match(r"https?://([^/:]+)(?::(\d+))?", url)
        if m:
            host = m.group(1)
            if m.group(2):
                port = int(m.group(2))
            else:
                port = 443 if url.startswith("https") else 80
            if (host, port) not in pairs:
                pairs.append((host, port))
    for host, port in pairs:
        print("  {}:{}  {}".format(host, port, tcp_check(host, port)))
except Exception as err:
    print("(this section crashed: [{}] {})".format(type(err).__name__, err))
sys.stdout.flush()

section("9. DIRECT HTTPS TO THE SSO ENDPOINT (proxy variables ignored on purpose)")
if not idp_urls:
    print("(no SSO endpoint was discovered - nothing to test)")
else:
    try:
        for url in idp_urls[:2]:
            print("  GET " + url)
            try:
                # ProxyHandler({}) = use NO proxy, whatever the environment
                # says: this is exactly what the SSO call looks like when the
                # backend no longer carries proxy variables.
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}))
                with opener.open(url, timeout=10) as response:
                    code = response.getcode()
                print("    -> HTTP {}: the endpoint answers when the call".format(code))
                print("       goes out WITHOUT a proxy - SSO can work again")
                print("       once the backend stops proxying its traffic")
            except urllib.error.HTTPError as err:
                print("    -> HTTP {}: any HTTP answer proves the endpoint".format(err.code))
                print("       is reachable without a proxy")
            except Exception as err:
                print("    -> FAILED: {}".format(str(err)[:160]))
                print("       (if this failed for DNS/TLS reasons the direct")
                print("        path is broken too - not a proxy-only incident)")
    except Exception as err:
        print("(this section crashed: [{}] {})".format(type(err).__name__, err))
sys.stdout.flush()

section("10. PUTTING IT TOGETHER")
if bashrc_mtime is not None and backend_start is not None:
    if backend_start >= bashrc_mtime:
        print("The longest-running backend java process came up AFTER the")
        print("last ~/.bashrc edit: DSS was started with the current")
        print("~/.bashrc applied, so proxy lines in it had a chance to")
        print("reach the backend's environment.")
    else:
        print("The longest-running backend java process came up BEFORE the")
        print("last ~/.bashrc edit: the backend predates the edit, so its")
        print("proxy variables (if any) came from elsewhere - or the edit")
        print("has not taken effect yet because DSS was not relaunched.")
else:
    print("(backend start time or ~/.bashrc mtime unknown - no timing verdict)")
if env_has_proxy:
    print("The backend environment carries proxy variables: outbound calls")
    print("that honor them - including the SSO code exchange - are being")
    print("sent through the proxy host.")
elif env_proxy_empty_only:
    print("Proxy variables in the backend environment are EMPTY: the")
    print("environment sends nothing through a proxy. If the SSO code")
    print("exchange still goes to the proxy host, the setting lives in the")
    print("DSS configuration itself - see section 5 (general-settings,")
    print("env-site.sh, ...).")
else:
    print("No proxy variable in the backend environment: the SSO breakage")
    print("is not an inherited-environment proxy (see sections 3 and 5).")
if proxy_dns is False:
    print("The proxy host does NOT resolve from the DSS host: no proxied")
    print("call can ever succeed from here. The proxy configuration has to")
    print("go - or the proxy address must be one this host can resolve.")
elif proxy_dns is True:
    print("The proxy host resolves from the DSS host (see section 8 for")
    print("whether it accepts connections).")
print("")
print("DIAG-DONE: read-only diagnostics complete - nothing was modified")
sys.stdout.flush()
'''


def build_snippet(proxy_host, proxy_port, grep_phrase):
    """The scenario payload: target constants prepended to the snippet."""
    header = ("PROXY_HOST = {!r}\n"
              "PROXY_PORT = {!r}\n"
              "GREP_PHRASE = {!r}\n").format(
        proxy_host, int(proxy_port), grep_phrase or "")
    return header + SNIPPET_DIAG


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

    def delete_scenario(self, project_key, scenario_id):
        # Same call as the official client's DSSScenario.delete():
        # DELETE /projects/{pk}/scenarios/{id}, JSON answer (empty body -> {})
        payload, _ = self.call("DELETE", "/projects/{}/scenarios/{}".format(
            q(project_key), q(scenario_id)))
        return payload


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


def ask_delete_scenario():
    """Yes/no prompt, default NO (deleting is the destructive choice, and
    it removes the run log from DSS too). EOF counts as no: the diagnosis
    is already done and printed, nothing here may crash the script."""
    while True:
        try:
            raw = input("Delete the scenario now? [y/N]: ").strip().lower()
        except EOFError:
            print("")
            return False
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
    """Fire the manual trigger, wait for our run, print its log.
    Returns (exit_code, finished); finished means the run ended and the
    report was printed above - i.e. a terminal copy of it exists."""
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
        return EXIT_FAIL, False
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
        return EXIT_OK, False
    if our_run is None:
        print("  WARNING: the run did not finish within {}s - it may still be".format(
            wait_timeout))
        print("  running on DSS. Check the log later: project {} -> Scenarios".format(
            project_key) + " -> {} -> History".format(scenario_name))
        return EXIT_FAIL, False
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
        return EXIT_FAIL, True
    return EXIT_OK, True


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="dss_proxydiag_scenario.py",
        description="Create a DSS scenario that runs READ-ONLY SSO/proxy "
                    "incident diagnostics on the DSS host, run it, and show "
                    "its report, via the public REST API.",
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
    parser.add_argument("--proxy-host", default=DEFAULT_PROXY_HOST,
                        metavar="HOST",
                        help="the proxy host to test DNS/TCP against "
                             "(default: {})".format(DEFAULT_PROXY_HOST))
    parser.add_argument("--proxy-port", type=int, default=DEFAULT_PROXY_PORT,
                        metavar="PORT",
                        help="the proxy port to test TCP against "
                             "(default: {})".format(DEFAULT_PROXY_PORT))
    parser.add_argument("--grep", default=None, metavar="TEXT",
                        help="additionally report every line containing this "
                             "exact text in the searched files (case-sensitive, "
                             "matched literally)")
    parser.add_argument("--payload-file", metavar="FILE",
                        help="run this file's python as the scenario payload "
                             "instead of the built-in diagnostics snippet")
    parser.add_argument("--no-run", action="store_true",
                        help="create/set the scenario but do not run it")
    parser.add_argument("--delete-scenario", action="store_true",
                        help="after showing the run log, delete the scenario "
                             "without asking (default: ask; deleting also "
                             "removes its run log from DSS, so the printed "
                             "report becomes the only copy)")
    parser.add_argument("--wait-timeout", type=int, default=240, metavar="SEC",
                        help="seconds to wait for the scenario run to finish "
                             "(default: 240; DNS/TCP probes can take a while)")
    parser.add_argument("--timeout", type=int, default=30, metavar="SEC",
                        help="per-call HTTP timeout in seconds (default: 30)")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS certificate verification (self-signed DSS)")
    args = parser.parse_args(argv)
    if not 1 <= args.proxy_port <= 65535:
        parser.error("--proxy-port must be 1-65535, got {}".format(args.proxy_port))
    if not args.proxy_host.strip():
        parser.error("--proxy-host must not be empty")
    args.proxy_host = args.proxy_host.strip()
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
    """The scenario's python payload: --payload-file or None (built-in)."""
    if not payload_file:
        return None
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
        snippet = read_payload(args.payload_file) or build_snippet(
            args.proxy_host, args.proxy_port, args.grep)
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
        if not args.payload_file:
            print("  payload plan  : READ-ONLY - nothing is written, nothing is")
            print("                  relaunched on the DSS host. The payload reads")
            print("                  this process's and the backend's environment")
            print("                  and /proc command lines, greps the shell")
            print("                  startup files and the DSS configuration for")
            print("                  'proxy' lines, and performs OUTBOUND DNS /")
            print("                  TCP / HTTPS probes to the proxy host ({})".format(
                args.proxy_host))
            print("                  and any SSO endpoints found in the config.")

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

        exit_code, finished = run_and_show(client, project_key, scenario_id,
                                           args.scenario_name, args.wait_timeout)
        if not finished:
            return exit_code

        # The report is printed above. Cleanup offer: deleting the scenario
        # removes its run log from DSS too, so afterwards the copy above is
        # the only one.
        print("")
        print("cleanup: delete scenario {!r} (and its run log) from project {}?".format(
            args.scenario_name, project_key))
        if args.delete_scenario or ask_delete_scenario():
            try:
                client.delete_scenario(project_key, scenario_id)
            except DssApiError as error:
                print("")
                print("warning: could not delete the scenario: {}".format(error))
                print("delete it from the DSS UI: project {} -> Scenarios -> {}".format(
                    project_key, args.scenario_name) + " -> Settings -> Delete")
                return EXIT_FAIL
            print("")
            print("scenario deleted (id was {}). The report above is the only".format(
                scenario_id) + " copy now - save it if you need it.")
            return exit_code
        print("scenario kept - re-run it from the DSS UI (project {} -> Scenarios".format(
            project_key) + " -> {} -> 'Run now'); this script reuses it next time.".format(
            args.scenario_name))
        return exit_code
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