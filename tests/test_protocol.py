import base64
import json
import os
import socket
import struct
import subprocess
import sys

import pytest

from tokenbooth import protocol
from tokenbooth.discover import Recorder
from tokenbooth.protocol import ProtocolError, Request, Result
from tokenbooth.store import Profile


def pair():
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)


def profile(**overrides) -> Profile:
    values = dict(name="t", start_url="https://example.invalid/", allowed_hosts=["example.invalid"],
                  cookie="token", cookie_domain="example.invalid", keep_cookies=["*@google.com", "*@*.google.com"],
                  timeout=1)
    values.update(overrides)
    return Profile(**values)


def keep_entry(raw: bytes, name: str, domain: str) -> dict:
    return {"name": name, "domain": domain, "path": "/", "expires": None, "raw": base64.b64encode(raw).decode()}


SEED = [
    keep_entry(b"SID=a; secure; HttpOnly; path=/; domain=.google.com", "SID", ".google.com"),
    keep_entry(b"__Host-GAPS=b; secure; HttpOnly; path=/; domain=accounts.google.com",
               "__Host-GAPS", "accounts.google.com"),
]


def test_a_frame_carries_a_passed_fd():
    a, b = pair()
    r, w = os.pipe()
    protocol.send(a, {"type": "request", "n": 1}, [w])
    os.close(w)
    message, fds = protocol.receive(b)
    assert message == {"type": "request", "n": 1}
    assert len(fds) == 1
    os.write(fds[0], b"through")
    os.close(fds[0])
    assert os.read(r, 16) == b"through"


def test_receive_returns_none_on_a_clean_close():
    a, b = pair()
    a.close()
    assert protocol.receive(b) is None


@pytest.mark.parametrize("data, complaint", [
    (struct.pack(">I", protocol.MAX_FRAME + 1), "exceeds"),
    (struct.pack(">I", 10) + b"{}", "inside a frame"),
    (b"\0\0", "inside a frame"),
    (struct.pack(">I", 3) + b"{x}", "not JSON"),
    (struct.pack(">I", 2) + b"[]", "with a type"),
])
def test_receive_refuses_a_bad_frame(data, complaint):
    a, b = pair()
    a.sendall(data)
    a.close()
    with pytest.raises(ProtocolError, match=complaint):
        protocol.receive(b)


def test_send_refuses_an_oversized_frame():
    a, _ = pair()
    with pytest.raises(ProtocolError, match="exceeds"):
        protocol.send(a, {"type": "log", "text": "x" * protocol.MAX_FRAME})


def test_request_round_trip():
    request = Request(mode="login", profile=profile(), keep=SEED, log=True)
    message = json.loads(json.dumps(request.to_message()))
    assert Request.from_message(message) == request


def request_message(**changes) -> dict:
    message = json.loads(json.dumps(Request(mode="login", profile=profile(), keep=SEED).to_message()))
    for path, value in changes.items():
        target, *keys = path.split("__")
        node = message if target == "m" else message[target]
        if target == "keep":
            node = node[0]
        if value is KeyError:
            del node[keys[0]]
        else:
            node[keys[0]] = value
    return message


@pytest.mark.parametrize("changes, complaint", [
    ({"m__version": 2}, "version"),
    ({"m__type": "result"}, "expected a request"),
    ({"m__mode": "steal"}, "unknown mode"),
    ({"m__extra": 1}, "unknown key"),
    ({"m__keep": KeyError}, "lacks keep"),
    ({"profile__start_url": "http://example.invalid/"}, "https"),
    ({"profile__timeout": True}, "wrong type"),
    ({"profile__allowed_hosts": [1]}, "strings"),
    ({"profile__cookie": ""}, "required to log in"),
    ({"profile__via": ["google"]}, "unknown key"),
    ({"keep__raw": KeyError}, "keep\\[0\\] lacks raw"),
])
def test_request_validation(changes, complaint):
    with pytest.raises(ProtocolError, match=complaint):
        Request.from_message(request_message(**changes))


def test_discover_needs_no_cookie():
    message = request_message(m__mode="discover", profile__cookie="")
    assert Request.from_message(message).mode == "discover"


def test_result_round_trip_and_token_rule():
    result = Result("token", token="t", expires=1.5, keep=SEED)
    assert Result.from_message(json.loads(json.dumps(result.to_message()))) == result
    with pytest.raises(ProtocolError, match="only then"):
        Result.from_message({"type": "result", "outcome": "cancelled", "token": "t"})
    with pytest.raises(ProtocolError, match="only then"):
        Result.from_message({"type": "result", "outcome": "token", "token": None})
    with pytest.raises(ProtocolError, match="unknown outcome"):
        Result.from_message({"type": "result", "outcome": "maybe"})


# A real booth, offscreen, through both ends of the protocol.

def offscreen() -> dict:
    pytest.importorskip("PySide6.QtWebEngineCore")
    return dict(os.environ, QT_QPA_PLATFORM="offscreen")


def start_booth(env: dict) -> tuple[socket.socket, subprocess.Popen]:
    ours, theirs = pair()
    booth = subprocess.Popen([sys.executable, "-I", "-m", "tokenbooth.booth", "--engine", "qt",
                              str(theirs.fileno())],
                             pass_fds=[theirs.fileno()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=env)
    theirs.close()
    return ours, booth


def test_booth_times_out_and_returns_the_seeded_keep_set():
    logs = []
    result = protocol.run_booth(Request(mode="login", profile=profile(), keep=SEED, log=True), logs.append,
                                env=offscreen(), engine="qt")
    assert result.outcome == "timed_out"
    assert result.token is None
    assert {(e["name"], e["domain"]) for e in result.keep} == {("SID", ".google.com"),
                                                              ("__Host-GAPS", "accounts.google.com")}
    assert any(text.startswith("user agent ") for text in logs)


def test_booth_in_discover_mode_reports_cookies_without_values():
    result = protocol.run_booth(Request(mode="discover", profile=profile(cookie=""), keep=SEED),
                                env=offscreen(), engine="qt")
    assert result.outcome == "timed_out"
    recorder = Recorder.from_cookies(result.cookies)
    assert {(s.name, s.domain) for s in recorder.cookies.values()} == {("SID", ".google.com"),
                                                                       ("__Host-GAPS", "accounts.google.com")}
    assert all("value" not in entry for entry in result.cookies)


def test_booth_refuses_a_bad_request():
    ours, booth = start_booth(offscreen())
    protocol.send(ours, {**Request(mode="login", profile=profile()).to_message(), "version": 99})
    message, _ = protocol.receive(ours)
    result = Result.from_message(message)
    assert (result.outcome, booth.wait(30)) == ("refused", 2)
    assert "version" in result.error


def test_booth_quits_when_the_caller_hangs_up():
    ours, booth = start_booth(offscreen())
    protocol.send(ours, Request(mode="login", profile=profile(timeout=0), log=True).to_message())
    message, _ = protocol.receive(ours)
    assert message["type"] == "log"
    ours.close()
    assert booth.wait(30) == 0
