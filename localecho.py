#!/usr/bin/env python3
"""localecho.py - run a command with mosh-style predictive local echo.

Usage:
    localecho.py [options] command [args...]

    localecho.py ssh user@host
    localecho.py --mark=grey et host

Options (must come before the command):
    --predict=MODE   adaptive (default): show predictions only when the
                     measured round trip is above ~30 ms.
                     always: show predictions whenever they are trusted.
                     never: pass everything straight through.
    --mark=STYLE     how to show characters the server hasn't echoed yet:
                     underline (default), grey, or none.  Once the echo
                     arrives the character is redrawn normally.
    --debug=FILE     append a log of prediction decisions to FILE.

Any interactive client whose echo comes back over a slow link can be
wrapped: ssh, et, docker or kubectl exec -it, telnet, a serial console.
There is no point wrapping mosh, which already predicts on its own.

How it works
------------
The command runs inside a pseudo-terminal, and everything it prints is passed
straight through to your terminal, so scrollback, colours, mouse support and
so on behave exactly as without localecho.  The same output is also fed to a
pyte terminal emulator, which keeps a "shadow" copy of what your screen
looks like.

When you type, the keystroke is sent to the command immediately and a guess
at its effect (the character appearing at the cursor, the rest of the line
shifting right, the cursor moving) is drawn on your screen straight away.
When the server's echo arrives, the guess is checked against the shadow
screen.

Like mosh, a guess is only *shown* once an earlier guess in the same
"epoch" has been confirmed by the server.  Pressing Enter, Tab, a control
key or anything else that can't be predicted starts a new epoch, so after a
password prompt appears nothing you type is drawn: the server never echoes
it, so the epoch is never confirmed.  As a second safeguard, nothing is
drawn while the cursor line looks like a password prompt.

Requires: pip install pyte
"""

import errno
import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import tty
from collections import defaultdict

try:
    import pyte
    from pyte import charsets as cs
    from pyte import graphics as g
    from pyte import modes as mo
    from pyte.screens import Char, Margins, StaticDefaultDict
    from wcwidth import wcwidth
except ImportError:  # pragma: no cover
    sys.exit("localecho: this script needs the pyte package (pip install pyte)")


# --------------------------------------------------------------------------
# Escape-sequence helpers
# --------------------------------------------------------------------------

# One token of terminal output that matters to us: a string sequence
# (OSC/DCS/APC/PM/SOS), a CSI sequence, or ESC 7 / ESC 8 / ESC c.
OUT_TOKEN_RE = re.compile(
    rb"\x1b(?:([\]P_^X])(.*?)(?:\x07|\x1b\\)"
    rb"|\[([\x30-\x3f]*)([\x20-\x2f]*)([\x40-\x7e])"
    rb"|([78c]))",
    re.S,
)
# Keys arriving from the user's terminal.
KEY_SEQ_RE = re.compile(rb"\x1b(?:\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]|O[\x40-\x7e])")
CPR_RE = re.compile(rb"\x1b\[(\d+);(\d+)R")
COMPLETE_ESC_RE = re.compile(
    rb"\x1b(?:\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"
    rb"|\][^\x07\x1b]*(?:\x07|\x1b\\)"
    rb"|[P_^X][^\x1b]*\x1b\\"
    rb"|(?![\[\]P_^X])[\x20-\x2f]*[\x30-\x7e])"
)
# Anything in bulk output that the fast path below can't skip over.
NOT_SKIPPABLE_RE = re.compile(rb"\x1b(?!\[[0-9;:]*m)|[\x00-\x06\x0b\x0c\x0e-\x1a\x1c-\x1f]")
PASSWORD_RE = re.compile(
    r"(?i)(pass(word|phrase|code)|\bpin\b|one-time|verification code|\botp\b)"
)


def split_safe(data):
    """Split output into (complete, incomplete-tail).

    We only ever inject our own drawing between server writes, never in the
    middle of an escape sequence or a UTF-8 character, so an unfinished
    sequence at the end of a read is held back until the rest arrives.
    """
    end = len(data)
    i = data.rfind(b"\x1b")
    if i != -1 and not COMPLETE_ESC_RE.match(data, i):
        tail = data[i:]
        # Only hold back something that could still become a sequence.
        if (len(tail) == 1 or tail[1:2] in b"[]P_^X"
                or 0x20 <= tail[1] <= 0x2f):
            end = i
            if len(tail) == 1:
                # A lone ESC may be the start of the ST that ends a string
                # sequence opened earlier in this read.
                j = data.rfind(b"\x1b", 0, i)
                if j != -1 and data[j + 1:j + 2] in b"]P_^X" \
                        and b"\x07" not in data[j:i]:
                    end = j
    # Don't cut a UTF-8 character in half.
    k = end
    back = 0
    while k > 0 and back < 3 and (data[k - 1] & 0xC0) == 0x80:
        k -= 1
        back += 1
    if k > 0 and data[k - 1] >= 0xC0:
        lead = data[k - 1]
        need = 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4
        if back + 1 < need:
            end = k - 1
    return data[:end], data[end:]


