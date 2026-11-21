"""S5 spike, caller side: run as the desktop user.

    client.py PROFILE.toml [--login] [--probe] [--local] [--home DIR]

Loads the profile, answers from the cached token if it is still valid (unless --login), else
connects to the compositor and passes that connection, the request and the keep-set to a
Chrome booth: the unit's socket, or with --local a booth.py started as the user.
State lives in its own home (default ~/.local/share/tokenbooth-s5), since its keep-set holds
DevTools cookie objects, not the prototype's.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tokenbooth import store  # noqa: E402

BOOTH = "/run/tokenbooth-spike-chrome/booth.sock"
HERE = Path(__file__).resolve().parent

parser = argparse.ArgumentParser()
parser.add_argument("profile", type=Path)
parser.add_argument("--login", action="store_true", help="ignore a cached token")
parser.add_argument("--probe", action="store_true", help="report the sandbox once the page loads, then close")
parser.add_argument("--local", action="store_true", help="run booth.py as the user instead of the unit")
parser.add_argument("--home", type=Path, default=Path.home() / ".local/share/tokenbooth-s5")
args = parser.parse_args()

name = args.profile.stem
with args.profile.open("rb") as f:
    overrides = tomllib.load(f)
profile = store.Profile.load(args.home, name, overrides)

if not (args.login or args.probe) and (token := store.cached_token(args.home, name)):
    print(token)
    sys.exit(0)

try:
    kept = json.loads(store.keep_path(args.home, name).read_text())
except (OSError, ValueError):
    kept = []
kept = [c for c in kept if c.get("session") or c.get("expires", 0) > time.time()]

request = {"start_url": profile.start_url, "allowed_hosts": profile.allowed_hosts, "cookie": profile.cookie,
           "cookie_domain": profile.cookie_domain, "done_url": profile.done_url,
           "keep_cookies": profile.keep_cookies, "keep": kept, "timeout": profile.timeout, "probe": args.probe}

display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
path = display if display.startswith("/") else os.path.join(os.environ["XDG_RUNTIME_DIR"], display)
wayland = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
wayland.connect(path)

booth_process = None
if args.local:
    # Chrome falls back to its Qt shim when GTK finds no display of its own, and Qt aborts the
    # process; an unloadable libQt*Widgets makes the fallback fail cleanly
    noqt = Path(tempfile.mkdtemp(prefix="s5-noqt-"))
    for lib in ("libQt5Widgets.so.5", "libQt6Widgets.so.6"):
        (noqt / lib).touch()
    booth, theirs = socket.socketpair()
    booth_process = subprocess.Popen([sys.executable, "-I", str(HERE / "booth.py"), str(theirs.fileno())],
                                     pass_fds=[theirs.fileno()], env={**os.environ, "S5_NOQT": str(noqt)})
    theirs.close()
else:
    booth = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    booth.connect(BOOTH)
socket.send_fds(booth, [json.dumps(request).encode() + b"\n"], [wayland.fileno()])
wayland.close()

result = None
with booth.makefile("r") as replies:
    for line in replies:
        message = json.loads(line)
        if "log" in message:
            print("booth:", message["log"]["text"], file=sys.stderr)
        elif "result" in message:
            result = message["result"]
if booth_process:
    booth_process.wait()

if result is None:
    print("client: booth ended without a result", file=sys.stderr)
    sys.exit(1)
if profile.keep_cookies and not args.probe:
    store.save_keep(args.home, name, result["keep"])
    print(f"client: kept {len(result['keep'])} cookie(s): "
          + " ".join(f"{c['name']}@{c['domain']}" for c in result["keep"]), file=sys.stderr)
if not result["token"]:
    sys.exit(0 if args.probe else 1)
store.save_token(args.home, name, result["token"], result["expires"])
print(result["token"])
