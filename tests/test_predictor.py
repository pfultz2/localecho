"""Prediction behaviour, with the test playing the remote shell."""
import pytest

LEFT, RIGHT, BS = "\x1b[D", "\x1b[C", "\x7f"


def test_nothing_is_drawn_until_an_echo_confirms(rig):
    rig.server(b"$ ")
    rig.type("e")
    assert rig.line() == "$"              # tentative: not shown yet
    rig.server(b"e")
    assert rig.line() == "$ e"
    rig.type("c")
    assert rig.line() == "$ ec"           # now trusted: shown immediately
    assert rig.cursor() == (0, 4)


def test_unconfirmed_characters_are_underlined_then_cleared(rig):
    rig.confident()
    rig.type("ch")
    assert rig.cell(3).underscore and rig.cell(4).underscore
    assert not rig.cell(2).underscore     # 'e' was already echoed
    rig.server(b"ch")
    assert rig.line() == "$ ech"
    assert not any(rig.cell(x).underscore for x in range(2, 5))


def test_grey_mark(make_rig):
    rig = make_rig(mark="grey").confident()
    rig.type("c")
    assert rig.cell(3).fg == "brightblack"
    rig.server(b"c")
    assert rig.cell(3).fg == "default"


def test_no_mark(make_rig):
    rig = make_rig(mark="none").confident()
    rig.type("c")
    assert rig.line() == "$ ec"
    assert not rig.cell(3).underscore and rig.cell(3).fg == "default"


def test_adaptive_mode_waits_for_a_slow_link(make_rig):
    rig = make_rig(mode="adaptive")
    rig.server(b"$ ")
    rig.type("e", dt=0.001)
    rig.server(b"e", dt=0.005)            # 5 ms round trip: no need to predict
    rig.type("c")
    assert rig.line() == "$ e"
    rig.server(b"c", dt=0.3)              # 300 ms: start predicting
    rig.type("h")
    assert rig.line() == "$ ech"


def test_typed_text_uses_the_current_colour_and_the_pen_is_restored(rig):
    rig.server(b"\x1b[1;32m$\x1b[0;33m ")     # prompt leaves the pen yellow
    rig.type("e")
    rig.server(b"e")
    rig.type("c")
    assert rig.cell(3).data == "c" and rig.cell(3).fg == "brown"   # pyte's name for 33
    rig.server(b"c")                         # server keeps writing in yellow
    assert rig.cell(3).fg == "brown" and not rig.cell(3).underscore
    assert rig.term.cursor.attrs.fg == "brown"


def test_a_colour_ending_in_0_is_not_a_reset(rig):
    rig.server(b"\x1b[1m\x1b[38;5;0m$ ")     # bold, colour 0
    rig.type("e")
    rig.server(b"e")
    rig.type("c")
    assert rig.cell(3).data == "c" and rig.cell(3).bold
    assert rig.term.cursor.attrs.bold        # still bold for the server's next write


def test_prompt_drawn_with_cursor_save_and_restore(rig):
    rig.server(b"$ \x1b[s\x1b[70G[12:00]\x1b[u")   # a right-hand prompt
    rig.type("e")
    rig.server(b"e")
    rig.type("c")
    assert rig.line().startswith("$ ec ") and rig.cursor() == (0, 4)


def test_nothing_is_drawn_in_line_drawing_mode(rig):
    rig.confident()
    rig.server(b"\x1b(0")                    # now "q" shows as a line
    rig.type("q")
    assert rig.line() == "$ e"


def test_insert_in_the_middle_shifts_the_rest(rig):
    rig.confident()
    rig.type("cho")
    rig.server(b"cho")
    rig.type(LEFT + LEFT + "X")
    assert rig.line() == "$ ecXho"
    assert rig.cursor() == (0, 5)
    rig.server(b"\b\bXho\b\b")               # what bash sends
    assert rig.line() == "$ ecXho"
    assert rig.cursor() == (0, 5)
    assert not rig.p.keys


def test_backspace_mid_line(rig):
    rig.confident()
    rig.type("cho")
    rig.server(b"cho")
    rig.type(LEFT + BS)
    assert rig.line() == "$ eco"
    assert rig.cursor() == (0, 4)
    rig.server(b"\b\bo \b\b")
    assert rig.line() == "$ eco"
    assert not rig.p.keys


