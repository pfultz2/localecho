"""The shadow screen must track what the real terminal does, where pyte doesn't."""
import pytest

from localecho import Predictor


def shadow(*chunks, rows=24, cols=80):
    p = Predictor(rows, cols)
    for c in chunks:
        p.on_output(c, 0.0)
    return p


def cursor(p):
    return (p.screen.cursor.y, p.screen.cursor.x)


@pytest.mark.parametrize("save, restore", [(b"\x1b7", b"\x1b8"), (b"\x1b[s", b"\x1b[u")])
def test_save_and_restore_cursor(save, restore):
    p = shadow(b"\r\n  \x1b[31m" + save + b"\x1b[0mabc\r\n" + restore)
    assert cursor(p) == (1, 2)
    assert p.screen.cursor.attrs.fg == "red" and p.screen.cursor.pen == (b"31",)


def test_restoring_twice_returns_to_the_same_place():
    # tput sc once, then tput rc before each redraw, as progress bars do
    assert cursor(shadow(b"\x1b[3;5H\x1b7one\x1b8two\x1b8")) == (2, 4)


def test_restoring_the_cursor_keeps_it_hidden():
    assert shadow(b"\x1b7\x1b[?25l\x1b8").screen.cursor.hidden
    assert not shadow(b"\x1b[?25l\x1b7\x1b[?25h\x1b8").screen.cursor.hidden


def test_saving_again_replaces_the_saved_cursor():
    assert cursor(shadow(b"\x1b[3;5H\x1b7\x1b[9;9H\x1b7\x1b[H\x1b8\x1b8")) == (8, 8)


def test_csi_s_with_parameters_is_not_a_save():
    # DECSLRM (left/right margins), which pyte doesn't support
    assert cursor(shadow(b"\x1b[3;5H\x1b7\x1b[9;9H\x1b[1;40s\x1b[H\x1b8")) == (2, 4)


def test_resize_keeps_the_saved_cursor():
    p = shadow(b"\x1b[3;5H\x1b7\x1b[20;1H")
    p.screen.resize(20, 80)                 # pyte saves and restores the cursor itself
    p.on_output(b"\x1b8", 0.0)
    assert cursor(p) == (2, 4)


def test_getting_shorter_loses_lines_below_the_cursor_first():
    p = shadow(b"\x1b[H\x1b[2JPassword: ")
    p.screen.resize(20, 80)
    assert p.screen.display[0].rstrip() == "Password:" and cursor(p) == (0, 10)


def test_getting_shorter_scrolls_to_keep_the_cursor_in_view():
    p = shadow(b"".join(b"line %d\r\n" % i for i in range(23)) + b"$ ")
    p.screen.resize(20, 80)
    assert p.screen.display[0].rstrip() == "line 4"
    assert p.screen.display[19].rstrip() == "$" and cursor(p) == (19, 2)
    p.screen.resize(24, 80)
    assert p.screen.display[20].rstrip() == ""


def test_reset_forgets_the_saved_cursor():
    p = shadow(b"\x1b[31m\x1b[5;5H\x1b7\x1bc\x1b8")
    assert cursor(p) == (0, 0)
    assert p.screen.cursor.pen == () and p.screen.cursor.attrs.fg == "default"


def test_soft_reset_clears_attributes_as_well_as_the_pen():
    attrs = shadow(b"\x1b[1;31m\x1b[!p").screen.cursor.attrs
    assert not attrs.bold and attrs.fg == "default"


def test_alternate_screen_restores_cursor_and_pen():
    p = shadow(b"\x1b[5;3H\x1b[31m\x1b[?1049h\x1b[H\x1b[0;32mfull screen\x1b[?1049l")
    assert cursor(p) == (4, 2)
    assert p.screen.cursor.attrs.fg == "red" and p.screen.cursor.pen == (b"31",)


def test_alternate_screen_has_its_own_saved_cursor():
    # A full-screen app saving the cursor mustn't disturb the shell's.
    assert cursor(shadow(b"\x1b[2;2H\x1b7\x1b[?47h\x1b[9;9H\x1b7\x1b[?47l\x1b8")) == (1, 1)
    assert cursor(shadow(b"\x1b[5;5H\x1b[?1049h\x1b[9;9H\x1b7\x1b[?1049l")) == (4, 4)


def test_line_drawing_charset():
    assert shadow(b"\x1b(0lqk\x1b(Bq").screen.display[0].rstrip() == "┌─┐q"
    assert shadow(b"\x1b)0a\x0eq\x0fq").screen.display[0].rstrip() == "a─q"


def test_shift_out_alone_stays_ascii():
    # G1 starts out as ASCII, as in xterm (pyte would start it as line drawing)
    assert shadow(b"\x0eq\x0f").screen.display[0].rstrip() == "q"


def test_utf8_is_still_decoded():
    p = shadow("é".encode(), b"\x1b%@", "ü".encode()[:1], "ü".encode()[1:])
    assert p.screen.display[0].rstrip() == "éü"
