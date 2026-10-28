"""Output parsing: holding back partial sequences, pen tracking, sanitising."""
import pytest

from localecho import MAX_PEN, Predictor, char_style, sgr_resets, split_safe
from pyte.screens import Char


@pytest.mark.parametrize("data, safe, held", [
    (b"abc", b"abc", b""),
    (b"abc\x1b", b"abc", b"\x1b"),
    (b"abc\x1b[12;3", b"abc", b"\x1b[12;3"),
    (b"abc\x1b[12;3m", b"abc\x1b[12;3m", b""),
    (b"x\x1b]0;title", b"x", b"\x1b]0;title"),
    (b"x\x1b]0;title\x07", b"x\x1b]0;title\x07", b""),
    (b"x\x1b]0;title\x1b", b"x", b"\x1b]0;title\x1b"),     # ST split in half
    (b"x\x1b]0;title\x1b\\", b"x\x1b]0;title\x1b\\", b""),
    (b"x\x1b(", b"x", b"\x1b("),
    (b"x\x1b(B", b"x\x1b(B", b""),
    (b"ok\x1b7", b"ok\x1b7", b""),
    ("é".encode()[:1], b"", "é".encode()[:1]),             # half a UTF-8 char
    ("€".encode()[:2], b"", "€".encode()[:2]),
    ("é€".encode(), "é€".encode(), b""),
])
def test_split_safe(data, safe, held):
    assert split_safe(data) == (safe, held)


def test_char_style_round_trips_common_attributes():
    assert char_style(Char("x")) == b"\x1b[0m"
    assert char_style(Char("x", fg="red", bold=True)) == b"\x1b[0;1;31m"
    assert char_style(Char("x", fg="brightblack", bg="blue")) == b"\x1b[0;90;44m"
    assert char_style(Char("x", fg="ff8700")) == b"\x1b[0;38;5;208m"     # 256-colour
    assert char_style(Char("x", fg="123456")) == b"\x1b[0;38;2;18;52;86m"  # truecolor


def pen_after(*chunks):
    p = Predictor(24, 80)
    for c in chunks:
        p.on_output(c, 0.0)
    return p.screen.cursor.pen


def test_pen_tracks_sgr_and_resets():
    assert pen_after(b"\x1b[1m\x1b[33m") == (b"1", b"33")
    assert pen_after(b"\x1b[1m\x1b[0;32m") == (b"0;32",)
    assert pen_after(b"\x1b[1m\x1b[m") == ()
    assert pen_after(b"\x1b[2m\x1b[4:3m") == (b"2", b"4:3")       # dim, undercurl kept
    assert pen_after(b"\x1b[1m\x1b[38;5;0m") == (b"1", b"38;5;0")  # a colour, not a reset


def test_pen_save_restore():
    assert pen_after(b"\x1b[31m\x1b7\x1b[0;32m\x1b8") == (b"31",)
    assert pen_after(b"\x1b[31m\x1b[s\x1b[0m\x1b[u") == (b"31",)
    assert pen_after(b"\x1b[31m\x1b[?1049h\x1b[0;32m\x1b[?1049l") == (b"31",)
    assert pen_after(b"\x1b[31m\x1bc") == ()
    assert pen_after(b"\x1b[31m\x1b[!p") == ()


def test_sub_parameters_stay_in_order_with_save_restore():
    assert pen_after(b"\x1b[4:3m\x1b7\x1b[0;58:2::255:0:0m\x1b8") == (b"4:3",)
    assert pen_after(b"\x1b[0m\x1b7\x1b[4:3m\x1b8\x1b[58:5:1m") == (b"58:5:1",)


def test_pen_too_long_to_replay_falls_back_to_attributes():
    p = Predictor(24, 80)
    p.on_output(b"\x1b[1m" + b"\x1b[31m\x1b[32m" * MAX_PEN, 0.0)
    assert p.screen.cursor.pen is None
    assert p._pen_bytes() == b"\x1b[0;1;32m"
    p.on_output(b"\x1b[0;33m", 0.0)
    assert p.screen.cursor.pen == (b"0;33",)


@pytest.mark.parametrize("params, resets", [
    (b"", True), (b"0", True), (b"1;0;32", True), (b"1;;4", True),
    (b"1", False), (b"4:3", False), (b"38:5:0", False),
    (b"38;5;0", False), (b"48;2;0;0;0", False), (b"58;5;0", False),
    (b"38;5;0;0", True),
])
def test_sgr_resets(params, resets):
    assert sgr_resets(params) == resets


@pytest.mark.parametrize("seq", [
    b"\x1b[?4m",          # private SGR (xterm modifyOtherKeys query) - crashes plain pyte
    b"\x1b[>4;2m",        # modifyOtherKeys, sent by vim
    b"\x1b[4:3m",         # colon sub-parameters
    b"\x1b[!p",           # soft reset
    b"\x1b[1;2;3;4H",     # too many parameters
    b"\x1b[<u",           # kitty keyboard protocol
    b"\x1b[2 q",          # cursor shape
    b"\x1bP+q544e\x1b\\", # DCS query
])
def test_unusual_sequences_do_not_disturb_the_shadow(seq):
    p = Predictor(24, 80)
    p.on_output(b"ab" + seq + b"cd", 0.0)
    assert p.screen.display[0].rstrip() == "abcd"
    assert p.screen.cursor.x == 4


def test_alternate_screen_restores_main_buffer():
    p = Predictor(24, 80)
    p.on_output(b"$ ls\r\nfile\r\n$ ", 0.0)
    p.on_output(b"\x1b[?1049h\x1b[H\x1b[2Jfull screen app", 0.0)
    assert p.screen.display[0].startswith("full screen app")
    p.on_output(b"\x1b[?1049l", 0.0)
    assert [line.rstrip() for line in p.screen.display[:3]] == ["$ ls", "file", "$"]
    assert (p.screen.cursor.y, p.screen.cursor.x) == (2, 2)
