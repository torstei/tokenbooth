"""The booth's engine: Google Chrome, an app window driven over --remote-debugging-pipe."""

import base64
import fcntl
import json
import os
import queue
import re
import select
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from tokenbooth.discover import Recorder
from tokenbooth.engines import Outcome
from tokenbooth.store import Profile, UsageError, host_matches, keep_matches

CHROME = "/opt/google/chrome/chrome"
SWITCHES = [
    "--remote-debugging-pipe",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--disable-sync",
    "--class=tokenbooth",
    # driven over the pipe, Chrome tells every page it is automated, and Google refuses sign-in
    "--disable-blink-features=AutomationControlled",
    "--disable-features=EnableBoundSessionCredentials,kEnableBoundSessionCredentials",
]
# Chrome's account sign-in presents a kept session as signed out to a Chrome holding no Google
# token of its own; the mode is fixed at startup from the second key.
PREFERENCES = {
    "signin": {"allowed": False, "allowed_on_next_startup": False},
    "credentials_enable_service": False,
    "credentials_enable_autosignin": False,
    "profile": {"password_manager_enabled": False},
    "autofill": {"profile_enabled": False, "credit_card_enabled": False},
}
DENIED_PERMISSIONS = ["geolocation", "notifications", "camera", "microphone", "midi", "clipboard-read",
                      "display-capture", "idle-detection", "local-fonts", "window-management", "storage-access"]
POLL_SECONDS = 0.25
PAGE_VIEW = ("JSON.stringify({webdriver: navigator.webdriver,"
             " brands: navigator.userAgentData && navigator.userAgentData.brands.map(b => b.brand + ' ' + b.version)})")


def blank_page(name: str) -> str:
    title = f"TOKEN BOOTH · {name}".replace("<", "")
    return f"data:text/html;charset=utf-8,<title>{title}</title><body style='background:%23c2185b'></body>"


