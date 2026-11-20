"""S1/S2 spike, booth side: runs as a DynamicUser instance of tokenbooth-spike-booth@.service.

Receives a Wayland connection and a URL over the accepted socket (fd 3), shows the URL
through that connection only, and reports what it can see back over the same socket.
"""

import array
import os
import socket
import sys

conn = socket.socket(fileno=3)


def report(line: str) -> None:
    conn.sendall((line + "\n").encode())


msg, ancdata, _, _ = conn.recvmsg(4096, socket.CMSG_SPACE(array.array("i", [0]).itemsize))
fds = array.array("i")
for level, kind, data in ancdata:
    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
        fds.frombytes(data[: len(data) - len(data) % fds.itemsize])
if len(fds) != 1:
    report(f"FAIL expected one fd, got {len(fds)}")
    sys.exit(1)
url = msg.decode().strip()

wayland = fds[0]
peer = socket.socket(fileno=os.dup(wayland)).getsockopt(
    socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
peer_pid, peer_uid, _ = array.array("i", peer)
os.environ["WAYLAND_SOCKET"] = str(wayland)
for name in ("WAYLAND_DISPLAY", "DISPLAY"):
    os.environ.pop(name, None)

report(f"uid {os.getuid()} gid {os.getgid()} wayland peer uid {peer_uid} pid {peer_pid}")
report(f"interpreter {os.path.realpath('/proc/self/exe')}, apparmor label "
       + open("/proc/self/attr/current").read().strip())
report("own seccomp filters " + next(
    line.split(":", 1)[1].strip() for line in open("/proc/self/status") if line.startswith("Seccomp_filters:")))
for path in ("/home", f"/run/user/{peer_uid}", "/run/user"):
    try:
        os.listdir(path)
        report(f"can list {path}")
    except OSError as e:
        report(f"cannot list {path}: {e.strerror}")
report("env " + " ".join(sorted(k for k in os.environ if not k.startswith("LISTEN_"))))

from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtWebEngineCore import QWebEngineProfile  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([sys.argv[0]])
app.setDesktopFileName("tokenbooth")
report(f"qt platform {app.platformName()}")

profile = QWebEngineProfile()
report(f"profile off-the-record {profile.isOffTheRecord()}")
view = QWebEngineView(profile)
view.setWindowTitle("TOKEN BOOTH · spike S1")
view.resize(1000, 800)
view.loadFinished.connect(lambda ok: report(f"load finished ok={ok}"))
view.load(QUrl(url))
view.show()


def sandbox() -> None:
    status = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            status[pid] = dict(line.split(":", 1) for line in open(f"/proc/{pid}/status").read().splitlines())
        except (OSError, ValueError):
            pass
    family = {str(os.getpid())}
    grew = True
    while grew:
        grew = False
        for pid, fields in status.items():
            if pid not in family and fields.get("PPid", "").strip() in family:
                family.add(pid)
                grew = True
    family.discard(str(os.getpid()))
    for pid in sorted(family, key=int):
        fields = status[pid]
        nspid = fields.get("NSpid", "").split()
        try:
            title = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()
        except OSError:
            title = ""
        kind = next((w for w in title.split() if w.startswith("--type=")), fields.get("Name", "").strip())
        report(f"child {pid} {kind}: own pid namespace {len(nspid) > 1} (NSpid {' '.join(nspid)}),"
               f" seccomp mode {fields.get('Seccomp', '?').strip()}"
               f" filters {fields.get('Seccomp_filters', '?').strip()}")
    if not family:
        report("children: none")


QTimer.singleShot(5000, sandbox)
app.exec()
report("window closed")
