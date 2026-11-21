"""What a booth does with the fds a request carries: one is the Wayland connection; more is refused."""

import socket
import subprocess
import sys

from tokenbooth import protocol
from tokenbooth.booth import adopt_wayland
from tokenbooth.protocol import Request, Result
from tokenbooth.store import Profile


def test_the_passed_connection_replaces_the_display():
    env = {"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"}
    adopt_wayland(7, env)
    assert env == {"WAYLAND_SOCKET": "7", "XDG_RUNTIME_DIR": "/run/user/1000"}


def test_a_request_with_two_fds_is_refused():
    ours, theirs = socket.socketpair()
    booth = subprocess.Popen([sys.executable, "-I", "-m", "tokenbooth.booth", str(theirs.fileno())],
                             pass_fds=[theirs.fileno()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    theirs.close()
    a, b = socket.socketpair()
    profile = Profile(name="t", start_url="https://example.invalid/", cookie="t", cookie_domain="example.invalid")
    protocol.send(ours, Request(mode="login", profile=profile).to_message(), [a.fileno(), b.fileno()])
    message, _ = protocol.receive(ours)
    result = Result.from_message(message)
    assert result.outcome == "refused" and "at most one fd" in result.error
    assert booth.wait(30) == 2
