"""The locked-down login window."""

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from tokenbooth.store import Profile, UsageError, host_matches, private_dir, session_path


def harden_environment() -> None:
    if os.environ.get("QTWEBENGINE_REMOTE_DEBUGGING"):
        raise UsageError("QTWEBENGINE_REMOTE_DEBUGGING is set: it would give every local process the session")
    os.environ.pop("QTWEBENGINE_CHROMIUM_FLAGS", None)


harden_environment()

import shiboken6  # noqa: E402
from PySide6.QtCore import Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtWebEngineCore import (  # noqa: E402
    QWebEnginePage,
    QWebEnginePermission,
    QWebEngineProfile,
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


@dataclass
class Outcome:
    token: str | None = None
    expires: float | None = None
    timed_out: bool = False


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
    def __init__(self, app: QApplication, profile: Profile, home: Path, verbose: bool):
        self.app = app
        self.profile = profile
        self.verbose = verbose
        self.outcome = Outcome()
        self.windows: list[LoginWindow] = []
        self.closed: list[LoginWindow] = []
        self.pending: tuple[str, float | None] | None = None
        self.main_url = QUrl()
        self.done_url = re.compile(profile.done_url) if profile.done_url else None

        if profile.persist_session:
            storage = private_dir(session_path(home, profile.name))
            self.qprofile = QWebEngineProfile(f"tokenbooth-{profile.name}")
            self.qprofile.setPersistentStoragePath(str(storage))
            self.qprofile.setCachePath(str(storage / "cache"))
            self.qprofile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        else:
            self.qprofile = QWebEngineProfile()
        self.qprofile.setPersistentPermissionsPolicy(QWebEngineProfile.PersistentPermissionsPolicy.AskEveryTime)

        user_agent = profile.user_agent or re.sub(r"\s*QtWebEngine/\S+", "", self.qprofile.httpUserAgent())
        self.qprofile.setHttpUserAgent(user_agent)
        self.log(f"user agent {user_agent}")
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
        self.open_window(popup=False).page.load(QUrl(self.profile.start_url))

    def wanted(self, cookie) -> bool:
        if bytes(cookie.name()).decode(errors="replace") != self.profile.cookie:
            return False
        domain = cookie.domain().lstrip(".").lower()
        want = self.profile.cookie_domain.lstrip(".").lower()
        return domain == want or domain.endswith("." + want)

    def cookie_added(self, cookie) -> None:
        self.log(f"cookie set {bytes(cookie.name()).decode(errors='replace')} @ {cookie.domain()}")
        if not self.wanted(cookie):
            return
        value = bytes(cookie.value()).decode(errors="replace")
        expires = None if cookie.isSessionCookie() else cookie.expirationDate().toSecsSinceEpoch()
        self.pending = (value, expires)
        self.check_done()

    def cookie_removed(self, cookie) -> None:
        if self.wanted(cookie) and self.pending and self.pending[0] == bytes(cookie.value()).decode(errors="replace"):
            self.log("captured cookie removed again")
            self.pending = None

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


def run(profile: Profile, home: Path, verbose: bool) -> Outcome:
    app = QApplication([sys.argv[0]])
    app.setApplicationName("tokenbooth")
    app.setDesktopFileName("tokenbooth")
    session = Session(app, profile, home, verbose)
    session.start()
    app.exec()
    outcome = session.outcome
    session.dispose()
    return outcome
