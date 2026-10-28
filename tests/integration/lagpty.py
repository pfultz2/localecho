#!/usr/bin/env python3
"""lagpty.py DELAY CMD...  - run CMD in a pseudo-terminal, delaying all bytes in
both directions by DELAY seconds. Simulates a high-latency ssh link."""
import fcntl
import os
import pty
import select
import signal
import sys
import termios
import time
import tty
from collections import deque


def main():
    delay = float(sys.argv[1])
    cmd = sys.argv[2:]

    def size(fd):
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(cmd[0], cmd)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size(0))
    tty.setraw(0)
    signal.signal(signal.SIGWINCH, lambda s, f: fcntl.ioctl(fd, termios.TIOCSWINSZ, size(0)))
    to_child, to_user = deque(), deque()
    alive = True
    while alive or to_user:
        now = time.monotonic()
        while to_child and to_child[0][0] <= now:
            os.write(fd, to_child.popleft()[1])
        while to_user and to_user[0][0] <= now:
            os.write(1, to_user.popleft()[1])
        due = [q[0][0] for q in (to_child, to_user) if q]
        timeout = max(0.0, min(due) - now) if due else None
        try:
            ready, _, _ = select.select([0] + ([fd] if alive else []), [], [], timeout)
        except InterruptedError:
            continue
        now = time.monotonic()
        if fd in ready:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if data:
                to_user.append((now + delay, data))
            else:
                alive = False
        if 0 in ready:
            data = os.read(0, 4096)
            if not data:
                break
            to_child.append((now + delay, data))
    _, status = os.waitpid(pid, 0)
    sys.exit(os.waitstatus_to_exitcode(status))


if __name__ == "__main__":
    main()