def _build_color_tables():
    fg = {v: k for k, v in g.FG_ANSI.items()}
    fg.update({v: k for k, v in g.FG_AIXTERM.items()})
    bg = {v: k for k, v in g.BG_ANSI.items()}
    bg.update({v: k for k, v in g.BG_AIXTERM.items()})
    idx = {}
    for i, h in enumerate(g.FG_BG_256):
        idx.setdefault(h, i)
    return fg, bg, idx


FG_CODE, BG_CODE, IDX_256 = _build_color_tables()


def _color(value, named, base):
    if value == "default":
        return None
    if value in named:
        return str(named[value])
    if len(value) == 6:
        if value in IDX_256:
            return "%d;5;%d" % (base, IDX_256[value])
        try:
            r, gg, b = (int(value[n:n + 2], 16) for n in (0, 2, 4))
            return "%d;2;%d;%d;%d" % (base, r, gg, b)
        except ValueError:
            return None
    return None


def char_style(c):
    """SGR sequence that reproduces a pyte Char's attributes."""
    parts = ["0"]
    if c.bold:
        parts.append("1")
    if c.italics:
        parts.append("3")
    if c.underscore:
        parts.append("4")
    if c.blink:
        parts.append("5")
    if c.reverse:
        parts.append("7")
    if c.strikethrough:
        parts.append("9")
    fgc = _color(c.fg, FG_CODE, 38)
    bgc = _color(c.bg, BG_CODE, 48)
    if fgc:
        parts.append(fgc)
    if bgc:
        parts.append(bgc)
    return ("\x1b[" + ";".join(parts) + "m").encode()


def goto(pos):
    return b"\x1b[%d;%dH" % (pos[0] + 1, pos[1] + 1)


# --------------------------------------------------------------------------
# Shadow terminal: pyte plus the few features it lacks that matter here
# --------------------------------------------------------------------------

class ShadowScreen(pyte.Screen):
    def __init__(self, columns, lines):
        self.in_alt = False
        self._main = None
        self._alt_saved = None
        self.scroll_count = 0
        self.region_scrolled = False
        self._last_char = ""
        super().__init__(columns, lines)

    def reset(self):
        super().reset()
        self.in_alt = False
        self._main = None
        self.region_scrolled = True

    def _new_buffer(self):
        return defaultdict(lambda: StaticDefaultDict(self.default_char))

    # Alternate screen (vim, less, htop ...). pyte has no alternate buffer.
    def _alt(self, on, save):
        if on == self.in_alt:
            return
        if on:
            if save:
                self._alt_saved = (self.cursor.x, self.cursor.y, self.cursor.attrs)
            self._main = self.buffer
            self.buffer = self._new_buffer()
        else:
            self.buffer = self._main if self._main is not None else self._new_buffer()
            self._main = None
            if save and self._alt_saved:
                self.cursor.x, self.cursor.y, self.cursor.attrs = self._alt_saved
        self.in_alt = on
        self.dirty.update(range(self.lines))
        self.region_scrolled = True

    def _split_alt(self, modes, kwargs, on):
        if not kwargs.get("private"):
            return modes
        rest = []
        for m in modes:
            if m in (47, 1047, 1049):
                self._alt(on, m == 1049)
            else:
                rest.append(m)
        return rest

    def set_mode(self, *modes, **kwargs):
        modes = self._split_alt(modes, kwargs, True)
        if modes:
            super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        modes = self._split_alt(modes, kwargs, False)
        if modes:
            super().reset_mode(*modes, **kwargs)

    # Track scrolling so predictions can move with the text.
    def _full_region(self):
        top, bottom = self.margins or Margins(0, self.lines - 1)
        return top == 0 and bottom == self.lines - 1

    def index(self):
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y == bottom:
            if self._full_region():
                self.scroll_count += 1
            else:
                self.region_scrolled = True
        super().index()

    def reverse_index(self):
        top, _bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y == top:
            self.region_scrolled = True
        super().reverse_index()

    def scroll_up(self, count=None, **kwargs):  # CSI S
        if kwargs.get("private"):
            return
        count = count or 1
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self._full_region():
            self.scroll_count += count
        else:
            self.region_scrolled = True
        for _ in range(min(count, bottom - top + 1)):
            for y in range(top, bottom):
                self.buffer[y] = self.buffer[y + 1]
            self.buffer.pop(bottom, None)
        self.dirty.update(range(self.lines))

    def scroll_down(self, count=None, **kwargs):  # CSI T
        if kwargs.get("private"):
            return
        count = count or 1
        top, bottom = self.margins or Margins(0, self.lines - 1)
        self.region_scrolled = True
        for _ in range(min(count, bottom - top + 1)):
            for y in range(bottom, top, -1):
                self.buffer[y] = self.buffer[y - 1]
            self.buffer.pop(top, None)
        self.dirty.update(range(self.lines))

    def draw(self, data):
        if data:
            self._last_char = data[-1]
        super().draw(data)

    def repeat_char(self, count=None, **kwargs):  # CSI b (REP)
        if self._last_char and not kwargs.get("private"):
            self.draw(self._last_char * min(count or 1, 1000))

    def sco_save(self, *args, **kwargs):  # CSI s
        if not args and not kwargs.get("private"):
            self.save_cursor()

    def sco_restore(self, *args, **kwargs):  # CSI u
        if not args and not kwargs.get("private"):
            self.restore_cursor()

    def write_process_input(self, data):
        pass  # the real terminal answers queries, not us


