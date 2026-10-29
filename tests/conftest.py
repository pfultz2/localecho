import pytest

from localecho import Predictor, ShadowScreen, ShadowStream


class Rig:
    """Drives a Predictor directly, without any processes.

    `term` stands in for the user's real terminal: it receives exactly the
    bytes localecho would write to stdout.  Server output is supplied by the
    test, which lets each test play the role of the remote shell.
    """

    def __init__(self, rows=24, cols=80, mark="underline", mode="always"):
        self.p = Predictor(rows, cols, mode=mode, mark=mark)
        self.term = ShadowScreen(cols, rows)
        self.term_stream = ShadowStream(self.term)
        self.now = 100.0

    def server(self, data, dt=0.1):
        """The remote side writes `data`."""
        self.now += dt
        self.term_stream.feed(self.p.on_output(data, self.now))

    def type(self, keys, dt=0.01):
        """The user presses `keys` (sent to the server, predictions drawn)."""
        if isinstance(keys, str):
            keys = keys.encode()
        self.now += dt
        self.term_stream.feed(self.p.on_input(keys, self.now))

    def tick(self, dt):
        self.now += dt
        self.term_stream.feed(self.p.on_tick(self.now))

    def line(self, y=None):
        if y is None:
            y = self.term.cursor.y
        return self.term.display[y].rstrip()

    def cell(self, x, y=None):
        if y is None:
            y = self.term.cursor.y
        return self.term.buffer[y][x]

    def cursor(self):
        return (self.term.cursor.y, self.term.cursor.x)

    def confident(self, prompt=b"$ ", echo=b"e"):
        """Show a prompt and confirm one echo, so later predictions display."""
        self.server(prompt)
        self.type("e")
        self.server(echo)
        return self


@pytest.fixture
def rig():
    return Rig()


@pytest.fixture
def make_rig():
    return Rig
