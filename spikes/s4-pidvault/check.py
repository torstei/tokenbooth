"""S4 spike: does the daemon's pidvault check hold against the real pidvault?

Run from a shell that has VAULT (default `default`) unlocked, with the daemon installed:

    .venv/bin/python spikes/s4-pidvault/check.py [VAULT]

Each case asks the daemon for `status` with a connection to pidvault and reports which vaults
it took to be unlocked for the process that asked.
"""

import os
import socket
import sys
import time

from tokenbooth import protocol

DAEMON = os.environ.get("TOKENBOOTH_SOCKET", "/run/tokenbooth/sock")
PIDVAULT = os.environ.get("PIDVAULT_SOCKET", "/run/pidvault.sock")
VAULT = sys.argv[1] if len(sys.argv) > 1 else "default"


def pidvault_connection() -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(PIDVAULT)
    return connection


def ask(fd: int) -> str:
    """The daemon's view, as `unlocked [...]` or `refused: ...`."""
    daemon = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    daemon.connect(DAEMON)
    protocol.send(daemon, {"type": "status"}, [fd])
    reply, _ = protocol.receive(daemon)
    daemon.close()
    if reply["type"] == "error":
        return f"refused: {reply['message']}"
    return f"unlocked {reply['unlocked']}"


def outside_the_shell(work) -> str:
    """Runs `work` in a grandchild that has left this shell's process tree (its parent exited
    and it was reparented), and returns what it reports."""
    r, w = os.pipe()
    go_r, go_w = os.pipe()
    middle = os.fork()
    if middle == 0:
        os.setsid()
        if os.fork() == 0:
            os.read(go_r, 1)
            os.write(w, work().encode())
            os._exit(0)
        os._exit(0)
    os.waitpid(middle, 0)
    time.sleep(0.2)
    os.write(go_w, b"x")
    os.close(w)
    with os.fdopen(r) as result:
        return result.read()


def opened_by_a_process_that_then_exits() -> socket.socket:
    ours, theirs = socket.socketpair()
    child = os.fork()
    if child == 0:
        connection = pidvault_connection()
        socket.send_fds(theirs, [b"x"], [connection.fileno()])
        os._exit(0)
    _, fds, _, _ = socket.recv_fds(ours, 1, 1)
    os.waitpid(child, 0)
    return socket.socket(fileno=fds[0])


def main() -> int:
    cases = []

    connection = pidvault_connection()
    cases.append(("my own connection, from this unlocked shell", ask(connection.fileno()), f"'{VAULT}'"))

    borrowed = opened_by_a_process_that_then_exits()
    cases.append(("a connection opened by a process that has since exited", ask(borrowed.fileno()),
                  "refused: pidvault cannot tell"))

    mine = pidvault_connection()
    cases.append(("my connection, used by a process outside the shell",
                  outside_the_shell(lambda: ask(mine.fileno())), "unlocked []"))

    def own_connection() -> str:
        connection = pidvault_connection()
        return ask(connection.fileno())

    cases.append(("a process outside the shell, with its own connection",
                  outside_the_shell(own_connection), "unlocked []"))

    failed = 0
    for what, got, expect in cases:
        ok = expect in got
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {what}\n      {got}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
