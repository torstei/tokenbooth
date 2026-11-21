"""The booth protocol: one request in, log events and one result out, over a connected Unix
stream socket. Specified in docs/booth-protocol.md."""

import array
import json
import os
import socket
import struct
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields

from tokenbooth.store import Profile

VERSION = 1
MAX_FRAME = 1 << 20
MAX_FDS = 4
MODES = ("login", "discover")
OUTCOMES = ("token", "confirmed", "cancelled", "timed_out", "refused")


class ProtocolError(Exception):
    pass


def send(sock: socket.socket, message: dict, fds: list[int] = ()) -> None:
    body = json.dumps(message, separators=(",", ":")).encode()
    if len(body) > MAX_FRAME:
        raise ProtocolError(f"frame of {len(body)} bytes exceeds {MAX_FRAME}")
    data = struct.pack(">I", len(body)) + body
    sent = 0
    if fds:
        sent = sock.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])
    sock.sendall(data[sent:])


def _receive_exactly(sock: socket.socket, size: int, fds: list[int]) -> bytes:
    data = b""
    space = socket.CMSG_SPACE(MAX_FDS * array.array("i").itemsize)
    while len(data) < size:
        chunk, ancillary, flags, _ = sock.recvmsg(size - len(data), space, socket.MSG_CMSG_CLOEXEC)
        for level, kind, payload in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                received = array.array("i")
                received.frombytes(payload[: len(payload) - len(payload) % received.itemsize])
                fds.extend(received)
        if flags & socket.MSG_CTRUNC:
            raise ProtocolError(f"more than {MAX_FDS} file descriptors passed")
        if not chunk:
            if data:
                raise ProtocolError("connection closed inside a frame")
            return b""
        data += chunk
    return data


def receive(sock: socket.socket) -> tuple[dict, list[int]] | None:
    """The next frame and any fds passed with it, or None when the peer closed between frames.
    The caller owns the fds, also when this raises after receiving them."""
    fds: list[int] = []
    try:
        header = _receive_exactly(sock, 4, fds)
        if not header:
            return None
        (size,) = struct.unpack(">I", header)
        if size > MAX_FRAME:
            raise ProtocolError(f"frame of {size} bytes exceeds {MAX_FRAME}")
        body = _receive_exactly(sock, size, fds)
        if len(body) != size:
            raise ProtocolError("connection closed inside a frame")
        try:
            message = json.loads(body)
        except ValueError as e:
            raise ProtocolError(f"frame is not JSON: {e}") from None
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            raise ProtocolError("frame is not an object with a type")
        return message, fds
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


PROFILE_FIELDS = {
    "name": str, "start_url": str, "allowed_hosts": list[str], "cookie": str, "cookie_domain": str,
    "done_url": str, "keep_cookies": list[str], "user_agent": str, "timeout": int,
}
KEEP_FIELDS = {"name": str, "domain": str, "path": str, "expires": (int, float, type(None)), "raw": str}


def _check(where: str, value: dict, spec: dict) -> None:
    if not isinstance(value, dict):
        raise ProtocolError(f"{where} is not an object")
    if missing := sorted(set(spec) - set(value)):
        raise ProtocolError(f"{where} lacks {', '.join(missing)}")
    if unknown := sorted(set(value) - set(spec)):
        raise ProtocolError(f"{where} has unknown key(s) {', '.join(unknown)}")
    for key, kind in spec.items():
        strings = kind == list[str]
        if not isinstance(value[key], list if strings else kind) or (kind is int and isinstance(value[key], bool)):
            raise ProtocolError(f"{where}.{key} has the wrong type")
        if strings and not all(isinstance(item, str) for item in value[key]):
            raise ProtocolError(f"{where}.{key} must hold strings")


@dataclass
class Request:
    mode: str
    profile: Profile
    keep: list[dict] = field(default_factory=list)
    log: bool = False

    def to_message(self) -> dict:
        profile = {k: getattr(self.profile, k) for k in PROFILE_FIELDS}
        return {"type": "request", "version": VERSION, "mode": self.mode, "log": self.log,
                "profile": profile, "keep": self.keep}

    @classmethod
    def from_message(cls, message: dict) -> "Request":
        _check("request", message, {"type": str, "version": int, "mode": str, "log": bool,
                                    "profile": dict, "keep": list})
        if message["type"] != "request":
            raise ProtocolError(f"expected a request, got {message['type']!r}")
        if message["version"] != VERSION:
            raise ProtocolError(f"protocol version {message['version']} is not {VERSION}")
        if message["mode"] not in MODES:
            raise ProtocolError(f"unknown mode {message['mode']!r}")
        profile = message["profile"]
        _check("profile", profile, PROFILE_FIELDS)
        if not profile["start_url"].startswith("https://"):
            raise ProtocolError("profile.start_url must be an https URL")
        if message["mode"] == "login" and not profile["cookie"]:
            raise ProtocolError("profile.cookie is required to log in")
        for i, entry in enumerate(message["keep"]):
            _check(f"keep[{i}]", entry, KEEP_FIELDS)
        return cls(mode=message["mode"], profile=Profile(**profile), keep=message["keep"], log=message["log"])


@dataclass
class Result:
    outcome: str
    token: str | None = None
    expires: float | None = None
    keep: list[dict] = field(default_factory=list)
    cookies: list[dict] | None = None
    error: str | None = None

    def to_message(self) -> dict:
        return {"type": "result", **{k: v for k, v in asdict(self).items() if v is not None or k in ("token", "expires")}}

    @classmethod
    def from_message(cls, message: dict) -> "Result":
        if message.get("type") != "result":
            raise ProtocolError(f"expected a result, got {message.get('type')!r}")
        known = {f.name for f in fields(cls)}
        if unknown := sorted(set(message) - known - {"type"}):
            raise ProtocolError(f"result has unknown key(s) {', '.join(unknown)}")
        result = cls(**{k: v for k, v in message.items() if k != "type"})
        if result.outcome not in OUTCOMES:
            raise ProtocolError(f"unknown outcome {result.outcome!r}")
        if (result.outcome == "token") != isinstance(result.token, str):
            raise ProtocolError("a token comes with outcome token, and only then")
        return result


def run_booth(request: Request, on_log: Callable[[str], None] | None = None,
              env: dict | None = None, engine: str = "chrome") -> Result:
    """Start a booth as a child process, hand it the request and wait for its result. Closing
    our end, as an exception here does, makes the booth quit."""
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    booth = subprocess.Popen([sys.executable, "-I", "-m", "tokenbooth.booth", "--engine", engine,
                              str(theirs.fileno())],
                             pass_fds=[theirs.fileno()], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, env=env)
    theirs.close()
    result = None
    try:
        send(ours, request.to_message())
        while result is None and (frame := receive(ours)) is not None:
            message, fds = frame
            for fd in fds:
                os.close(fd)
            if message["type"] == "log":
                if on_log and isinstance(message.get("text"), str):
                    on_log(message["text"])
            else:
                result = Result.from_message(message)
    finally:
        ours.close()
        status = booth.wait()
    if result is None:
        raise ProtocolError(f"the booth exited without a result (status {status})")
    return result
