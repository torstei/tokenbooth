"""S5 spike, booth side: Google Chrome driven over --remote-debugging-pipe.

Runs as a DynamicUser instance of tokenbooth-spike-chrome@.service, with the accepted socket on
fd 3, or as the user with `client.py --local`, with the socket's fd as the only argument.
Receives a Wayland connection and a JSON request, opens the login in a Chrome app window drawn
through that connection alone, and reports back what it sees and the token, as JSON lines.
"""

import fcntl
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tokenbooth.store import host_matches, keep_matches  # noqa: E402

CHROME = "/opt/google/chrome/chrome"
DENIED_PERMISSIONS = ["geolocation", "notifications", "camera", "microphone", "midi", "clipboard-read",
                      "display-capture", "idle-detection", "local-fonts", "window-management", "storage-access"]
PAGE_VIEW = """JSON.stringify({
  webdriver: navigator.webdriver,
  brands: navigator.userAgentData && navigator.userAgentData.brands.map(b => b.brand + " " + b.version),
  webgl: (() => { const g = document.createElement("canvas").getContext("webgl");
                  const d = g && g.getExtension("WEBGL_debug_renderer_info");
                  return d ? g.getParameter(d.UNMASKED_RENDERER_WEBGL) : String(g); })(),
  plugins: navigator.plugins.length, languages: navigator.languages,
})"""
ATTRIBUTES = ("path", "secure", "httpOnly", "sameSite", "session", "priority", "sourceScheme", "sourcePort",
              "partitionKey", "sameParty")
BLANK = "data:text/html,<title>TOKEN BOOTH</title><body style='background:%23c00'></body>"

conn = socket.socket(fileno=int(sys.argv[1]) if len(sys.argv) > 1 else 3)


def send(kind: str, **fields) -> None:
    conn.sendall((json.dumps({kind: fields} if fields else {kind: True}) + "\n").encode())


def report(text: str) -> None:
    send("log", text=text)


def receive_request() -> tuple[dict, int]:
    data, fds, _, _ = socket.recv_fds(conn, 1 << 20, 1)
    while not data.endswith(b"\n"):
        more = conn.recv(1 << 20)
        if not more:
            break
        data += more
    if len(fds) != 1:
        report(f"FAIL expected one fd, got {len(fds)}")
        sys.exit(1)
    return json.loads(data), fds[0]


