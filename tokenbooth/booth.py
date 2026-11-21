"""The booth: serves one request on the connected socket whose fd is its argument (default 3,
where systemd puts an accepted connection), then exits. See docs/booth-protocol.md."""

import os
import socket
import stat
import sys
from collections.abc import MutableMapping
from dataclasses import asdict
from pathlib import Path

from tokenbooth import protocol
from tokenbooth.protocol import ProtocolError, Request, Result
from tokenbooth.store import UsageError


def confine_display(environ: MutableMapping[str, str]) -> None:
    """Lets Qt draw only through Wayland: on X11 every client can read the keystrokes typed
    into the booth. `offscreen` draws nowhere and takes no input, and is what the tests use."""
    platform = environ.get("QT_QPA_PLATFORM", "wayland")
    if platform not in ("wayland", "offscreen"):
        raise UsageError(f"QT_QPA_PLATFORM={platform}: the booth draws only through Wayland")
    if platform == "wayland":
        if "WAYLAND_SOCKET" in environ:
            try:
                is_socket = stat.S_ISSOCK(os.fstat(int(environ["WAYLAND_SOCKET"])).st_mode)
            except (ValueError, OSError):
                is_socket = False
            if not is_socket:
                raise UsageError("WAYLAND_SOCKET is not a connected socket")
        else:
            display = environ.get("WAYLAND_DISPLAY")
            if not display:
                raise UsageError("no Wayland display: the booth never draws through X11, where every client"
                                 " can read what is typed")
            path = Path(display) if display.startswith("/") else Path(environ.get("XDG_RUNTIME_DIR", "/nonexistent"), display)
            try:
                is_socket = stat.S_ISSOCK(path.stat().st_mode)
            except OSError:
                is_socket = False
            if not is_socket:
                raise UsageError(f"WAYLAND_DISPLAY={display}: no Wayland socket at {path}")
    environ["QT_QPA_PLATFORM"] = platform
    for name in ("DISPLAY", "XAUTHORITY"):
        environ.pop(name, None)


def serve(sock: socket.socket) -> int:
    try:
        frame = protocol.receive(sock)
        if frame is None:
            return 1
        message, fds = frame
        for fd in fds:
            os.close(fd)
        request = Request.from_message(message)
    except ProtocolError as e:
        reply(sock, Result("refused", error=str(e)))
        return 2

    def log(text: str) -> None:
        try:
            protocol.send(sock, {"type": "log", "text": text})
        except OSError:
            pass

    try:
        confine_display(os.environ)
        from tokenbooth import browser
    except (UsageError, ImportError) as e:
        reply(sock, Result("refused", error=str(e)))
        return 2

    from tokenbooth.discover import Recorder
    recorder = Recorder() if request.mode == "discover" else None
    try:
        outcome, kept = browser.run(request.profile, request.keep, log if request.log else None, recorder,
                                    hangup=sock.fileno())
    except UsageError as e:
        reply(sock, Result("refused", error=str(e)))
        return 2
    if outcome.token is not None:
        result = Result("token", token=outcome.token, expires=outcome.expires)
    elif outcome.confirmed:
        result = Result("confirmed")
    elif outcome.timed_out:
        result = Result("timed_out")
    else:
        result = Result("cancelled")
    result.keep = kept if request.profile.keep_cookies else []
    if recorder:
        result.cookies = [asdict(seen) for seen in recorder.cookies.values()]
    reply(sock, result)
    return 0


def reply(sock: socket.socket, result: Result) -> None:
    try:
        protocol.send(sock, result.to_message())
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    sock = socket.socket(fileno=int(args[0]) if args else 3)
    try:
        return serve(sock)
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