def loggable(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.netloc else parts.scheme + ":"


def high_fd(fd: int, inheritable: bool) -> int:
    new = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.set_inheritable(new, inheritable)
    return new


class DevTools:
    """The DevTools protocol over Chrome's pipe: NUL-terminated JSON, flat sessions."""

    def __init__(self, to_chrome: int, from_chrome: int):
        self.out = os.fdopen(to_chrome, "wb", buffering=0)
        self.inp = os.fdopen(from_chrome, "rb", buffering=0)
        self.next_id = 0
        self.lock = threading.Lock()
        self.pending: dict[int, list] = {}
        self.events: queue.Queue = queue.Queue()
        self.closed = False
        threading.Thread(target=self.read, daemon=True).start()

    def read(self) -> None:
        buffer = b""
        try:
            while chunk := self.inp.read(1 << 16):
                buffer += chunk
                *messages, buffer = buffer.split(b"\0")
                for raw in messages:
                    message = json.loads(raw)
                    if "id" in message:
                        if slot := self.pending.pop(message["id"], None):
                            slot.append(message)
                            slot[0].set()
                    else:
                        self.events.put(message)
        except (OSError, ValueError):
            pass
        self.closed = True
        self.events.put(None)
        for slot in list(self.pending.values()):
            slot.append({"error": {"message": "Chrome has exited"}})
            slot[0].set()

    def post(self, method: str, params: dict | None = None, session: str | None = None) -> list:
        with self.lock:
            self.next_id += 1
            slot = [threading.Event()]
            self.pending[self.next_id] = slot
            message = {"id": self.next_id, "method": method, "params": params or {}}
            if session:
                message["sessionId"] = session
            try:
                self.out.write(json.dumps(message).encode() + b"\0")
            except OSError:
                self.pending.pop(self.next_id, None)
                slot.append({"error": {"message": "Chrome has exited"}})
                slot[0].set()
        return slot

    def call(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 30) -> dict:
        slot = self.post(method, params, session)
        if not slot[0].wait(timeout):
            raise RuntimeError(f"{method}: no reply")
        if "error" in slot[1]:
            raise RuntimeError(f"{method}: {slot[1]['error'].get('message')}")
        return slot[1].get("result", {})


def keep_entry(cookie: dict) -> dict:
    return {"name": cookie["name"], "domain": cookie["domain"], "path": cookie["path"],
            "expires": None if cookie.get("session") else cookie.get("expires"),
            "raw": base64.b64encode(json.dumps(cookie).encode()).decode()}


def cookie_param(cookie: dict) -> dict:
    """A cookie as Storage.getCookies gives it, as Storage.setCookies takes it. A host-only
    cookie is given by URL: given a domain it becomes a domain cookie, which `__Host-` refuses."""
    param = {k: cookie[k] for k in ("name", "value", "path", "secure", "httpOnly", "sameSite", "priority",
                                    "sourceScheme", "sourcePort", "partitionKey") if k in cookie}
    if not cookie.get("session") and cookie.get("expires", -1) > 0:
        param["expires"] = cookie["expires"]
    if cookie["domain"].startswith("."):
        param["domain"] = cookie["domain"]
    else:
        param["url"] = f"https://{cookie['domain']}{cookie.get('path') or '/'}"
    return param


class Booth:
    def __init__(self, profile: Profile, keep: list[dict], log: Callable[[str], None] | None,
                 recorder: Recorder | None, hangup: int | None, offscreen: bool, data_dir: str):
        self.profile = profile
        self.seed = keep
        self.logger = log
        self.recorder = recorder
        self.hangup = hangup
        self.offscreen = offscreen
        self.data_dir = data_dir
        self.outcome = Outcome()
        self.done_url = re.compile(profile.done_url) if profile.done_url else None
        self.sessions: dict[str, str] = {}
        self.main_target: str | None = None
        self.main_session: str | None = None
        self.main_url = ""
        self.cookies: dict[tuple[str, str, str], dict] = {}
        self.finished = False

    def log(self, message: str) -> None:
        if self.logger:
            self.logger(message)

    def wanted(self, cookie: dict) -> bool:
        if cookie["name"] != self.profile.cookie:
            return False
        domain = cookie["domain"].lstrip(".").lower()
        want = self.profile.cookie_domain.lstrip(".").lower()
        return domain == want or domain.endswith("." + want)

    def keepable(self, cookie: dict) -> bool:
        return not self.wanted(cookie) and keep_matches(self.profile.keep_cookies, cookie["name"], cookie["domain"])

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.scheme == "https" and any(host_matches(parts.hostname or "", h)
                                               for h in self.profile.allowed_hosts)

    def start_chrome(self) -> subprocess.Popen:
        if not os.access(CHROME, os.X_OK):
            raise UsageError(f"Google Chrome is not installed at {CHROME}")
        os.makedirs(f"{self.data_dir}/Default")
        Path(f"{self.data_dir}/Default/Preferences").write_text(json.dumps(PREFERENCES))
        env = {"HOME": self.data_dir, "XDG_RUNTIME_DIR": self.data_dir, "PATH": "/usr/bin:/bin",
               "LANG": os.environ.get("LANG", "C.UTF-8")}
        args = [CHROME, f"--user-data-dir={self.data_dir}", *SWITCHES]
        keep_open = []
        if self.offscreen:
            args += ["--headless", blank_page(self.profile.name)]
        else:
            args += ["--ozone-platform=wayland", f"--app={blank_page(self.profile.name)}"]
            if "WAYLAND_SOCKET" in os.environ:
                wayland = high_fd(int(os.environ["WAYLAND_SOCKET"]), inheritable=True)
                env["WAYLAND_SOCKET"] = str(wayland)
                keep_open.append(wayland)
            else:
                display = os.environ["WAYLAND_DISPLAY"]
                env["WAYLAND_DISPLAY"] = display if display.startswith("/") else \
                    os.path.join(os.environ["XDG_RUNTIME_DIR"], display)

        to_chrome_r, to_chrome_w = os.pipe()
        from_chrome_r, from_chrome_w = os.pipe()
        child_in, child_out = high_fd(to_chrome_r, False), high_fd(from_chrome_w, False)
        parent_out, parent_in = high_fd(to_chrome_w, False), high_fd(from_chrome_r, False)
        for fd in (to_chrome_r, to_chrome_w, from_chrome_r, from_chrome_w):
            os.close(fd)

        def pipe_on_3_and_4() -> None:
            os.dup2(child_in, 3)
            os.dup2(child_out, 4)

        with open(f"{self.data_dir}/chrome.log", "wb") as chrome_log:
            chrome = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=chrome_log, pass_fds=(3, 4, *keep_open), preexec_fn=pipe_on_3_and_4)
        for fd in (child_in, child_out, *keep_open):
            os.close(fd)
        self.cdp = DevTools(parent_out, parent_in)
        return chrome

    def prepare(self) -> None:
        version = self.cdp.call("Browser.getVersion")
        self.log(f"engine {version['product']}, user agent {self.profile.user_agent or version['userAgent']}")
        self.cdp.call("Browser.setDownloadBehavior", {"behavior": "deny"})
        for name in DENIED_PERMISSIONS:
            try:
                self.cdp.call("Browser.setPermission", {"permission": {"name": name}, "setting": "denied"})
            except RuntimeError as e:
                self.log(f"could not deny {name}: {e}")
        self.seed_keep_set()
        self.cdp.call("Target.setDiscoverTargets", {"discover": True})
        self.cdp.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})

    def seed_keep_set(self) -> None:
        params = []
        for entry in self.seed:
            try:
                cookie = json.loads(base64.b64decode(entry["raw"]))
                readable = isinstance(cookie, dict) and {"name", "value", "domain"} <= set(cookie)
            except ValueError:
                readable = False
            if not readable:
                self.log(f"kept cookie from another engine, not seeded: {entry['name']} @ {entry['domain']}")
            elif self.keepable(cookie):
                params.append(cookie_param(cookie))
        if not params:
            return
        self.cdp.call("Storage.setCookies", {"cookies": params})
        present = {(c["name"], c["domain"]) for c in self.cdp.call("Storage.getCookies")["cookies"]}
        self.log(f"seeded {len(params)} kept cookie(s)")
        for p in params:
            domain = p.get("domain") or urlsplit(p["url"]).hostname
            if (p["name"], domain) not in present:
                self.log(f"kept cookie not accepted: {p['name']} @ {domain}")

    def poll(self) -> None:
        cookies = self.cdp.call("Storage.getCookies", timeout=5)["cookies"]
        now = {(c["name"], c["domain"], c["path"]): c for c in cookies}
        for key, cookie in now.items():
            old = self.cookies.get(key)
            if old is None or old["value"] != cookie["value"]:
                self.log(f"cookie set {cookie['name']} @ {cookie['domain']}")
                if self.recorder:
                    self.recorder.cookie_set(*key, cookie["value"], http_only=cookie.get("httpOnly", False),
                                             secure=cookie.get("secure", False), session=cookie.get("session", True))
        if self.recorder:
            for key in self.cookies.keys() - now.keys():
                self.recorder.cookie_removed(*key)
        self.cookies = now
        if self.done_url and not self.done_url.search(self.main_url):
            return
        for cookie in cookies:
            if self.wanted(cookie):
                self.outcome.token = cookie["value"]
                self.outcome.expires = None if cookie.get("session") else cookie.get("expires")
                self.log(f"captured {cookie['name']}")
                self.finished = True

    def attached(self, params: dict) -> None:
        info, session = params["targetInfo"], params["sessionId"]
        if info["type"] == "page":
            self.sessions[session] = info["targetId"]
            self.cdp.call("Fetch.enable", {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]},
                          session)
            if self.profile.user_agent:
                self.cdp.call("Emulation.setUserAgentOverride", {"userAgent": self.profile.user_agent}, session)
            if self.main_target is None:
                self.main_target, self.main_session = info["targetId"], session
                if self.logger:
                    self.cdp.call("Page.enable", None, session)
                # replies only once the navigation commits, which needs the event loop free
                self.cdp.post("Page.navigate", {"url": self.profile.start_url}, session)
            else:
                self.log("popup opened")
        if params.get("waitingForDebugger"):
            self.cdp.post("Runtime.runIfWaitingForDebugger", None, session)

    def request_paused(self, session: str, params: dict) -> None:
        url = params["request"]["url"]
        if params.get("frameId") != self.sessions.get(session):
            self.cdp.post("Fetch.continueRequest", {"requestId": params["requestId"]}, session)
        elif self.allowed(url):
            self.log(f"navigate {loggable(url)}")
            if self.recorder and session == self.main_session:
                self.poll()
                parts = urlsplit(url)
                self.recorder.navigated(parts.hostname or "", parts.path)
            self.cdp.post("Fetch.continueRequest", {"requestId": params["requestId"]}, session)
        else:
            self.log(f"BLOCKED  {loggable(url)}")
            self.cdp.post("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"},
                          session)

    def window_closed(self) -> None:
        self.finished = True
        if self.recorder:
            self.outcome.confirmed = True

    def handle(self, event: dict) -> None:
        method, params = event.get("method"), event.get("params", {})
        if method == "Target.attachedToTarget":
            self.attached(params)
        elif method == "Fetch.requestPaused":
            self.request_paused(event["sessionId"], params)
        elif method == "Target.targetInfoChanged":
            if params["targetInfo"]["targetId"] == self.main_target:
                self.main_url = params["targetInfo"]["url"]
        elif method == "Target.targetDestroyed":
            if params["targetId"] == self.main_target:
                self.window_closed()
        elif method == "Target.detachedFromTarget":
            self.sessions.pop(params["sessionId"], None)
        elif method == "Page.loadEventFired" and event.get("sessionId") == self.main_session:
            try:
                seen = self.cdp.call("Runtime.evaluate", {"expression": PAGE_VIEW, "returnByValue": True},
                                     self.main_session, timeout=5)
                self.log(f"page sees {seen.get('result', {}).get('value')}")
            except RuntimeError:
                pass

    def hung_up(self) -> bool:
        return self.hangup is not None and bool(select.select([self.hangup], [], [], 0)[0])

    def loop(self) -> None:
        deadline = time.monotonic() + self.profile.timeout if self.profile.timeout > 0 else None
        next_poll = 0.0
        while not self.finished:
            try:
                event = self.cdp.events.get(timeout=POLL_SECONDS)
            except queue.Empty:
                event = {}
            if event is None:
                self.window_closed()
                break
            if event:
                self.handle(event)
            if self.hung_up():
                self.log("caller hung up")
                break
            if deadline and time.monotonic() >= deadline:
                self.outcome.timed_out = True
                break
            if not self.finished and time.monotonic() >= next_poll:
                next_poll = time.monotonic() + POLL_SECONDS
                self.poll()

    def keep_set(self) -> list[dict]:
        return [keep_entry(c) for _, c in sorted(self.cookies.items()) if self.keepable(c)]


