"""A bug in the predictor must never break the session."""
import io

from localecho import Guarded, Predictor


class Boom(Exception):
    pass


def breaking(p, name):
    def fail(*args):
        raise Boom(name)
    setattr(p, name, fail)
    return p


def test_failure_passes_output_through_with_what_was_held_back():
    p = Predictor(24, 80)
    g = Guarded(p)
    assert g.on_output(b"x\x1b[3", 0.0) == b"x"          # "\x1b[3" is held back
    breaking(p, "_feed")
    assert g.on_output(b"1mhi\x1b[", 0.1) == b"\x1b[31mhi\x1b["
    assert g.pred is None
    assert g.on_output(b"more", 0.2) == b"more"


def test_a_failing_deadline_falls_back_too():
    p = Predictor(24, 80)
    g = Guarded(p)
    g.on_output(b"\x1b]0;tit", 0.0)                      # held back
    breaking(p, "next_deadline")
    assert g.next_deadline(0.0) is None
    assert g.on_tick(0.1) == b"\x1b]0;tit"               # and not lost


def test_after_a_failure_everything_passes_through():
    g = Guarded(breaking(Predictor(24, 80), "on_input"))
    assert g.on_input(b"a", 0.0) == b""
    assert g.filter_input(b"\x1b[5;5R", 0.0) == (b"\x1b[5;5R", b"")
    assert g.next_deadline(0.0) is None
    assert g.on_tick(0.0) == g.on_resize(24, 80, 0.0) == g.finish() == b""


def test_failures_are_logged():
    log = io.StringIO()
    Guarded(breaking(Predictor(24, 80), "on_tick"), log).on_tick(0.0)
    assert "Boom: on_tick" in log.getvalue()


def test_without_a_predictor_everything_passes_through():
    g = Guarded(None)
    assert g.on_output(b"abc", 0.0) == b"abc"
    assert g.filter_input(b"k", 0.0) == (b"k", b"")
    assert g.next_deadline(0.0) is None
    assert g.on_input(b"k", 0.0) == g.on_tick(0.0) == g.finish() == b""


def test_finish_passes_on_held_output():
    g = Guarded(Predictor(24, 80))
    g.on_output(b"done\x1b[", 0.0)
    assert g.finish() == b"\x1b["
