"""A scripted user at a terminal, for end-to-end tests.

Session starts `localecho --exec -- lagpty.py DELAY <cmd>` in a pseudo-terminal
and plays the user's terminal emulator: it answers cursor-position queries,
keeps the screen in a pyte emulator, and lets tests type keys and look at
the screen.
"""
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time

import localecho
from localecho import ShadowScreen, ShadowStream

HERE = os.path.dirname(os.path.abspath(__file__))
LAGPTY = os.path.join(HERE, "lagpty.py")


class _Terminal(ShadowScreen):
    fd = None

    def write_process_input(self, data):
        os.write(self.fd, data.encode())      # e.g. the answer to ESC [ 6 n


class Session:
    def __init__(self, cmd=None, delay=0.15, predict="adaptive", extra=(),
                 rows=24, cols=80, env=None):
        """predict="none" runs without localecho at all (the reference)."""
        cmd = cmd or ["bash", "--norc", "--noprofile"]
        argv = [sys.executable, LAGPTY, str(delay)] + list(cmd)
        if predict != "none":
            argv = ([sys.executable, localecho.__file__, "--predict=" + predict]
                    + list(extra) + ["--exec", "--"] + argv)
        environ = dict(os.environ, TERM="xterm-256color", PS1="$ ", HISTFILE="/dev/null",
                       LANG="C.UTF-8", LC_ALL="C.UTF-8",
                       BASH_SILENCE_DEPRECATION_WARNING="1")   # macOS /bin/bash banner
        environ.update(env or {})
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(argv[0], argv, environ)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.pid, self.fd = pid, fd
        self.screen = _Terminal(cols, rows)
        self.screen.fd = fd
        self.stream = ShadowStream(self.screen)
        self.exit_code = None

    def pump(self, secs):
        """Let the session run for `secs`, updating the screen."""
        end = time.monotonic() + secs
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            ready, _, _ = select.select([self.fd], [], [], left)
            if ready:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    return
                self.stream.feed(data)

    def send(self, keys, wait=0.0):
        if isinstance(keys, str):
            keys = keys.encode()
        os.write(self.fd, keys)
        if wait:
            self.pump(wait)

    def wait_for(self, text, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(text in line for line in self.screen.display):
                return True
            self.pump(0.05)
        return False

    def line(self, y=None):
        return self.screen.display[self.screen.cursor.y if y is None else y].rstrip()

    def lines(self):
        return [line.rstrip() for line in self.screen.display]

    def cursor(self):
        return (self.screen.cursor.y, self.screen.cursor.x)

    def wait_exit(self, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump(0.05)
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.exit_code = os.waitstatus_to_exitcode(status)
                self.pid = None
                return self.exit_code
        raise TimeoutError("session did not exit")

    def close(self):
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
            except OSError:
                pass
            self.pid = None
        try:
            os.close(self.fd)
        except OSError:
            pass
