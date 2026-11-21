"""The booth: serves one request on the connected socket whose fd is its argument (default 3,
where systemd puts an accepted connection), then exits. See docs/booth-protocol.md."""

import os
import socket
import sys
from dataclasses import asdict

from tokenbooth import protocol
from tokenbooth.protocol import ProtocolError, Request, Result
from tokenbooth.store import UsageError


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
