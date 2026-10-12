# payload_strip_all_proxy.py — a custom_python scenario payload for
# dss_proxyfix_scenario.py --payload-file (NOT a standalone script).
#
# Incident it was built for: SSO login fails with
# ERR_OPENID_IDP_EXCHANGE_CODE_FAILURE (UnknownHostException on the proxy
# host) after proxy lines — http_proxy / https_proxy, any spelling — were
# added to the DSS backend user's ~/.bashrc, and DSS was relaunched.
#
# Why not the built-in payload: dss_proxyfix_scenario.py --phrase removes
# every matching line fine, but --strip-env strips ONE variable from the
# relaunch environment; this incident needs ALL proxy variables gone, since
# the running backend may carry several (http_proxy AND https_proxy AND
# their uppercase spellings).
#
# Run it with (the driver does create/reuse, payload round-trip, confirm,
# outage + back-online watching — it reads the PROXYFIX-RESULT markers this
# payload prints, so its whole flow works unchanged):
#
#     DKU_API_KEY=... python3 dss_proxyfix_scenario.py \
#         --url http://dss-host:11000 --project MYPROJECT \
#         --scenario-name "Remove all proxy lines and restart DSS" \
#         --payload-file payload_strip_all_proxy.py
#
# What it does, in order (all reported into the run log):
#   1. prints host / user / bashrc path (same diagnostics as the other tools)
#   2. backs up ~/.bashrc to ~/.bashrc.bak-proxyfix-<timestamp> (copy2)
#   3. removes every line that exports an env var whose name contains
#      "proxy" (any case) — EXCEPT no_proxy/NO_PROXY, which only *exempts*
#      hosts from proxying and is worth keeping — plus every line that sets
#      a JVM -D*proxy* option (e.g. a DKU_BACKEND_JAVA_OPTS line carrying
#      -Dhttp.proxyHost=...). Lines are removed whole; the backup has them.
#   4. atomically rewrites ~/.bashrc (temp file + copymode + os.replace)
#      and re-reads it to verify nothing matching remains
#   5. strips every proxy-named env var (except no_proxy*) from the
#      environment it passes to the relaunch: the scenario process
#      inherited them from the CURRENT backend, and bin/dss does not
#      re-source ~/.bashrc, so without this the NEW backend would come up
#      proxied again and nothing would change
#   6. relaunches DSS detached (start_new_session=True, output ->
#      ~/dss-restart-scenario.log) — the backend running this payload dies
#      seconds later, which is expected — and prints
#      "PROXYFIX-RESULT: line-removed, restart launched"
#
# Scope: only ~/.bashrc of the scenario user (the file the incident lives
# in). If dss_proxydiag_scenario.py shows proxy lines in OTHER shell files
# or in DSS configuration, those need separate handling — this payload does
# not touch them.
#
# DSS_BIN: the dss administration command to relaunch with. The env
# variable DSS_BIN wins if present (that is how the offline sandbox tests
# point it at a fake binary); on a real server it is unset and the default
# below applies — edit it if the install lives elsewhere.

import getpass
import os
import platform
import re
import shutil
import subprocess
import sys
import time

DSS_BIN = os.environ.get("DSS_BIN") or "/dataiku/dss_data/bin/dss"

bashrc = os.path.join(os.path.expanduser("~"), ".bashrc")
restart_log = os.path.join(os.path.expanduser("~"), "dss-restart-scenario.log")

# any exported variable whose name contains "proxy" (case-insensitive),
# except the harmless no_proxy exemptions ...
line_pattern = re.compile(
    r"^\s*(?:export\s+)?(?![A-Za-z0-9_]*no_)[A-Za-z0-9_]*proxy[A-Za-z0-9_]*\s*=",
    re.IGNORECASE)
# ... plus any line setting a JVM proxy option (whatever variable carries it)
jvm_pattern = re.compile(r"-D[A-Za-z0-9_.]*proxy", re.IGNORECASE)

print("DSS host         : " + platform.node())
try:
    user = getpass.getuser()
except Exception:
    user = os.environ.get("USER") or "(unknown)"
print("scenario runs as : " + user)
print("bashrc           : " + bashrc)
print("target           : every line exporting a *proxy* variable (except")
print("                   no_proxy*) or setting a JVM -D*proxy* option")
print("restart command  : " + DSS_BIN + " restart")
sys.stdout.flush()

try:
    with open(bashrc, "r", encoding="utf-8", errors="replace") as handle:
        content = handle.read()
except OSError as err:
    print("PROXYFIX-RESULT: error - cannot read " + bashrc + ": " + str(err))
    sys.exit(1)
lines = content.split("\n")
hits = [i for i, line in enumerate(lines)
        if line_pattern.search(line) or jvm_pattern.search(line)]
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
    residue = [line for line in handle.read().split("\n")
               if line_pattern.search(line) or jvm_pattern.search(line)]
if residue:
    print("PROXYFIX-RESULT: error - target line still present after rewrite")
    print("NOT restarting DSS; restore from " + backup + " if needed")
    sys.exit(1)
print(".bashrc updated  : {} line(s) removed ({} -> {} lines)".format(
    len(hits), len(lines), len(kept)))
sys.stdout.flush()

# Strip EVERY proxy-named variable (except no_proxy*) from the relaunch
# environment — the scenario process inherited them all from the CURRENT
# backend, and bin/dss does not re-source ~/.bashrc.
env = dict(os.environ)
stripped = []
for name in list(env):
    low = name.lower()
    if "proxy" in low and not low.startswith("no_"):
        stripped.append(name + "=" + env.pop(name))
if stripped:
    print("restart env     : stripped " + ", ".join(stripped))
else:
    print("restart env     : no proxy variable inherited (nothing to strip)")
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