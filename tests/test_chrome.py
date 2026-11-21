"""The Chrome engine, headless, through the booth protocol. Nothing here needs the network."""

import base64
import os
import socket
import subprocess
import sys

import pytest

from tokenbooth import protocol
from tokenbooth.discover import Recorder
from tokenbooth.engines import chrome
from tokenbooth.protocol import Request
from tokenbooth.store import Profile

pytestmark = pytest.mark.skipif(not os.access(chrome.CHROME, os.X_OK), reason="Google Chrome is not installed")

GOOGLE = ["*@google.com", "*@*.google.com"]


def profile(**overrides) -> Profile:
    values = dict(name="t", start_url="https://example.invalid/", allowed_hosts=["example.invalid"],
                  cookie="token", cookie_domain="example.invalid", keep_cookies=GOOGLE, timeout=3)
    values.update(overrides)
    return Profile(**values)


def cookie(name: str, domain: str, value: str = "secret-value") -> dict:
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": -1, "httpOnly": True,
            "secure": True, "session": True, "sameSite": "Lax", "priority": "Medium"}


SEED = [chrome.keep_entry(cookie("SID", ".google.com")),
        chrome.keep_entry(cookie("__Host-GAPS", "accounts.google.com")),
        chrome.keep_entry(cookie("OTHER", ".example.com"))]
QT_ENTRY = {"name": "HSID", "domain": ".google.com", "path": "/", "expires": None,
            "raw": base64.b64encode(b"HSID=x; secure; HttpOnly; path=/; domain=.google.com").decode()}


def headless() -> dict:
    return dict(os.environ, QT_QPA_PLATFORM="offscreen")


def run(request: Request) -> tuple[protocol.Result, list[str]]:
    logs: list[str] = []
    return protocol.run_booth(request, logs.append, env=headless(), engine="chrome"), logs


def test_the_seeded_keep_set_comes_back_host_only_cookies_included():
    result, logs = run(Request(mode="login", profile=profile(), keep=SEED + [QT_ENTRY], log=True))
    assert result.outcome == "timed_out"
    assert {(e["name"], e["domain"]) for e in result.keep} == {("SID", ".google.com"),
                                                              ("__Host-GAPS", "accounts.google.com")}
    assert "kept cookie from another engine, not seeded: HSID @ .google.com" in logs
    assert not any(line.startswith("kept cookie not accepted") for line in logs)

    again, _ = run(Request(mode="login", profile=profile(), keep=result.keep))
    assert {(e["name"], e["domain"]) for e in again.keep} == {("SID", ".google.com"),
                                                             ("__Host-GAPS", "accounts.google.com")}


def test_a_top_level_navigation_outside_the_allowlist_is_blocked():
    _, logs = run(Request(mode="login", profile=profile(allowed_hosts=["other.invalid"], timeout=2), log=True))
    assert "BLOCKED  https://example.invalid/" in logs
    assert not any(line.startswith("navigate ") for line in logs)


def test_discover_reports_cookies_without_values():
    result, _ = run(Request(mode="discover", profile=profile(cookie=""), keep=SEED))
    recorder = Recorder.from_cookies(result.cookies)
    assert {(s.name, s.domain) for s in recorder.cookies.values()} == {("SID", ".google.com"),
                                                                       ("__Host-GAPS", "accounts.google.com")}
    assert "secret-value" not in str(result.cookies)


def test_the_booth_and_its_chrome_quit_when_the_caller_hangs_up(tmp_path):
    ours, theirs = socket.socketpair()
    booth = subprocess.Popen([sys.executable, "-I", "-m", "tokenbooth.booth", "--engine", "chrome",
                              str(theirs.fileno())], pass_fds=[theirs.fileno()], env={**headless(), "TMPDIR": str(tmp_path)},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    theirs.close()
    protocol.send(ours, Request(mode="login", profile=profile(timeout=0), log=True).to_message())
    message, _ = protocol.receive(ours)
    assert message["type"] == "log"
    ours.close()
    assert booth.wait(30) == 0
    assert subprocess.run(["pgrep", "-f", str(tmp_path)], capture_output=True).returncode == 1
    assert list(tmp_path.iterdir()) == []


def test_a_cookie_given_a_domain_stays_a_domain_cookie_and_a_host_only_one_goes_by_url():
    assert chrome.cookie_param(cookie("SID", ".google.com"))["domain"] == ".google.com"
    host_only = chrome.cookie_param(cookie("__Host-GAPS", "accounts.google.com"))
    assert host_only["url"] == "https://accounts.google.com/" and "domain" not in host_only
