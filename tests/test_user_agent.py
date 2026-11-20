"""What a page learns about the browser, through a real QtWebEngine profile, offscreen.

A booth claiming a browser other than Chromium must not contradict itself: no
`navigator.userAgentData`, in subframes too, and no high-entropy client hints even when a site
asks for them with `Accept-CH`. QtWebEngine 6.12 still sends the low-entropy `Sec-CH-UA`,
`-Mobile` and `-Platform` headers; it can blank them but not drop them. With Chromium's own
user agent everything stays. Each case is its own process, as a booth is.
"""

import json
import os
import subprocess
import sys

import pytest

FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0"


def page_view(user_agent: str) -> dict:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    result = subprocess.run([sys.executable, __file__, user_agent], env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.splitlines()[-1])


def test_a_firefox_user_agent_comes_without_client_hints():
    pytest.importorskip("PySide6.QtWebEngineCore")
    seen = page_view(FIREFOX)
    assert seen["user_agent_header"] == FIREFOX
    assert "sec-ch-ua-full-version-list" not in seen["frame_client_hint_headers"]
    assert seen["js"] == {"ua": FIREFOX, "hasUAData": False, "frameHasUAData": False}


def test_chromiums_own_user_agent_keeps_them():
    pytest.importorskip("PySide6.QtWebEngineCore")
    seen = page_view("")
    assert "sec-ch-ua-full-version-list" in seen["frame_client_hint_headers"]
    assert seen["js"]["hasUAData"] and seen["js"]["frameHasUAData"]


def main(user_agent: str) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWidgets import QApplication

    from tokenbooth import browser
    from tokenbooth.store import Profile

    headers = {}
    frame_headers = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                headers.update({k.lower(): v for k, v in self.headers.items()})
                body = b"<iframe src='/frame'></iframe>"
            else:
                frame_headers.update({k.lower(): v for k, v in self.headers.items()})
                body = b"<p>frame</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Accept-CH", "Sec-CH-UA-Full-Version-List, Sec-CH-UA-Platform-Version")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    app = QApplication([sys.argv[0]])
    profile = Profile(name="t", start_url="https://example.invalid/", cookie="token",
                      cookie_domain="example.invalid", user_agent=user_agent)
    session = browser.Session(app, profile, [], verbose=False)
    page = QWebEnginePage(session.qprofile)
    result = {}

    def report(js):
        result["js"] = json.loads(js) if js else None
        app.quit()

    def loaded(ok):
        QTimer.singleShot(500, lambda: page.runJavaScript(
            "(() => { try { return JSON.stringify({ua: navigator.userAgent, hasUAData: 'userAgentData' in navigator,"
            " frameHasUAData: 'userAgentData' in document.querySelector('iframe').contentWindow.navigator}); }"
            " catch (e) { return JSON.stringify({error: String(e)}); } })()",
            0, report))

    page.loadFinished.connect(loaded)
    page.load(QUrl(f"http://localhost:{server.server_port}/"))
    QTimer.singleShot(15000, app.quit)
    app.exec()
    print(json.dumps({
        "user_agent_header": headers.get("user-agent"),
        "frame_client_hint_headers": sorted(k for k in frame_headers if k.startswith("sec-ch-ua")),
        "js": result.get("js"),
    }))
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1])
