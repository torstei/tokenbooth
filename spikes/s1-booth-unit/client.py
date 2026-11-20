"""S1 spike, caller side: run as the desktop user.

Connects to the compositor, passes that connection and a URL to a booth instance, and prints
what the booth reports until it exits.
"""

import os
import socket
import sys

BOOTH = "/run/tokenbooth-spike/booth.sock"
url = sys.argv[1] if len(sys.argv) > 1 else "https://gopro.com/login"

display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
path = display if display.startswith("/") else os.path.join(os.environ["XDG_RUNTIME_DIR"], display)
wayland = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
wayland.connect(path)

booth = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
booth.connect(BOOTH)
socket.send_fds(booth, [url.encode() + b"\n"], [wayland.fileno()])
wayland.close()

with booth.makefile("r") as replies:
    for line in replies:
        print("booth:", line.rstrip())
