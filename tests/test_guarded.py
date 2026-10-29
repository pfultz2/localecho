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


def test_output_passes_straight_through_before_and_after_a_failure():
    p = Predictor(24, 80)
    g = Guarded(p)
    assert g.on_output(b"x\x1b[3", 0.0) == b"x\x1b[3"    # unfinished, but not held back
    breaking(p, "_feed")
    assert g.on_output(b"1mhi\x1b[", 0.1) == b"1mhi\x1b["
    assert g.pred is None
    assert g.on_output(b"more", 0.2) == b"more"
    assert g.finish() == b""


def test_failure_takes_drawn_predictions_off_first(make_rig):
    rig = make_rig().confident()
    rig.type("ch")                       # drawn, with the real cursor two columns ahead
    p = rig.p
    rig.p = Guarded(p)
    breaking(p, "_feed")
    rig.server(b"ch")
    assert rig.p.pred is None
    assert rig.line() == "$ ech" and rig.cursor() == (0, 5)
    assert not any(rig.cell(x).underscore for x in range(6))


def test_a_failing_deadline_cleans_up_on_the_next_tick(make_rig):
    rig = make_rig().confident()
    rig.type("ch")
    p = rig.p
    rig.p = Guarded(breaking(p, "next_deadline"))
    assert rig.p.next_deadline(rig.now) is None
    rig.tick(0.01)
    assert rig.line() == "$ e" and rig.cursor() == (0, 3)


def test_after_a_failure_everything_passes_through():
    g = Guarded(breaking(Predictor(24, 80), "on_input"))
    assert g.on_input(b"a", 0.0) == b""
    assert g.filter_input(b"\x1b[5;5R", 0.0) == (b"\x1b[5;5R", b"")
    assert g.next_deadline(0.0) is None
    assert g.on_tick(0.0) == g.on_resize(24, 80, 0.0) == g.finish() == b""


def test_failures_are_logged():
    log = io.StringIO()
    g = Guarded(breaking(Predictor(24, 80), "on_tick"), log)
    g.on_tick(0.0)
    g.write_log(1.5)
    assert log.getvalue().startswith("1.500 Traceback")
    assert "Boom: on_tick" in log.getvalue()


def test_the_predictor_does_no_logging_of_its_own(make_rig):
    log = io.StringIO()
    rig = make_rig()
    rig.p = Guarded(rig.p, log)
    rig.confident()
    rig.type("\r")                          # a barrier, which is logged ...
    assert log.getvalue() == ""             # ... but not written yet
    rig.p.write_log(rig.now)
    assert log.getvalue() == "%.3f barrier: key b'\\r' (input from (0, 2))\n" % rig.now


def test_without_a_predictor_everything_passes_through():
    g = Guarded(None)
    assert g.on_output(b"abc", 0.0) == b"abc"
    assert g.filter_input(b"k", 0.0) == (b"k", b"")
    assert g.next_deadline(0.0) is None
    assert g.on_input(b"k", 0.0) == g.on_tick(0.0) == g.finish() == b""
