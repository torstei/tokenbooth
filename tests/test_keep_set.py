"""Seeding and harvesting the keep-set through a real QtWebEngine cookie store, offscreen.

The seed must reach the store before the first navigation, with host-only cookies (and so
`__Host-` cookies) staying host-only; the harvest must contain exactly the kept cookies, and
one run's harvest must seed the next. Each run is its own process, as a booth is.
"""

import base64
import json
import os
import subprocess
import sys

import pytest

PATTERNS = ["*@google.com", "*@*.google.com"]


def run_until_seeded(seed: list[dict]) -> dict:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    result = subprocess.run([sys.executable, __file__], input=json.dumps(seed), env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.splitlines()[-1])


def entry(raw: bytes, name: str, domain: str) -> dict:
    return {"name": name, "domain": domain, "path": "/", "expires": None, "raw": base64.b64encode(raw).decode()}


def test_seeded_cookies_are_accepted_before_the_first_navigation_and_harvested():
    pytest.importorskip("PySide6.QtWebEngineCore")
    seed = [
        entry(b"SID=a; secure; HttpOnly; path=/; domain=.google.com", "SID", ".google.com"),
        entry(b"__Host-GAPS=b; secure; HttpOnly; path=/; domain=accounts.google.com",
              "__Host-GAPS", "accounts.google.com"),
        entry(b"OTHER=c; secure; path=/; domain=.example.com", "OTHER", ".example.com"),
        entry(b"token=old; secure; path=/; domain=.gopro.com", "token", ".gopro.com"),
    ]
    first = run_until_seeded(seed)
    assert first["unconfirmed"] == [], "start URL loaded before every seeded cookie was confirmed"
    kept = {(e["name"], e["domain"]) for e in first["kept"]}
    assert kept == {("SID", ".google.com"), ("__Host-GAPS", "accounts.google.com")}

    second = run_until_seeded(first["kept"])
    assert second["unconfirmed"] == []
    assert {(e["name"], e["domain"]) for e in second["kept"]} == kept


def main() -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from tokenbooth import browser
    from tokenbooth.store import Profile

    app = QApplication([sys.argv[0]])
    profile = Profile(name="t", start_url="https://example.invalid/", cookie="token",
                      cookie_domain="gopro.com", keep_cookies=PATTERNS)
    session = browser.Session(app, profile, json.load(sys.stdin))
    loaded = []
    session.load_start_url = lambda: (loaded.append(sorted(session.unconfirmed)), QTimer.singleShot(0, app.quit))
    QTimer.singleShot(5000, app.quit)
    session.start()
    app.exec()
    unconfirmed = loaded[0] if loaded else ["(never loaded)"]
    print(json.dumps({"unconfirmed": unconfirmed, "kept": session.keep_set(), "token": session.outcome.token}))
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
