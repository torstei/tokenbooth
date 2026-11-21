"""The booth draws only through Wayland: on X11 any client can read what is typed into it."""

import os
import socket

import pytest

from tokenbooth import protocol
from tokenbooth.booth import confine_display
from tokenbooth.protocol import Request
from tokenbooth.store import Profile, UsageError


@pytest.fixture
def runtime_dir(tmp_path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(tmp_path / "wayland-1"))
    yield tmp_path
    listener.close()


def test_a_wayland_display_is_kept_and_x11_dropped(runtime_dir):
    env = {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": str(runtime_dir), "DISPLAY": ":0",
           "XAUTHORITY": "/x"}
    confine_display(env)
    assert env == {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": str(runtime_dir), "QT_QPA_PLATFORM": "wayland"}


def test_an_absolute_wayland_display(runtime_dir):
    env = {"WAYLAND_DISPLAY": str(runtime_dir / "wayland-1")}
    confine_display(env)
    assert env["QT_QPA_PLATFORM"] == "wayland"


def test_a_passed_wayland_socket():
    ours, theirs = socket.socketpair()
    env = {"WAYLAND_SOCKET": str(theirs.fileno()), "DISPLAY": ":0"}
    confine_display(env)
    assert env == {"WAYLAND_SOCKET": str(theirs.fileno()), "QT_QPA_PLATFORM": "wayland"}


def test_offscreen_draws_nowhere_and_is_allowed():
    env = {"QT_QPA_PLATFORM": "offscreen", "DISPLAY": ":0"}
    confine_display(env)
    assert env == {"QT_QPA_PLATFORM": "offscreen"}


@pytest.mark.parametrize("env, complaint", [
    ({"DISPLAY": ":0"}, "no Wayland display"),
    ({"QT_QPA_PLATFORM": "xcb", "DISPLAY": ":0"}, "QT_QPA_PLATFORM=xcb"),
    ({"QT_QPA_PLATFORM": "wayland;xcb", "WAYLAND_DISPLAY": "wayland-0"}, "QT_QPA_PLATFORM=wayland;xcb"),
    ({"WAYLAND_DISPLAY": "wayland-9", "XDG_RUNTIME_DIR": "/nonexistent"}, "no Wayland socket"),
    ({"WAYLAND_SOCKET": "not-a-number"}, "not a connected socket"),
])
def test_refused(env, complaint):
    with pytest.raises(UsageError, match=complaint):
        confine_display(env)


def test_a_wayland_socket_that_is_a_pipe_is_refused():
    r, w = os.pipe()
    with pytest.raises(UsageError, match="not a connected socket"):
        confine_display({"WAYLAND_SOCKET": str(r)})
    os.close(r)
    os.close(w)


def test_the_booth_refuses_on_x11_before_starting_qt():
    env = {k: v for k, v in os.environ.items()
           if k not in ("WAYLAND_DISPLAY", "WAYLAND_SOCKET", "QT_QPA_PLATFORM")}
    env["DISPLAY"] = ":0"
    profile = Profile(name="t", start_url="https://example.invalid/", cookie="t", cookie_domain="example.invalid",
                      allowed_hosts=["example.invalid"], timeout=1)
    result = protocol.run_booth(Request(mode="login", profile=profile), env=env)
    assert result.outcome == "refused"
    assert "no Wayland display" in result.error