def test_backspace_never_erases_the_prompt(rig):
    rig.server(b"$ x")
    rig.type("e")
    rig.server(b"e")                         # confident, but input started at col 3
    rig.type(BS + BS)
    assert rig.line() == "$ x"               # first BS (over 'e') predicted, second not
    assert rig.cursor() == (0, 3)


def test_enter_starts_a_new_untrusted_epoch(rig):
    rig.confident()
    rig.type("\r")
    rig.server(b"\r\n$ ")
    rig.type("ls")
    assert rig.line() == "$"                 # hidden until this line's first echo
    rig.server(b"l")
    assert rig.line() == "$ ls"


def test_password_prompt_never_shows_typed_text(rig):
    rig.confident()
    rig.type("\r")
    rig.server(b"\r\nPassword: ")
    rig.type("hunter2")
    assert "hunter2" not in rig.line()
    rig.tick(6.0)                            # predictions eventually expire
    rig.type("\r")
    assert all("hunter" not in line for line in rig.term.display)


def test_password_line_blocks_drawing_even_when_trusted(rig):
    rig.server(b"Enter passphrase: ")
    rig.type("a")
    rig.server(b"a")                         # an echoing prompt that says passphrase
    rig.type("b")
    assert rig.line() == "Enter passphrase: a"


def test_misprediction_is_corrected(rig):
    rig.confident()
    rig.type("x")
    assert rig.line() == "$ ex"
    rig.server(b"Q")                         # the server did something else
    rig.tick(0.1)                            # after a short grace period ...
    assert rig.line() == "$ eQ"              # ... the screen shows the truth
    assert not rig.p.keys
    rig.type("y")
    assert rig.line() == "$ eQ"              # and we're cautious again


def test_split_echo_is_not_a_misprediction(rig):
    rig.confident()
    rig.type("cho")
    rig.server(b"cho")
    rig.type(LEFT + LEFT + "X")
    rig.server(b"\b\bXh", dt=0.1)            # echo arrives in two pieces
    rig.server(b"o\b\b", dt=0.01)
    assert rig.line() == "$ ecXho"
    assert not rig.p.keys and rig.p.epoch == rig.p.confirmed_epoch


def test_predictions_follow_scrolling(make_rig):
    rig = make_rig(rows=5, cols=40)
    rig.server(b"\r\n" * 4 + b"$ ")          # prompt on the bottom row
    rig.type("e")
    rig.server(b"e")
    rig.type("c")
    assert rig.p.keys[0].head == (4, 3, "c")
    rig.server(b"\x1b[2S", dt=0.01)          # screen scrolls up two lines
    assert rig.p.keys[0].head == (2, 3, "c")
    assert rig.p.keys[0].bcur == (2, 3)


def test_nothing_is_drawn_in_the_middle_of_an_escape_sequence(rig):
    rig.confident()
    rig.server(b"\x1b[3")                    # incomplete colour change, passed on at once
    assert rig.p.held == b"\x1b[3"
    rig.type("c")                            # drawing now would break it up
    assert rig.line() == "$ e"
    rig.server(b"1m")
    assert rig.p.held == b""
    assert rig.line() == "$ ec"
    assert rig.term.cursor.attrs.fg == "red"


def test_nothing_is_drawn_inside_a_dcs_with_escapes_in_it(rig):
    rig.confident()
    rig.server(b"\x1bPtmux;\x1b\x1b[31m")    # tmux passthrough, not finished yet
    assert rig.p.on_input(b"c", rig.now) == b""
    assert b"c" in rig.p.on_output(b"x\x1b\\", rig.now + 0.1)


def test_a_string_sequence_never_finished_is_given_up_on(rig):
    rig.confident()
    rig.server(b"\x1bPxyz")                  # a stray DCS start, from cat of a binary
    rig.server(b"\r\nmore\r\n$ ", dt=3.0)    # (the test terminal takes it all for text)
    rig.tick(1.0)
    assert rig.p.held == b"\x1bP"
    rig.tick(rig.p.STRING_TIMEOUT - 3.9)     # counted from its start, as in tmux
    assert rig.p.held == b"" and rig.p.quarantine
    assert rig.answer() == b""
    assert not rig.p.quarantine
    rig.type("e")
    rig.server(b"e")
    rig.type("c")
    assert rig.line() == "$ ec"