def run(profile: Profile, keep: list[dict], log: Callable[[str], None] | None = None,
        recorder: Recorder | None = None, hangup: int | None = None,
        offscreen: bool = False) -> tuple[Outcome, list[dict]]:
    """One login. When `hangup` becomes readable, the caller has gone and the run ends."""
    data_dir = tempfile.mkdtemp(prefix="tokenbooth-chrome-")
    booth = Booth(profile, keep, log, recorder, hangup, offscreen, data_dir)
    chrome = None
    try:
        chrome = booth.start_chrome()
        try:
            booth.prepare()
            booth.loop()
            if not booth.cdp.closed:
                booth.poll()
        except RuntimeError as e:
            if not booth.cdp.closed:
                raise UsageError(f"Chrome: {e}") from None
        if booth.main_target is None and booth.cdp.closed:
            status = chrome.wait()
            tail = Path(f"{data_dir}/chrome.log").read_text(errors="replace").splitlines()[-5:]
            raise UsageError(f"Chrome exited before opening its window (status {status}):\n" + "\n".join(tail))
        try:
            booth.cdp.call("Browser.close", timeout=5)
        except RuntimeError:
            pass
        return booth.outcome, booth.keep_set()
    finally:
        if chrome:
            try:
                chrome.wait(10)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait()
        shutil.rmtree(data_dir, ignore_errors=True)