def loggable(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.netloc else parts.scheme + ":"


def high_fd(fd: int, inheritable: bool) -> int:
    new = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
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
        threading.Thread(target=self.read, daemon=True).start()

    def read(self) -> None:
        buffer = b""
        while chunk := self.inp.read(1 << 16):
            buffer += chunk
            *messages, buffer = buffer.split(b"\0")
            for raw in messages:
                message = json.loads(raw)
                if "id" in message:
                    slot = self.pending.pop(message["id"])
                    slot.append(message)
                    slot[0].set()
                else:
                    self.events.put(message)
        self.events.put(None)
        for slot in list(self.pending.values()):
            slot.append({"error": {"message": "pipe closed"}})
            slot[0].set()

    def post(self, method: str, params: dict | None = None, session: str | None = None) -> list:
        with self.lock:
            self.next_id += 1
            ident = self.next_id
            slot = [threading.Event()]
            self.pending[ident] = slot
            message = {"id": ident, "method": method, "params": params or {}}
            if session:
                message["sessionId"] = session
            self.out.write(json.dumps(message).encode() + b"\0")
        return slot

    def call(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 30) -> dict:
        slot = self.post(method, params, session)
        if not slot[0].wait(timeout):
            raise RuntimeError(f"{method}: no reply")
        reply = slot[1]
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result", {})


def descendants(root: int) -> list[tuple[int, dict]]:
    status = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            status[int(pid)] = dict(line.split(":", 1) for line in open(f"/proc/{pid}/status").read().splitlines())
        except (OSError, ValueError):
            pass
    family = {root}
    grew = True
    while grew:
        grew = False
        for pid, fields in status.items():
            if pid not in family and int(fields.get("PPid", "0").strip() or 0) in family:
                family.add(pid)
                grew = True
    return [(pid, status[pid]) for pid in sorted(family) if pid in status]


def report_sandbox(chrome_pid: int, data_dir: str) -> None:
    for pid, fields in descendants(chrome_pid):
        nspid = fields.get("NSpid", "").split()
        try:
            title = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()
            label = open(f"/proc/{pid}/attr/current").read().strip()
        except OSError:
            title, label = "", "?"
        if "--remote-debugging-port" in title:
            report(f"FAIL process {pid} has --remote-debugging-port")
        kind = next((w for w in title.split() if w.startswith("--type=")), "browser")
        report(f"process {pid} {kind}: own pid namespace {len(nspid) > 1} (NSpid {' '.join(nspid)}),"
               f" seccomp mode {fields.get('Seccomp', '?').strip()} filters {fields.get('Seccomp_filters', '?').strip()},"
               f" apparmor {label}")
    sockets = set()
    for pid, _ in descendants(chrome_pid):
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
                if target.startswith("socket:["):
                    sockets.add(target[8:-1])
        except OSError:
            pass
    listening = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for row in open(table).read().splitlines()[1:]:
            cols = row.split()
            if cols[3] == "0A" and cols[9] in sockets:
                listening.append(f"{table.rsplit('/', 1)[1]} {cols[1]}")
    report("listening TCP sockets held by chrome: " + (", ".join(listening) or "none"))
    report("DevToolsActivePort file: " + ("present" if os.path.exists(f"{data_dir}/DevToolsActivePort") else "absent"))


def cookie_param(cookie: dict) -> dict:
    """A cookie from Storage.getCookies as Storage.setCookies takes it; a host-only cookie is
    given by URL, since one given a domain becomes a domain cookie and __Host- refuses that."""
    param = {k: cookie[k] for k in ("name", "value", "path", "secure", "httpOnly", "sameSite", "priority",
                                    "sourceScheme", "sourcePort", "partitionKey") if k in cookie}
    if not cookie.get("session") and cookie.get("expires", -1) > 0:
        param["expires"] = cookie["expires"]
    if cookie["domain"].startswith("."):
        param["domain"] = cookie["domain"]
    else:
        param["url"] = f"https://{cookie['domain']}{cookie.get('path', '/')}"
    return param


def main() -> int:
    request, wayland = receive_request()
    wayland = high_fd(wayland, inheritable=True)
    peer = socket.socket(fileno=os.dup(wayland)).getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    peer_pid, peer_uid = int.from_bytes(peer[0:4], "little"), int.from_bytes(peer[4:8], "little")
    report(f"uid {os.getuid()} gid {os.getgid()} wayland peer uid {peer_uid} pid {peer_pid}")
    for path in ("/home", f"/run/user/{peer_uid}"):
        try:
            os.listdir(path)
            report(f"can list {path}")
        except OSError as e:
            report(f"cannot list {path}: {e.strerror}")

    allowed = request["allowed_hosts"]
    data_dir = tempfile.mkdtemp(prefix="booth-chrome-")
    # Chrome's account sync with Google presents a kept session as signed out to a Chrome
    # that holds no Google token of its own; the mode is fixed at startup from the second
    preferences = {"signin": {"allowed": False, "allowed_on_next_startup": False}}
    os.makedirs(f"{data_dir}/Default", exist_ok=True)
    with open(f"{data_dir}/Default/Preferences", "w") as f:
        json.dump(preferences, f)
    to_chrome_r, to_chrome_w = os.pipe()
    from_chrome_r, from_chrome_w = os.pipe()
    child_in, child_out = high_fd(to_chrome_r, False), high_fd(from_chrome_w, False)
    to_chrome_w, from_chrome_r = high_fd(to_chrome_w, False), high_fd(from_chrome_r, False)

    def chrome_fds() -> None:
        os.dup2(child_in, 3)
        os.dup2(child_out, 4)

    env = {"HOME": os.environ.get("HOME", data_dir), "WAYLAND_SOCKET": str(wayland), "LANG": "C.UTF-8",
           "XDG_RUNTIME_DIR": data_dir, "PATH": "/usr/bin:/bin",
           # where the unit cannot hide the Qt shim, its libraries are made unloadable instead
           **({"LD_LIBRARY_PATH": os.environ["S5_NOQT"]} if "S5_NOQT" in os.environ else {})}
    args = [CHROME, "--ozone-platform=wayland", "--remote-debugging-pipe", f"--user-data-dir={data_dir}",
            "--no-first-run", "--no-default-browser-check", "--password-store=basic", "--class=tokenbooth",
            "--disable-sync", "--disable-blink-features=AutomationControlled",
            "--disable-features=EnableBoundSessionCredentials,kEnableBoundSessionCredentials", f"--app={BLANK}"]
    chrome = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, pass_fds=(3, 4, wayland),
                              preexec_fn=chrome_fds, close_fds=True)
    for fd in (child_in, child_out, wayland):
        os.close(fd)
    report(f"chrome pid {chrome.pid}")

    cdp = DevTools(to_chrome_w, from_chrome_r)
    token, expires, keep = None, None, []
    try:
        version = cdp.call("Browser.getVersion")
        report(f"product {version['product']}, user agent {version['userAgent']}")
        cdp.call("Browser.setDownloadBehavior", {"behavior": "deny"})
        refused = []
        for name in DENIED_PERMISSIONS:
            try:
                cdp.call("Browser.setPermission", {"permission": {"name": name}, "setting": "denied"})
            except RuntimeError as e:
                refused.append(f"{name} ({e})")
        report("permissions denied" + (f"; could not deny: {', '.join(refused)}" if refused else ""))
        if request.get("keep"):
            cdp.call("Storage.setCookies", {"cookies": [cookie_param(c) for c in request["keep"]]})
            report(f"seeded {len(request['keep'])} kept cookie(s)")
            present = {(c["name"], c["domain"]) for c in cdp.call("Storage.getCookies")["cookies"]}
            missing = [f"{c['name']}@{c['domain']}" for c in request["keep"] if (c["name"], c["domain"]) not in present]
            report("seeded cookies missing afterwards: " + (" ".join(missing) or "none"))
        cdp.call("Target.setDiscoverTargets", {"discover": True})
        cdp.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})

        sessions: dict[str, str] = {}
        request_url: dict[str, str] = {}
        dbsc_seen: set = set()
        page_url: dict[str, str] = {}
        main_target = None
        deadline = time.monotonic() + request["timeout"] if request.get("timeout") else None
        probe_at = None
        last_poll = 0.0
        while True:
            try:
                event = cdp.events.get(timeout=0.25)
            except queue.Empty:
                event = ""
            if event is None:
                report("chrome exited: window closed")
                break
            method = event.get("method") if event else None
            params = event.get("params", {}) if event else {}
            if method == "Target.attachedToTarget":
                info, session = params["targetInfo"], params["sessionId"]
                if info["type"] == "page":
                    sessions[session] = info["targetId"]
                    cdp.call("Fetch.enable", {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]},
                             session)
                    cdp.call("Network.enable", None, session)
                    report(f"page {info['targetId'][:8]} attached at {loggable(info['url'])}")
                    if main_target is None:
                        main_target = info["targetId"]
                        # replies only once the navigation commits, which needs this loop free
                        cdp.post("Page.navigate", {"url": request["start_url"]}, session)
                        probe_at = time.monotonic() + 8
                if params.get("waitingForDebugger"):
                    cdp.call("Runtime.runIfWaitingForDebugger", None, session)
            elif method == "Fetch.requestPaused":
                session = event["sessionId"]
                url = params["request"]["url"]
                top = params.get("frameId") == sessions.get(session)
                parts = urlsplit(url)
                if top and not (parts.scheme == "https" and any(host_matches(parts.hostname or "", h) for h in allowed)):
                    report(f"BLOCKED {loggable(url)}")
                    cdp.call("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"},
                             session)
                else:
                    cdp.call("Fetch.continueRequest", {"requestId": params["requestId"]}, session)
            elif method == "Target.targetInfoChanged":
                info = params["targetInfo"]
                if info["type"] == "page" and page_url.get(info["targetId"]) != info["url"]:
                    page_url[info["targetId"]] = info["url"]
                    report(f"page {info['targetId'][:8]} at {loggable(info['url'])}")
            elif method in ("Network.responseReceivedExtraInfo", "Network.requestWillBeSentExtraInfo"):
                host = urlsplit(request_url.get(params.get("requestId"), "")).hostname or "?"
                for header in params.get("headers", {}):
                    if header.lower().startswith(("sec-session", "sec-secure-session", "x-chrome-", "google-accounts-")) and (header, host) not in dbsc_seen:
                        dbsc_seen.add((header, host))
                        report(f"account header {header} {'from' if 'response' in method else 'to'} {host}")
            elif method == "Network.requestWillBeSent":
                request_url[params["requestId"]] = params["request"]["url"]
            elif method == "Target.detachedFromTarget":
                sessions.pop(params["sessionId"], None)

            now = time.monotonic()
            if probe_at and now >= probe_at:
                probe_at = None
                report_sandbox(chrome.pid, data_dir)
                main_session = next((k for k, v in sessions.items() if v == main_target), None)
                if main_session:
                    seen = cdp.call("Runtime.evaluate", {"returnByValue": True, "expression": PAGE_VIEW}, main_session)
                    report(f"page sees {seen.get('result', {}).get('value')}")
                if request.get("probe"):
                    break
            if deadline and now >= deadline:
                report("timed out")
                break
            if now - last_poll >= 0.5:
                last_poll = now
                cookies = cdp.call("Storage.getCookies")["cookies"]
                done = not request.get("done_url") or re.search(request["done_url"], page_url.get(main_target, ""))
                for c in cookies:
                    domain = c["domain"].lstrip(".")
                    if c["name"] == request["cookie"] and done and (
                            domain == request["cookie_domain"] or domain.endswith("." + request["cookie_domain"])):
                        token, expires = c["value"], None if c.get("session") else c.get("expires")
                if token or request.get("keep_cookies"):
                    keep = [c for c in cookies if c["name"] != request["cookie"]
                            and keep_matches(request.get("keep_cookies", []), c["name"], c["domain"])]
                if token:
                    report(f"token cookie {request['cookie']} appeared")
                    report("google cookies at the end: " + " ".join(sorted(
                        f"{c['name']}@{c['domain']}" for c in cookies if host_matches(c["domain"].lstrip("."), "*.google.com")
                        or c["domain"].lstrip(".") == "google.com")))
                    seeded = {(c["name"], c["domain"]): c for c in request.get("keep", [])}
                    for c in cookies:
                        before = seeded.get((c["name"], c["domain"]))
                        if before:
                            changed = [f"{k} {before.get(k)}->{c.get(k)}" for k in ATTRIBUTES if before.get(k) != c.get(k)]
                            if changed:
                                report(f"cookie {c['name']}@{c['domain']} changed: {', '.join(changed)}")
                    report("profile: " + " ".join(sorted(os.listdir(f"{data_dir}/Default")))
                           if os.path.isdir(f"{data_dir}/Default") else "profile: no Default directory")
                    break
        try:
            cdp.call("Browser.close", timeout=5)
        except RuntimeError:
            pass
    finally:
        try:
            chrome.wait(10)
        except subprocess.TimeoutExpired:
            chrome.kill()
            chrome.wait()
        shutil.rmtree(data_dir, ignore_errors=True)
    send("result", token=token, expires=expires, keep=keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
