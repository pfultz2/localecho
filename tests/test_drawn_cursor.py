"""Applications that hide the cursor and draw their own, as Ink apps do."""
import pytest

LEFT, RIGHT, BS = "\x1b[D", "\x1b[C", "\x7f"
ROW = 1     # the input line


def ink(text, at=None, prompt="> "):
    """What an Ink app writes to show `text` with its cursor at index `at`:
    the line redrawn, the cursor a reverse-video cell, and the real cursor
    hidden and parked below."""
    at = len(text) if at is None else at
    under = text[at] if at < len(text) else " "
    return ("\x1b[?25l\x1b[2;1H\x1b[2K%s%s\x1b[7m%s\x1b[27m%s\x1b[4;1H"
            % (prompt, text[:at], under, text[at + 1:])).encode()


def block(rig):
    """Where the user sees the drawn cursor."""
    return [(y, x) for y in range(rig.term.lines) for x in range(rig.term.columns)
            if rig.cell(x, y).reverse]


def test_typed_text_moves_the_drawn_cursor(rig):
    rig.server(ink(""))
    rig.type("e")
    assert rig.line(ROW) == ">"              # tentative, as with a real cursor
    rig.server(ink("e"))
    rig.type("ch")
    assert rig.line(ROW) == "> ech"
    assert block(rig) == [(ROW, 5)]
    assert rig.cell(3, ROW).underscore and not rig.cell(3, ROW).reverse
    assert rig.cursor() == (3, 0)            # the hidden cursor stays where it was
    rig.server(ink("ech"))
    assert rig.line(ROW) == "> ech" and block(rig) == [(ROW, 5)]
    assert not rig.cell(3, ROW).underscore
    assert not rig.p.keys


def test_editing_keys_move_the_drawn_cursor(rig):
    rig.confident(ink(""), ink("e"))
    rig.type("cho")
    rig.server(ink("echo"))
    rig.type(LEFT + LEFT + "X")
    assert rig.line(ROW) == "> ecXho" and block(rig) == [(ROW, 5)]
    rig.server(ink("ecXho", 3))
    assert not rig.p.keys
    rig.type(BS + RIGHT)
    assert rig.line(ROW) == "> echo" and block(rig) == [(ROW, 5)]
    assert rig.cell(4, ROW).data == "h" and not rig.cell(4, ROW).reverse
    rig.server(ink("echo", 3))
    assert not rig.p.keys and rig.p.epoch == rig.p.confirmed_epoch


@pytest.mark.parametrize("screen", [
    b"\x1b[?25l",                                    # no drawn cursor
    b"\x1b[?25l\x1b[3;1H\x1b[7ma\x1b[27m b \x1b[7mc",   # two candidates
])
def test_no_prediction_without_exactly_one_drawn_cursor(rig, screen):
    rig.confident()
    rig.server(screen)
    rig.type("c")
    assert rig.line(0) == "$ e"
    assert not rig.p.keys


def test_a_reverse_video_bar_is_not_a_cursor(rig):
    rig.confident(ink(""), ink("e"))
    rig.server(b"\x1b[6;1H\x1b[7m status \x1b[27m")
    rig.type("c")
    assert rig.line(ROW) == "> ec"


def test_password_prompt_with_a_drawn_cursor(rig):
    rig.server(ink("", prompt="Passcode: "))
    rig.type("a")
    rig.server(ink("a", prompt="Passcode: "))
    rig.type("b")
    assert rig.line(ROW) == "Passcode: a"


def test_hidden_cursor_left_at_pending_autowrap(make_rig):
    rig = make_rig(rows=6, cols=20)
    rule = b"\x1b[3;1H" + "─".encode() * 20     # ends at the right margin
    rig.confident(ink("") + rule, ink("e") + rule)
    rig.type("c")
    assert rig.line(ROW) == "> ec"
    assert rig.cursor()[1] == 20             # still about to wrap ...
    rig.server(b"Z")
    assert rig.cell(0, 3).data == "Z"        # ... so the server's next char wraps


def test_typing_continues_while_a_frame_is_half_drawn(rig):
    rig.confident(ink(""), ink("e"))
    rig.type("c")
    frame = ink("ec")
    half = frame.index(b"\x1b[7m")
    rig.server(frame[:half], dt=0.01)        # the drawn cursor isn't there yet
    rig.type("h")
    rig.server(frame[half:], dt=0.01)
    rig.type("o")
    assert rig.line(ROW) == "> echo"
    rig.server(ink("echo"))
    assert not rig.p.keys and rig.p.epoch == rig.p.confirmed_epoch