class ShadowStream(pyte.ByteStream):
    csi = dict(pyte.ByteStream.csi)
    csi.update({"S": "scroll_up", "T": "scroll_down", "b": "repeat_char",
                "s": "sco_save", "u": "sco_restore"})


def _tolerant(fn):
    """pyte raises on some unusual-but-valid sequences (e.g. extra
    parameters); ignore the sequence rather than lose the whole stream."""
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except (TypeError, ValueError, IndexError, KeyError):
            return None
    wrapper.__name__ = fn.__name__
    return wrapper


for _name in (set(ShadowStream.csi.values()) | set(ShadowStream.escape.values())
              | set(ShadowStream.basic.values()) | set(ShadowStream.sharp.values())):
    if hasattr(ShadowScreen, _name):
        setattr(ShadowScreen, _name, _tolerant(getattr(ShadowScreen, _name)))


# --------------------------------------------------------------------------
# Prediction engine
# --------------------------------------------------------------------------

class Key:
    """One predicted keystroke.

    cells: {(y, x): (text, sgr_bytes)} cells it changes on screen
    head:  (y, x, ch) for a typed character - the cell whose echo confirms it
    hard:  cells that must match before the keystroke counts as processed
           (a typed character's own cell, or everything a backspace changes;
           text we merely guessed would shift along is not required)
    bcur/ecur: predicted cursor before / after the keystroke
    """
    __slots__ = ("kind", "cells", "head", "hard", "bcur", "ecur", "t", "epoch")

    def __init__(self, kind, cells, head, bcur, ecur, t, epoch):
        self.kind, self.cells, self.head = kind, cells, head
        self.bcur, self.ecur, self.t, self.epoch = bcur, ecur, t, epoch
        if kind == "char":
            self.hard = {head[:2]}
        elif kind == "bs":
            self.hard = set(cells)
        else:
            self.hard = set()


