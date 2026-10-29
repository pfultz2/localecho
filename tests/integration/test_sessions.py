"""End-to-end: real programs behind a simulated 300 ms round trip.

Each test types the same keys with and without localecho and compares what
the screen shows. Without prediction we wait for the echo after every key;
with prediction we look 30 ms after each key, long before any echo could
arrive, so whatever is on screen then was drawn by localecho.
"""
import os
import shutil
import sys

import pytest

from ptyterm import HERE, Session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != "posix", reason="needs pseudo-terminals"),
    pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash"),
]

DELAY = 0.15        # one way, so a 300 ms round trip
SETTLE = 0.8        # comfortably longer than the round trip
FAST = 0.03         # typing gap: much shorter than the round trip
LEFT, RIGHT, BS = "\x1b[D", "\x1b[C", "\x7f"


@pytest.fixture
def sessions():
    started = []

    def start(**kwargs):
        s = Session(delay=DELAY, **kwargs)
        started.append(s)
        return s
    yield start
    for s in started:
        s.close()


def ready_shell(s, prompt="$"):
    assert s.wait_for(prompt), s.lines()
    s.pump(0.3)
    s.send("e", SETTLE)      # first echo lets localecho measure the link and trust it
    return s


EDIT_KEYS = list("cho abc") + [LEFT, LEFT, "X", BS, "Y", RIGHT, "z", BS, BS, "Q"]


def replay(s, wait, state):
    """Type EDIT_KEYS, noting state(s) `wait` seconds after each key."""
    seen = []
    for k in EDIT_KEYS:
        s.send(k, wait)
        seen.append(state(s))
    return seen


def test_editing_keys_show_what_the_server_will_show(sessions):
    def state(s):
        return s.line(), s.cursor()

    expected = replay(ready_shell(sessions(predict="none")), SETTLE, state)
    pred = ready_shell(sessions())
    assert replay(pred, FAST, state) == expected
    pred.send("\r", SETTLE)
    assert pred.lines()[1] == "aYQc"


def test_drawn_cursor_like_claude_code(sessions):
    def state(s):
        y = next(y for y, line in enumerate(s.lines()) if line.startswith(">"))
        row = s.screen.buffer[y]
        return s.lines()[y], [x for x in range(s.screen.columns) if row[x].reverse]

    cmd = [sys.executable, os.path.join(HERE, "inkapp.py")]
    expected = replay(ready_shell(sessions(predict="none", cmd=cmd), ">"), SETTLE, state)
    pred = ready_shell(sessions(cmd=cmd), ">")
    assert replay(pred, FAST, state) == expected
    pred.send("\r", SETTLE)
    assert "got:echo aYQc" in pred.lines()


def test_predictions_are_marked_until_echoed(sessions):
    s = ready_shell(sessions(extra=["--mark=grey"]))
    for ch in "cho hi":
        s.send(ch, FAST)
    y = s.cursor()[0]
    assert s.line() == "$ echo hi"
    assert s.screen.buffer[y][7].fg == "brightblack"
    s.pump(SETTLE)
    assert all(s.screen.buffer[y][x].fg == "default" for x in range(2, 9))


@pytest.mark.parametrize("prompt", ["Password: ", "Code: "])
def test_hidden_input_is_never_drawn(sessions, prompt):
    s = ready_shell(sessions())
    for ch in "cho; read -s -p '%s' pw; echo got:$pw" % prompt:
        s.send(ch, 0.01)
    s.send("\r")
    leaked = False
    for ch in "zqxv77":           # typed before the prompt is even visible
        s.send(ch, FAST)
        leaked |= any("zq" in line for line in s.lines())
    s.pump(SETTLE)
    for ch in "kk":               # and after
        s.send(ch, 0.1)
        leaked |= any("zq" in line or "kk" in line for line in s.lines())
    s.send("\r", SETTLE)
    assert not leaked
    assert any(line.endswith("got:zqxv77kk") for line in s.lines())   # input reached the shell


def test_bulk_output_then_typing(sessions):
    s = ready_shell(sessions())
    s.send("\x15", 0.0)                       # Ctrl-U: drop the 'e'
    s.send("seq 1 20000\r")
    assert s.wait_for("20000")
    s.pump(SETTLE)
    s.send("e", SETTLE)
    for ch in "cho done":
        s.send(ch, FAST)
    assert s.line() == "$ echo done"


def test_exit_code_is_passed_through(sessions):
    s = sessions(cmd=["bash", "-c", "exit 7"])
    assert s.wait_exit() == 7


@pytest.mark.skipif(shutil.which("vim") is None, reason="needs vim")
def test_vim_session_ends_identical(sessions):
    def run(predict):
        s = ready_shell(sessions(predict=predict))
        s.send("\x15vim -u NONE -N -i NONE -n\r", 1.5)
        s.send("i", 0.3)
        for ch in "hello world":
            s.send(ch, FAST)
        s.send(BS + "D", SETTLE)
        s.send("\x1b", 0.5)
        for ch in "0jjkl":
            s.send(ch, FAST)
        s.pump(SETTLE)
        in_vim = s.lines()
        s.send(":q!\r", 1.0)
        s.send("echo after\r", SETTLE)
        return in_vim, s.lines()

    assert run("adaptive") == run("none")


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_inside_tmux(sessions, tmp_path):
    def run(predict):
        sock = str(tmp_path / ("tmux-" + predict))
        s = sessions(predict=predict,
                     cmd=["tmux", "-S", sock, "-f", "/dev/null", "new-session",
                          "bash --norc --noprofile"])
        s.pump(2.0)
        s.send("e", SETTLE)
        snaps = []
        for ch in "cho in tmux":
            s.send(ch, FAST)
            snaps.append(s.line().split("# ")[-1].split("$ ")[-1])
        s.pump(SETTLE)
        s.send("\r", SETTLE)
        final = s.lines()[:-1]                   # drop the status bar (it has a clock)
        os.system("tmux -S %s kill-server >/dev/null 2>&1" % sock)
        return snaps, final

    snaps, final = run("adaptive")
    _, final_ref = run("none")
    assert snaps[-1].endswith("echo in tmux")   # drawn before tmux echoed it
    assert final == final_ref
