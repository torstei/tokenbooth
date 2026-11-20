"""The locked-down login window."""

import base64
import os
import re
import sys
from dataclasses import dataclass

from tokenbooth.discover import Recorder
from tokenbooth.store import Profile, UsageError, host_matches, keep_matches


def harden_environment() -> None:
    if os.environ.get("QTWEBENGINE_REMOTE_DEBUGGING"):
        raise UsageError("QTWEBENGINE_REMOTE_DEBUGGING is set: it would give every local process the session")
    os.environ.pop("QTWEBENGINE_CHROMIUM_FLAGS", None)


harden_environment()

import shiboken6  # noqa: E402
from PySide6.QtCore import Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtNetwork import QNetworkCookie  # noqa: E402
from PySide6.QtWebEngineCore import (  # noqa: E402
    QWebEnginePage,
    QWebEnginePermission,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineSettings,
)
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

ACCENT = "#c2185b"
ACCENT_TEXT = "#ffffff"


SEED_TIMEOUT_MS = 3000

# Only Chromium-based browsers have client hints; a page that sees them next to another
# browser's user agent sees a contradiction.
HIDE_USER_AGENT_DATA = "delete Navigator.prototype.userAgentData;"


@dataclass
class Outcome:
    token: str | None = None
    expires: float | None = None
    timed_out: bool = False
    confirmed: bool = False


def cookie_name(cookie) -> str:
    return bytes(cookie.name()).decode(errors="replace")


def cookie_value(cookie) -> str:
    return bytes(cookie.value()).decode(errors="replace")


def cookie_key(cookie) -> tuple[str, str, str]:
    return cookie_name(cookie), cookie.domain(), cookie.path()


def loggable(url: QUrl) -> str:
    if url.scheme() in ("http", "https"):
        return f"{url.scheme()}://{url.host()}{url.path()}"
    return url.scheme() + ":"


class LockedPage(QWebEnginePage):
    def __init__(self, session: "Session", parent: QWidget):
        super().__init__(session.qprofile, parent)
        self.session = session
        self.permissionRequested.connect(lambda permission: permission.deny())

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if not is_main_frame:
            return True
        if self.session.allowed(url):
            self.session.log(f"navigate {loggable(url)}")
            if self.session.recorder:
                self.session.recorder.navigated(url.host(), url.path())
            return True
        self.session.log(f"BLOCKED  {loggable(url)}")
        self.session.blocked(url)
        return False

    def createWindow(self, window_type):
        return self.session.open_window(popup=True).page

    def javaScriptConsoleMessage(self, level, message, line, source):
        pass


class LoginWindow(QMainWindow):
    def __init__(self, session: "Session", popup: bool):
        super().__init__()
        self.session = session
        self.popup = popup
        name = session.profile.name
        self.setWindowTitle(f"TOKEN BOOTH · {name}" + (" · popup" if popup else ""))

        frame = QWidget()
        frame.setObjectName("frame")
        frame.setStyleSheet(f"#frame {{ background: {ACCENT}; }}")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 0, 6, 6)
        layout.setSpacing(0)

        bar = QWidget()
        bar.setStyleSheet(f"color: {ACCENT_TEXT}; font-size: 11pt;")
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 6, 0, 6)
        row.addWidget(QLabel(f"<b>TOKEN BOOTH</b> &nbsp;·&nbsp; {name}"))
        row.addStretch(1)
        self.host = QLabel()
        self.host.setStyleSheet("font-family: monospace; font-weight: bold;")
        row.addWidget(self.host)
        row.addStretch(1)
        self.status = QLabel()
        row.addWidget(self.status)
        if session.recorder and not popup:
            done = QPushButton("I'm logged in")
            done.clicked.connect(session.confirm_logged_in)
            row.addWidget(done)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.close)
        row.addWidget(cancel)

        self.view = QWebEngineView()
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.page = LockedPage(session, self.view)
        self.view.setPage(self.page)
        self.page.urlChanged.connect(self.url_changed)
        self.page.windowCloseRequested.connect(self.close)
        self.page.loadFinished.connect(self.load_finished)

        layout.addWidget(bar)
        layout.addWidget(self.view, 1)
        self.setCentralWidget(frame)
        if popup:
            self.resize(560, 720)
        else:
            self.resize(1000, 820)

    def url_changed(self, url: QUrl) -> None:
        mark = "🔒" if url.scheme() == "https" else "⚠"
        self.host.setText(f"{mark} {url.host() or url.toString()}")
        if not self.popup:
            self.session.main_url_changed(url)

    def load_finished(self, ok: bool) -> None:
        if self.session.verbose and not self.popup:
            self.page.runJavaScript(
                "JSON.stringify({webdriver: navigator.webdriver,"
                " brands: navigator.userAgentData && navigator.userAgentData.brands})",
                0,
                lambda result: self.session.log(f"page sees {result}"),
            )

    def show_status(self, text: str) -> None:
        self.status.setText(text)

    def closeEvent(self, event) -> None:
        self.session.window_closed(self)
        super().closeEvent(event)