class Predictor:
    HOLD_TIMEOUT = 0.05      # flush a partial escape sequence after this long
    HARD_TIMEOUT = 5.0       # give up on an unconfirmed prediction
    GRACE = 0.05             # wait this long for the rest of a split echo
    MAX_PEN = 32

    MARKS = {"underline": b"\x1b[4m", "grey": b"\x1b[90m", "none": b""}

    def __init__(self, rows, cols, mode="adaptive", mark="underline", log=None):
        self.screen = ShadowScreen(cols, rows)
        self.stream = ShadowStream(self.screen)
        self.mode = mode
        self.mark = self.MARKS[mark]
        self.log_file = log
        self.keys = []
        self.epoch = 1
        self.confirmed_epoch = 0
        self.srtt = None
        self.showing = mode == "always"
        self.blocked = False
        self.input_start = None
        self.in_paste = False
        self.shown = {}          # cells we have drawn over: (y,x) -> cell
        self.cursor_at = None    # where we left the real cursor, if moved
        self.held = b""
        self.held_since = 0.0
        self.unsafe = False      # real terminal may be mid-sequence
        self.pen = []            # SGR params since the last reset
        self.saved_pen = []
        self.alt_pen = []
        self.fed = 0
        self.quarantine = False         # no drawing until resynced after a resize
        self.dsr_ok = True
        self.dsr_pending = None
        self.dsr_tries = 0
        self.mismatch_since = None

    # ---- logging ---------------------------------------------------------
    def log(self, msg):
        if self.log_file:
            self.log_file.write("%.3f %s\n" % (time.monotonic(), msg))
            self.log_file.flush()

    # ---- server output ---------------------------------------------------
    def on_output(self, data, now):
        data = self.held + data
        self.held = b""
        safe, rest = split_safe(data)
        out = bytearray()
        if safe:
            out += self._render(hide=True)
            out += safe
            self._feed(safe)
            self.unsafe = False
        if rest:
            self.held = rest
            self.held_since = now
        if safe:
            self._evaluate(now)
            out += self._render()
        return bytes(out)

    def _feed(self, data):
        sc = self.screen
        sc.scroll_count = 0
        sc.region_scrolled = False
        data = self._fast_forward(data)
        self.stream.feed(OUT_TOKEN_RE.sub(self._token, data))
        self.fed += 1
        if sc.region_scrolled:
            if self.keys:
                self._reset("screen switched or region scrolled")
        elif sc.scroll_count:
            self._shift(-sc.scroll_count)

    def _fast_forward(self, data):
        """Skip emulating text that will scroll off screen anyway.

        pyte manages well under 1 MB/s, so for bulk output (cat, build logs)
        only the last screenful is fed to it. This is exact as long as the
        skipped part is just text, colours and line breaks."""
        sc = self.screen
        rows = sc.lines
        if sc.margins is not None or data.count(b"\r\n") <= rows + 1:
            return data
        pos = len(data)
        for _ in range(rows + 1):
            pos = data.rfind(b"\r\n", 0, pos)
        head, tail = data[:pos + 2], data[pos + 2:]
        if NOT_SKIPPABLE_RE.search(head):
            return data
        OUT_TOKEN_RE.sub(self._token, head)          # keep the pen up to date
        pen = b"\x1b[0m" + b"".join(b"\x1b[" + p + b"m" for p in self.pen if b":" not in p)
        self.stream.feed(b"\r" + b"\n" * rows + pen)
        sc.region_scrolled = True
        return tail

    def _token(self, m):
        """Track the SGR pen and strip sequences pyte would misparse."""
        if m.group(1) is not None:          # OSC / DCS / ... : pyte doesn't need them
            return b""
        if m.group(6) is not None:          # ESC 7 / ESC 8 / ESC c
            f = m.group(6)
            if f == b"7":
                self.saved_pen = list(self.pen)
            elif f == b"8":
                self.pen = list(self.saved_pen)
            else:
                self.pen = []
            return m.group(0)
        params, inter, final = m.group(3), m.group(4), m.group(5)
        if final == b"m" and not inter and params[:1] not in (b"<", b"=", b">", b"?"):
            fields = params.split(b";")
            if not params or b"0" in fields or b"" in fields:
                self.pen = [params] if params else []
            else:
                self.pen.append(params)
            return b"" if b":" in params else m.group(0)
        if params[:1] == b"?" and final not in b"hlJK":
            return b""                      # private sequences pyte doesn't know
        if inter or params[:1] in (b"<", b"=", b">"):
            if inter == b"!" and final == b"p":   # DECSTR soft reset
                self.pen = []
            return b""
        if b":" in params:
            return b""
        if not params and final == b"s":
            self.saved_pen = list(self.pen)
        elif not params and final == b"u":
            self.pen = list(self.saved_pen)
        elif final in b"hl" and params.startswith(b"?") and b"1049" in params.split(b";"):
            if final == b"h":
                self.alt_pen = list(self.pen)
            else:
                self.pen = list(self.alt_pen)
        return m.group(0)

    def _cell(self, y, x):
        return self.screen.buffer[y][x].data

    def _evaluate(self, now):
        sc = self.screen
        cur = (sc.cursor.y, sc.cursor.x)
        if self.keys:
            # Find the newest keystroke whose predicted screen state (the
            # cursor, plus every required cell of it and the keystrokes
            # before it) is what the server is showing now.
            hit = None
            state = {}
            for i, k in enumerate(self.keys):
                for pos, cell in k.cells.items():
                    state[pos] = (cell[0], pos in k.hard)
                if k.ecur == cur and all(self._cell(*pos) == text
                                         for pos, (text, hard) in state.items() if hard):
                    hit = i
            if hit is not None:
                k = self.keys[hit]
                chars = [k2 for k2 in self.keys[:hit + 1] if k2.kind == "char"]
                if chars:
                    self._confirm(max(chars, key=lambda k2: k2.epoch), now,
                                  sample=False)
                if k.kind == "char":
                    self._confirm(k, now, sample=True)
                del self.keys[:hit + 1]
                self.mismatch_since = None
            elif cur != self.keys[0].bcur:
                # Probably wrong - but the server may have written its echo
                # in several pieces, so give the rest a moment to arrive.
                if self.mismatch_since is None:
                    self.mismatch_since = now
                    if self.log_file:
                        self.log("mismatch: cursor=%s row=%r keys=%s" % (
                            cur, "".join(self._cell(cur[0], x) for x in range(min(sc.columns, 40))),
                            [(k.kind, k.head, k.bcur, k.ecur) for k in self.keys]))
            elif now - self.keys[0].t > self._late():
                self._reset("keystroke had no visible effect")
            else:
                self.mismatch_since = None
        if not self.keys:
            self.mismatch_since = None
            self.blocked = False

    def _late(self):
        if self.srtt is None:
            return 1.0
        return max(2.5 * self.srtt, self.srtt + 0.08)

    def _confirm(self, k, now, sample):
        if k.epoch > self.confirmed_epoch:
            self.confirmed_epoch = k.epoch
        if not sample:
            return
        rtt = now - k.t
        self.srtt = rtt if self.srtt is None else 0.875 * self.srtt + 0.125 * rtt
        if self.mode == "adaptive":
            if self.srtt > 0.030:
                self.showing = True
            elif self.srtt < 0.020:
                self.showing = False

    def _reset(self, why):
        if self.keys:
            self.log("reset: %s (%d pending)" % (why, len(self.keys)))
        self.keys.clear()
        self.epoch += 1
        self.blocked = False
        self.input_start = None
        self.mismatch_since = None

    def _shift(self, dy):
        def mv(p):
            return (p[0] + dy, p[1])
        for k in self.keys:
            if min(y for y, _ in list(k.cells) + [k.bcur, k.ecur]) + dy < 0:
                self._reset("prediction scrolled off screen")
                return
        for k in self.keys:
            k.cells = {mv(p): c for p, c in k.cells.items()}
            k.bcur, k.ecur = mv(k.bcur), mv(k.ecur)
            k.hard = {mv(p) for p in k.hard}
            if k.head:
                k.head = (k.head[0] + dy, k.head[1], k.head[2])
        if self.input_start:
            self.input_start = mv(self.input_start)
            if self.input_start[0] < 0:
                self.input_start = None

    # ---- keyboard input ---------------------------------------------------
    def filter_input(self, data, now):
        """Remove the answer to our own cursor-position query.

        Returns (data to forward, bytes to write to the terminal)."""
        if not self.dsr_pending:
            return data, b""
        m = CPR_RE.search(data)
        if not m:
            return data, b""
        data = data[:m.start()] + data[m.end():]
        _t, fed_then = self.dsr_pending
        self.dsr_pending = None
        if fed_then != self.fed:
            # Output arrived in between, so the answer may be stale; ask again.
            self.dsr_tries += 1
            return data, self._dsr_query(now) if self.dsr_tries < 5 else b""
        sc = self.screen
        y = min(int(m.group(1)) - 1, sc.lines - 1)
        x = min(int(m.group(2)) - 1, sc.columns - 1)
        if not (x == sc.columns - 1 and sc.cursor.x >= sc.columns - 1):
            sc.cursor.x = x
        sc.cursor.y = y
        if not sc.in_alt:
            # After a resize the terminal may have reflowed; forget what we
            # thought was to the right of and below the cursor.
            for xx in range(sc.cursor.x, sc.columns):
                sc.buffer[y].pop(xx, None)
            for yy in range(y + 1, sc.lines):
                sc.buffer.pop(yy, None)
        self.dsr_ok = True
        self.quarantine = False
        self.log("cursor resynced to %d,%d" % (y, x))
        return data, b""

    def _dsr_query(self, now):
        self.dsr_pending = (now, self.fed)
        return b"\x1b[6n"

    def on_input(self, data, now):
        i, n = 0, len(data)
        while i < n:
            b = data[i]
            if self.in_paste:
                j = data.find(b"\x1b[201~", i)
                if j < 0:
                    break
                self.in_paste = False
                i = j + 6
                continue
            if b == 0x1B:
                m = KEY_SEQ_RE.match(data, i)
                if m:
                    seq = m.group(0)
                    i = m.end()
                    if seq == b"\x1b[200~":
                        self.in_paste = True
                        self._barrier()
                    elif seq in (b"\x1b[D", b"\x1bOD"):
                        self._key_move(-1, now)
                    elif seq in (b"\x1b[C", b"\x1bOC"):
                        self._key_move(1, now)
                    elif seq in (b"\x1b[I", b"\x1b[O"):
                        pass  # focus in/out
                    else:
                        self._barrier()
                    continue
                i += 2 if i + 1 < n else 1      # lone Esc or Alt+key
                self._barrier()
                continue
            if b in (0x7F, 0x08):
                self._key_backspace(now)
                i += 1
                continue
            if b < 0x20:
                self._barrier()
                i += 1
                continue
            ln = 1 if b < 0x80 else 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            try:
                ch = data[i:i + ln].decode("utf-8")
            except UnicodeDecodeError:
                ch = None
            i += ln
            if ch is None:
                self._barrier()
            else:
                self._key_char(ch, now)
        out = self._render()
        if self.quarantine and not self.dsr_ok and not self.dsr_pending \
                and not self.held and not self.unsafe:
            out += self._dsr_query(now)
        return out

    def _barrier(self):
        """A key whose effect we can't predict: stop until the server catches up."""
        self.blocked = True
        self.epoch += 1
        self.input_start = None

    def _next_cursor(self):
        if self.keys:
            return self.keys[-1].ecur
        return (self.screen.cursor.y, self.screen.cursor.x)

    def _row(self, y):
        """Row y as it will look once all pending keystrokes are processed."""
        line = self.screen.buffer[y]
        row = [line[x] for x in range(self.screen.columns)]
        for k in self.keys:
            for (cy, cx), cell in k.cells.items():
                if cy == y:
                    row[cx] = cell
        return row

    @staticmethod
    def _blank(cell):
        return cell[0] in (" ", "")

    def _run_end(self, row, x):
        """Last column of the text that would move if we insert/delete at x.

        Text separated by three or more blanks (e.g. zsh's right prompt) is
        treated as something else and left alone."""
        last, gap = x - 1, 0
        for xx in range(x, len(row)):
            if self._blank(row[xx]):
                gap += 1
                if gap >= 3:
                    break
            else:
                gap, last = 0, xx
        return last

    @staticmethod
    def _as_cell(v):
        if isinstance(v, Char):
            return (v.data, char_style(v))
        return v

    def _narrow(self, row, lo, hi):
        for xx in range(max(lo, 0), min(hi, len(row) - 1) + 1):
            d = row[xx][0]
            if d == "" or (len(d) == 1 and wcwidth(d) > 1):
                return False
        return True

    def _pen_bytes(self):
        if len(self.pen) > self.MAX_PEN:
            return char_style(self.screen.cursor.attrs)
        return b"\x1b[0m" + b"".join(b"\x1b[" + p + b"m" for p in self.pen)

    def _key_char(self, ch, now):
        if self.blocked or self.in_paste:
            return
        if wcwidth(ch) != 1:
            self._barrier()
            return
        sc = self.screen
        y, x = self._next_cursor()
        if x >= sc.columns - 1 or sc.cursor.x >= sc.columns:
            self._barrier()
            return
        row = self._row(y)
        end = self._run_end(row, x)
        if end >= sc.columns - 1 or not self._narrow(row, x - 1, end + 1):
            self._barrier()
            return
        cells = {}
        for xx in range(end + 1, x, -1):       # insert: shift the rest right
            cells[(y, xx)] = self._as_cell(row[xx - 1])
        cells[(y, x)] = (ch, self._pen_bytes())
        if self.input_start is None or self.input_start[0] != y:
            self.input_start = (y, x)
        self.keys.append(Key("char", cells, (y, x, ch), (y, x), (y, x + 1),
                             now, self.epoch))

    def _key_backspace(self, now):
        if self.blocked or self.in_paste:
            return
        y, x = self._next_cursor()
        st = self.input_start
        # Only erase text we saw being typed, never the prompt.
        if x == 0 or st is None or st[0] != y or x - 1 < st[1]:
            self._barrier()
            return
        row = self._row(y)
        end = self._run_end(row, x)
        if not self._narrow(row, x - 1, end + 1):
            self._barrier()
            return
        cells = {}
        for xx in range(x - 1, max(end, x - 1) + 1):   # delete: shift left
            src = row[xx + 1] if xx + 1 <= end else (" ", self._pen_bytes())
            cells[(y, xx)] = self._as_cell(src)
        self.keys.append(Key("bs", cells, None, (y, x), (y, x - 1), now, self.epoch))

    def _key_move(self, d, now):
        if self.blocked or self.in_paste:
            return
        y, x = self._next_cursor()
        st = self.input_start
        nx = x + d
        if st is None or st[0] != y or nx < st[1] or nx >= self.screen.columns - 1:
            self._barrier()
            return
        if d > 0:
            row = self._row(y)
            if self._run_end(row, x) < x:
                self._barrier()          # nothing to the right to move over
                return
        self.keys.append(Key("mv", {}, None, (y, x), (y, nx), now, self.epoch))

    # ---- timers and resizes ----------------------------------------------
    def next_deadline(self, now):
        waits = []
        if self.held:
            waits.append(self.held_since + self.HOLD_TIMEOUT - now)
        if self.keys:
            waits.append(self.keys[0].t + self.HARD_TIMEOUT - now)
        if self.dsr_pending:
            waits.append(self.dsr_pending[0] + 1.0 - now)
        if self.mismatch_since is not None:
            waits.append(self.mismatch_since + self.GRACE - now)
        return max(0.0, min(waits)) if waits else None

    def on_tick(self, now):
        out = bytearray()
        changed = False
        if self.held and now - self.held_since >= self.HOLD_TIMEOUT:
            out += self._render(hide=True)
            out += self.held
            self._feed(self.held)
            self.held = b""
            self.unsafe = True
            self._evaluate(now)
            changed = True
        if self.mismatch_since is not None and now - self.mismatch_since >= self.GRACE:
            self._reset("server did something else")
            changed = True
        if self.keys and now - self.keys[0].t >= self.HARD_TIMEOUT:
            self._reset("timed out")
            changed = True
        if self.dsr_pending and now - self.dsr_pending[0] >= 1.0:
            self.dsr_pending = None
        if changed:
            out += self._render()
        return bytes(out)

    def on_resize(self, rows, cols, now):
        sc = self.screen
        sc.resize(rows, cols)
        sc.cursor.x = min(sc.cursor.x, cols - 1)
        sc.cursor.y = min(sc.cursor.y, rows - 1)
        # The terminal has already reflowed; anything we drew is now out of
        # our control, so just forget it.
        self.shown.clear()
        self.cursor_at = None
        self._reset("window resized")
        self.quarantine = True
        self.dsr_ok = False
        self.dsr_tries = 0
        self.dsr_pending = None
        if self.held or self.unsafe:
            return b""          # on_input asks later
        return self._dsr_query(now)

    def finish(self):
        return self._render(hide=True)

    # ---- drawing ----------------------------------------------------------
    def _password_prompt(self):
        sc = self.screen
        y = sc.cursor.y
        text = "".join(sc.buffer[y][x].data for x in range(min(sc.cursor.x, sc.columns)))
        if y > 0:
            text = "".join(sc.buffer[y - 1][x].data for x in range(sc.columns)) + text
        return PASSWORD_RE.search(text) is not None

    def _can_draw(self):
        if not self.showing or self.unsafe or self.quarantine:
            return False
        sc = self.screen
        if sc.cursor.x >= sc.columns:           # pending autowrap
            return False
        if mo.DECOM in sc.mode or mo.IRM in sc.mode:
            return False
        if (sc.g1_charset if sc.charset else sc.g0_charset) != cs.LAT1_MAP:
            return False
        return not self._password_prompt()

    def _restore_cell(self, pos):
        y, x = pos
        line = self.screen.buffer[y]
        c = line[x]
        if c.data == "" and x > 0:              # right half of a wide char
            x -= 1
            c = line[x]
        return goto((y, x)) + char_style(c) + (c.data or " ").encode("utf-8", "replace")

    def _render(self, hide=False):
        sc = self.screen
        desired, dcur = {}, None
        if not hide and self._can_draw():
            for k in self.keys:
                if k.epoch > self.confirmed_epoch:
                    break                        # not trusted yet
                desired.update(k.cells)
                dcur = k.ecur
        mark = self.mark
        out = bytearray()
        for pos in list(self.shown):
            if pos not in desired:
                out += self._restore_cell(pos)
                del self.shown[pos]
        for pos, (text, style) in desired.items():
            want = (text, style)
            if self.shown.get(pos) == want:
                continue
            if pos not in self.shown and text.strip() and self._cell(*pos) == text:
                continue                         # already looks right
            out += goto(pos) + style + mark + text.encode()
            self.shown[pos] = want
        shadow_cur = (sc.cursor.y, sc.cursor.x)
        target = dcur or shadow_cur
        if out:
            out += goto(target) + self._pen_bytes()
        elif self.cursor_at is not None and self.cursor_at != target:
            out += goto(target)
        elif self.cursor_at is None and target != shadow_cur:
            out += goto(target)
        else:
            return b""
        self.cursor_at = None if target == shadow_cur else target
        return bytes(out)


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------