def test_an_unfinished_string_sequence_keeps_nothing_of_its_contents(rig):
    rig.server(b"\x1b]52;c;")                # a big clipboard copy
    for _ in range(10):
        rig.server(b"YWJj" * 1000, dt=0.01)
    assert rig.p.held == b"\x1b]"
    rig.server(b"\x07$ ")
    assert rig.p.held == b"" and rig.p.screen.display[0].rstrip() == "$"


def test_resize_pauses_until_cursor_is_resynced(rig):
    rig.confident()
    query = rig.p.on_resize(30, 100, rig.now)
    rig.term.resize(30, 100)
    assert query == b"\x1b[6n"
    rig.type("c")
    assert rig.line() == "$ e"               # not drawn while out of sync
    data, extra = rig.p.filter_input(b"\x1b[1;4R", rig.now)
    assert data == b"" and extra == b""      # our answer is swallowed, not sent to ssh
    assert not rig.p.quarantine


@pytest.mark.parametrize("rows, scrolled", [(30, 0), (20, 4)])
def test_resize_takes_drawn_predictions_off_first(rig, rows, scrolled):
    rig.server(b"\r\n" * 23)                 # prompt on the bottom row
    rig.confident()
    rig.type("ch")
    assert rig.line() == "$ ech" and rig.cursor() == (23, 5)
    # The terminal resizes first.  Getting shorter, it keeps the cursor's
    # line in view by scrolling.
    rig.term.resize(rows, 80)
    rig.term_stream.feed(rig.p.on_resize(rows, 80, rig.now))
    y = 23 - scrolled
    assert (rig.line(), rig.cursor()) == ("$ e", (y, 3))
    assert not any(rig.cell(x).underscore for x in range(6))
    assert rig.answer() == b""
    rig.server(b"ch")
    assert rig.line() == "$ ech" and rig.cursor() == (y, 5)
    assert rig.term.display == rig.p.screen.display


def test_every_answer_to_a_cursor_query_is_swallowed(rig):
    rig.confident()
    rig.p.on_resize(30, 100, rig.now)
    rig.p.on_resize(31, 100, rig.now)        # dragging the window: two queries
    assert rig.p.filter_input(b"\x1b[1;4Rx\x1b[1;4R", rig.now) == (b"x", b"")
    assert not rig.p.quarantine
    assert rig.p.filter_input(b"\x1b[1;2R", rig.now) == (b"\x1b[1;2R", b"")   # Shift+F3


def test_a_late_answer_is_swallowed_too(rig):
    rig.p.on_resize(30, 100, rig.now)
    rig.tick(rig.p.DSR_TIMEOUT)              # given up on
    assert rig.p.filter_input(b"\x1b[1;1R", rig.now) == (b"", b"")
    rig.p.on_resize(31, 100, rig.now)
    rig.tick(rig.p.DSR_FORGET)               # no answer is coming
    assert rig.p.filter_input(b"\x1b[1;2R", rig.now) == (b"\x1b[1;2R", b"")


def test_end_of_paste_split_across_reads(rig):
    rig.confident()
    rig.type("\x1b[200~pasted\x1b[20")
    rig.server(b"pasted")
    rig.type("1~ok")
    assert not rig.p.in_paste
    rig.type("!")
    rig.server(b"ok")
    assert rig.line() == "$ epastedok!"


def test_tentative_epoch_survives_vim_style_normal_mode(rig):
    rig.server(b"line one\r\nline two")
    rig.server(b"\x1b[1;1H")                 # cursor home, like vim normal mode
    rig.type("j")                            # moves the cursor in vim, prints nothing
    rig.server(b"\x1b[2;1H")
    rig.tick(0.1)
    assert rig.term.display[0].rstrip() == "line one"
    assert rig.term.display[1].rstrip() == "line two"