class Session:
    def __init__(self, app: QApplication, profile: Profile, keep: list[dict], verbose: bool,
                 recorder: Recorder | None = None):
        self.app = app
        self.profile = profile
        self.verbose = verbose
        self.recorder = recorder
        self.outcome = Outcome()
        self.seed = keep
        self.unconfirmed: set[tuple[str, str, str]] = set()
        self.kept: dict[tuple[str, str, str], QNetworkCookie] = {}
        self.started = False
        self.windows: list[LoginWindow] = []
        self.closed: list[LoginWindow] = []
        self.pending: tuple[str, float | None] | None = None
        self.main_url = QUrl()
        self.done_url = re.compile(profile.done_url) if profile.done_url else None

        self.qprofile = QWebEngineProfile()
        if not self.qprofile.isOffTheRecord():
            raise UsageError("the browser profile is not off the record")
        self.qprofile.setPersistentPermissionsPolicy(QWebEngineProfile.PersistentPermissionsPolicy.AskEveryTime)

        user_agent = profile.user_agent or re.sub(r"\s*QtWebEngine/\S+", "", self.qprofile.httpUserAgent())
        self.qprofile.setHttpUserAgent(user_agent)
        self.log(f"user agent {user_agent}")
        if "Chrome/" not in user_agent:
            self.qprofile.clientHints().setAllClientHintsEnabled(False)
            script = QWebEngineScript()
            script.setName("hide-user-agent-data")
            script.setSourceCode(HIDE_USER_AGENT_DATA)
            script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
            script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            script.setRunsOnSubFrames(True)
            self.qprofile.scripts().insert(script)
            self.log("client hints off: the user agent is not Chromium's")
        else:
            self.log(f"client hints {self.qprofile.clientHints().fullVersionList()}")

        settings = self.qprofile.settings()
        for attribute, on in [
            (QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, False),
            (QWebEngineSettings.WebAttribute.JavascriptCanPaste, False),
            (QWebEngineSettings.WebAttribute.PluginsEnabled, False),
            (QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, False),
            (QWebEngineSettings.WebAttribute.ScreenCaptureEnabled, False),
            (QWebEngineSettings.WebAttribute.PdfViewerEnabled, False),
            (QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False),
            (QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False),
        ]:
            settings.setAttribute(attribute, on)

        self.qprofile.downloadRequested.connect(lambda item: item.cancel())
        store = self.qprofile.cookieStore()
        store.cookieAdded.connect(self.cookie_added)
        store.cookieRemoved.connect(self.cookie_removed)

        if profile.timeout > 0:
            QTimer.singleShot(profile.timeout * 1000, self.time_out)

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"tokenbooth: {message}", file=sys.stderr, flush=True)

    def allowed(self, url: QUrl) -> bool:
        if url.toString() == "about:blank":
            return True
        if url.scheme() != "https":
            return False
        return any(host_matches(url.host(), pattern) for pattern in self.profile.allowed_hosts)

    def blocked(self, url: QUrl) -> None:
        for window in self.windows:
            window.show_status(f"blocked: {url.host()}")

    def open_window(self, popup: bool) -> LoginWindow:
        window = LoginWindow(self, popup)
        self.windows.append(window)
        window.show()
        return window

    def start(self) -> None:
        self.window = self.open_window(popup=False)
        store = self.qprofile.cookieStore()
        for entry in self.seed:
            for cookie in QNetworkCookie.parseCookies(base64.b64decode(entry["raw"])):
                if not self.keepable(cookie):
                    continue
                origin = QUrl(f"https://{cookie.domain().lstrip('.')}{cookie.path() or '/'}")
                if not cookie.domain().startswith("."):
                    cookie.setDomain("")
                self.unconfirmed.add((cookie_name(cookie), entry["domain"], cookie.path()))
                store.setCookie(cookie, origin)
        if self.unconfirmed:
            self.log(f"seeding {len(self.unconfirmed)} kept cookie(s)")
            QTimer.singleShot(SEED_TIMEOUT_MS, self.load_start_url)
        else:
            self.load_start_url()

    def load_start_url(self) -> None:
        if self.started:
            return
        self.started = True
        for name, domain, _ in sorted(self.unconfirmed):
            self.log(f"kept cookie not accepted: {name} @ {domain}")
        self.window.page.load(QUrl(self.profile.start_url))

    def wanted(self, cookie) -> bool:
        if cookie_name(cookie) != self.profile.cookie:
            return False
        domain = cookie.domain().lstrip(".").lower()
        want = self.profile.cookie_domain.lstrip(".").lower()
        return domain == want or domain.endswith("." + want)

    def keepable(self, cookie) -> bool:
        return not self.wanted(cookie) and keep_matches(self.profile.keep_cookies, cookie_name(cookie), cookie.domain())

    def cookie_added(self, cookie) -> None:
        key = cookie_key(cookie)
        if not self.started and key in self.unconfirmed:
            self.unconfirmed.discard(key)
            if not self.unconfirmed:
                self.load_start_url()
        else:
            self.log(f"cookie set {key[0]} @ {key[1]}")
        if self.recorder:
            self.recorder.cookie_set(*key, cookie_value(cookie), http_only=cookie.isHttpOnly(),
                                     secure=cookie.isSecure(), session=cookie.isSessionCookie())
        if self.keepable(cookie):
            self.kept[key] = QNetworkCookie(cookie)
        if not self.wanted(cookie):
            return
        expires = None if cookie.isSessionCookie() else cookie.expirationDate().toSecsSinceEpoch()
        self.pending = (cookie_value(cookie), expires)
        self.check_done()

    def cookie_removed(self, cookie) -> None:
        key = cookie_key(cookie)
        if self.recorder:
            self.recorder.cookie_removed(*key)
        if key in self.kept and cookie_value(self.kept[key]) == cookie_value(cookie):
            del self.kept[key]
        if self.wanted(cookie) and self.pending and self.pending[0] == cookie_value(cookie):
            self.log("captured cookie removed again")
            self.pending = None

    def keep_set(self) -> list[dict]:
        entries = []
        for (name, domain, path), cookie in sorted(self.kept.items()):
            expires = None if cookie.isSessionCookie() else cookie.expirationDate().toSecsSinceEpoch()
            raw = bytes(cookie.toRawForm(QNetworkCookie.RawForm.Full))
            entries.append({"name": name, "domain": domain, "path": path, "expires": expires,
                            "raw": base64.b64encode(raw).decode()})
        return entries

    def main_url_changed(self, url: QUrl) -> None:
        self.main_url = url
        self.check_done()

    def check_done(self) -> None:
        if self.pending is None or self.outcome.token is not None:
            return
        if self.done_url and not self.done_url.search(self.main_url.toString()):
            self.log(f"have {self.profile.cookie}, waiting for a page matching done_url")
            return
        self.outcome.token, self.outcome.expires = self.pending
        self.log(f"captured {self.profile.cookie}")
        for window in self.windows:
            window.show_status("✓ got it")
        QTimer.singleShot(300, self.app.quit)

    def confirm_logged_in(self) -> None:
        self.outcome.confirmed = True
        self.log("confirmed logged in")
        QTimer.singleShot(0, self.app.quit)

    def time_out(self) -> None:
        self.outcome.timed_out = True
        self.app.quit()

    def window_closed(self, window: LoginWindow) -> None:
        if window in self.windows:
            self.windows.remove(window)
            self.closed.append(window)
        if not window.popup:
            self.app.quit()

    def dispose(self) -> None:
        for window in self.windows + self.closed:
            shiboken6.delete(window)
        self.windows.clear()
        self.closed.clear()
        self.app.processEvents()
        shiboken6.delete(self.qprofile)


def run(profile: Profile, keep: list[dict], verbose: bool,
        recorder: Recorder | None = None) -> tuple[Outcome, list[dict]]:
    app = QApplication([sys.argv[0]])
    app.setApplicationName("tokenbooth")
    app.setDesktopFileName("tokenbooth")
    session = Session(app, profile, keep, verbose, recorder)
    session.start()
    app.exec()
    outcome, kept = session.outcome, session.keep_set()
    session.dispose()
    return outcome, kept