def get_size(fd):
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        rows, cols = 0, 0
    return rows or 24, cols or 80


def set_size(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def write_all(fd, data):
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except InterruptedError:
            continue
        except BlockingIOError:
            select.select([], [fd], [])
            continue
        view = view[n:]


def query_cursor(timeout=1.0):
    """Ask the terminal where the cursor is. Returns ((y, x) or None, leftover)."""
    write_all(1, b"\x1b[6n")
    buf = b""
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None, buf
        r, _, _ = select.select([0], [], [], left)
        if r:
            chunk = os.read(0, 1024)
            if not chunk:
                return None, buf
            buf += chunk
            m = CPR_RE.search(buf)
            if m:
                return (int(m.group(1)) - 1, int(m.group(2)) - 1), buf[:m.start()] + buf[m.end():]


def parse_args(argv):
    opts = {"predict": "adaptive", "mark": "underline", "debug": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--predict="):
            opts["predict"] = a.split("=", 1)[1]
            if opts["predict"] not in ("adaptive", "always", "never"):
                sys.exit("localecho: --predict must be adaptive, always or never")
        elif a.startswith("--mark="):
            opts["mark"] = a.split("=", 1)[1]
            if opts["mark"] not in Predictor.MARKS:
                sys.exit("localecho: --mark must be underline, grey or none")
        elif a == "--no-underline":
            opts["mark"] = "none"
        elif a.startswith("--debug="):
            opts["debug"] = a.split("=", 1)[1]
        elif a == "--exec":
            pass                        # needed when ssh was implied; kept for old scripts
        elif a == "--":
            i += 1
            break
        elif a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        elif a.startswith("-"):
            sys.exit("localecho: unknown option %s (options go before the command, see --help)" % a)
        else:
            break
        i += 1
    cmd = argv[i:]
    if not cmd:
        sys.exit("usage: localecho.py [options] command [args...]  (see --help)")
    return opts, cmd


def main():
    opts, cmd = parse_args(sys.argv[1:])
    if not (os.isatty(0) and os.isatty(1)):
        os.execvp(cmd[0], cmd)          # nothing to predict without a terminal

    rows, cols = get_size(1)
    log = open(opts["debug"], "a") if opts["debug"] else None
    saved = termios.tcgetattr(0)

    def bail(signum, frame):
        raise SystemExit(128 + signum)
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, bail)

    status = 0
    child = None
    pred = None
    tty.setraw(0)
    try:
        pos, leftover = query_cursor()
        if pos is None:
            write_all(1, b"\x1b[H\x1b[2J")
            pos = (0, 0)

        child, master = pty.fork()
        if child == 0:
            try:
                os.execvp(cmd[0], cmd)
            except OSError as e:
                os.write(2, ("localecho: %s: %s\r\n" % (cmd[0], e.strerror)).encode())
            os._exit(127)
        set_size(master, rows, cols)

        if opts["predict"] != "never":
            pred = Predictor(rows, cols, mode=opts["predict"],
                             mark=opts["mark"], log=log)
            pred.screen.cursor.y = min(pos[0], rows - 1)
            pred.screen.cursor.x = min(pos[1], cols - 1)

        def give_up(raw):
            """A bug in the predictor must never break the session: log it
            and fall back to plain pass-through."""
            nonlocal pred
            if log:
                import traceback
                log.write(traceback.format_exc())
                log.flush()
            held = pred.held if pred else b""
            pred = None
            return held + raw

        if leftover:
            write_all(master, leftover)

        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_w, False)
        signal.set_wakeup_fd(wake_w)
        resized = [False]
        signal.signal(signal.SIGWINCH, lambda s, f: resized.__setitem__(0, True))

        while True:
            now = time.monotonic()
            timeout = pred.next_deadline(now) if pred else None
            try:
                ready, _, _ = select.select([0, master, wake_r], [], [], timeout)
            except InterruptedError:
                ready = []
            now = time.monotonic()
            if wake_r in ready:
                os.read(wake_r, 512)
            if resized[0]:
                resized[0] = False
                rows, cols = get_size(1)
                set_size(master, rows, cols)
                if pred:
                    try:
                        write_all(1, pred.on_resize(rows, cols, now))
                    except Exception:
                        write_all(1, give_up(b""))
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError as e:
                    if e.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    break
                if pred:
                    try:
                        data = pred.on_output(data, now)
                    except Exception:
                        data = give_up(data)
                write_all(1, data)
            if 0 in ready:
                data = os.read(0, 4096)
                if not data:
                    break
                if pred:
                    try:
                        data, extra = pred.filter_input(data, now)
                    except Exception:
                        extra = give_up(b"")
                    write_all(1, extra)
                if data:
                    write_all(master, data)
                    if pred:
                        try:
                            write_all(1, pred.on_input(data, now))
                        except Exception:
                            write_all(1, give_up(b""))
            if pred:
                try:
                    write_all(1, pred.on_tick(now))
                except Exception:
                    write_all(1, give_up(b""))
    finally:
        try:
            if pred is not None:
                write_all(1, pred.finish() + pred.held)
        except Exception:
            pass
        termios.tcsetattr(0, termios.TCSAFLUSH, saved)
        if child:
            try:
                _, st = os.waitpid(child, 0)
                status = os.waitstatus_to_exitcode(st)
                if status < 0:
                    status = 128 - status
            except ChildProcessError:
                pass
    sys.exit(status)


if __name__ == "__main__":
    main()
