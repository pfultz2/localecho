"""The bulk-output shortcut must leave the same screen as full emulation."""
import random

import pytest

from localecho import Predictor, ShadowScreen, ShadowStream


def random_output(rnd):
    lines = []
    for _ in range(rnd.randint(10, 300)):
        n = rnd.choice([0, 5, 30, 79, 80, 81, 170])
        line = "".join(rnd.choice("abc  xyz012") for _ in range(n))
        if rnd.random() < 0.3:
            colour = rnd.choice(["1;31", "32", "0", "38;5;208", "4"])
            line = "\x1b[%sm%s%s" % (colour, line, "\x1b[0m" if rnd.random() < 0.5 else "")
        lines.append(line)
    return ("\r\n".join(lines) + "\r\n" + rnd.choice(["$ ", ""])).encode()


@pytest.mark.parametrize("seed", range(40))
def test_fast_forward_matches_full_emulation(seed):
    rnd = random.Random(seed)
    rows, cols = rnd.choice([(24, 80), (10, 40), (50, 132)])
    p = Predictor(rows, cols)
    ref = ShadowScreen(cols, rows)
    ref_stream = ShadowStream(ref)
    p.screen.cursor.y = ref.cursor.y = rnd.randrange(rows)
    data = random_output(rnd)
    i = 0
    while i < len(data):
        n = rnd.choice([1, 7, 100, 4096, 65536])
        p.on_output(data[i:i + n], 0.0)
        ref_stream.feed(data[i:i + n])
        i += n
    assert p.screen.display == ref.display
    assert (p.screen.cursor.y, p.screen.cursor.x) == (ref.cursor.y, ref.cursor.x)
    assert p.screen.cursor.attrs == ref.cursor.attrs


def test_cursor_addressing_disables_the_shortcut():
    p = Predictor(5, 20)
    p.on_output(b"x\r\n" * 10 + b"\x1b[1;1Htop" + b"\r\ny" * 10, 0.0)
    ref = ShadowScreen(20, 5)
    ShadowStream(ref).feed(b"x\r\n" * 10 + b"\x1b[1;1Htop" + b"\r\ny" * 10)
    assert p.screen.display == ref.display
