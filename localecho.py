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

Some programs, such as Claude Code and other Ink applications, hide the
terminal's cursor and draw their own as a reverse-video cell.  Then the
guess is made at that cell, and the drawn cursor moves along with the text.

Like mosh, a guess is only *shown* once an earlier guess in the same
"epoch" has been confirmed by the server.  Pressing Enter, Tab, a control
key or anything else that can't be predicted starts a new epoch, so after a
password prompt appears nothing you type is drawn: the server never echoes
it, so the epoch is never confirmed.  As a second safeguard, nothing is
drawn while the cursor line looks like a password prompt.

Requires: pip install pyte
"""

import copy
import errno
import fcntl
import functools
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import traceback
import tty
from collections import defaultdict

try:
    import pyte
    from pyte import charsets as cs
    from pyte import graphics as g
    from pyte import modes as mo
    from pyte.screens import Char, Cursor, Margins
    from wcwidth import wcwidth
except ImportError:  # pragma: no cover
    sys.exit("localecho: this script needs the pyte package (pip install pyte)")


# --------------------------------------------------------------------------
# Escape-sequence helpers
# --------------------------------------------------------------------------

# One escape sequence in terminal output, as far as the terminal's parser
# is concerned.  A CAN, SUB or ESC cuts any sequence short (the ESC starts
# a new one), except that a DCS ends only with ST: tmux passes the
# sequences inside it on with each ESC doubled.  Groups: csi and final for
# a CSI (no final byte if it was cut short), str for a string sequence
# (OSC, DCS, APC, PM, SOS), esc for the final byte of any other sequence.
OUT_TOKEN_RE = re.compile(
    rb"\x1b(?:"
    rb"\[(?P<csi>[^\x18\x1a\x1b\x40-\x7e]*)(?P<final>[\x40-\x7e]|(?=[\x18\x1a\x1b]))"
    rb"|(?P<str>P[^\x1b]*(?:\x1b[^\\][^\x1b]*)*\x1b\\"
    rb"|\][^\x07\x18\x1a\x1b]*(?:\x07|(?=[\x18\x1a\x1b]))"
    rb"|[_^X][^\x18\x1a\x1b]*(?=[\x18\x1a\x1b]))"
    rb"|(?![\[\]P_^X])[\x20-\x2f]*(?:(?P<esc>[\x30-\x7e])|(?=[^\x20-\x7e])))"
)
# The longest run of output made of complete sequences and text.  Without
# the groups, which would make it twice as slow.
COMPLETE_RE = re.compile(
    rb"(?:[^\x1b]+|" + re.sub(rb"\(\?P<\w+>", b"(?:", OUT_TOKEN_RE.pattern) + rb")*")
STRING_KINDS = (b"P", b"]", b"_", b"^", b"X")     # DCS, OSC, APC, PM, SOS
DCS_OPEN_RE = re.compile(rb"\x1bP[^\x1b]*(?:\x1b[^\\][^\x1b]*)*")
CSI_BODY_RE = re.compile(rb"([\x30-\x3f]*)([\x20-\x2f]*)")
# Controls that terminals ignore, but that make pyte drop the rest of the text.
IGNORED_C0_RE = re.compile(rb"[\x00-\x06\x10-\x1a\x1c-\x1f]")
# Keys arriving from the user's terminal.
KEY_SEQ_RE = re.compile(rb"\x1b(?:\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]|O[\x40-\x7e])")
CPR_RE = re.compile(rb"\x1b\[(\d+);(\d+)R")
PASTE_END = b"\x1b[201~"
# Anything in bulk output that the fast path below can't skip over.
NOT_SKIPPABLE_RE = re.compile(rb"\x1b(?!\[[0-9;:]*m)|[\x00-\x06\x0b\x0c\x0e-\x1a\x1c-\x1f]")
PASSWORD_RE = re.compile(
    r"(?i)(pass(word|phrase|code)|\bpin\b|one-time|verification code|\botp\b)"
)


def split_safe(data):
    """Split output into (complete, incomplete-tail).

    The tail is an escape sequence or UTF-8 character that the output ends
    in the middle of.  Until it is finished, the terminal is in the middle
    of it too, so we mustn't draw, and the shadow screen waits for the rest.
    Of an unfinished string sequence (OSC, DCS, ...) only what decides where
    it ends is kept: nobody needs its contents.
    """
    end = COMPLETE_RE.match(data).end()
    if end < len(data):
        tail = data[end:]
        kind = tail[1:2]
        if kind == b"P":
            # Keep an ESC at the end: it may be the start of the ST.
            return data[:end], b"\x1bP" + tail[DCS_OPEN_RE.match(tail).end():]
        if kind in STRING_KINDS:
            return data[:end], tail[:2]
        return data[:end], tail
    # Don't cut a UTF-8 character in half.
    k = end
    while k > max(0, end - 3) and (data[k - 1] & 0xC0) == 0x80:
        k -= 1
    if k > 0 and data[k - 1] >= 0xC0:
        lead = data[k - 1]
        need = 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4
        if end - k + 1 < need:
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
            r, gg, b = bytes.fromhex(value)
            return "%d;2;%d;%d;%d" % (base, r, gg, b)
        except ValueError:
            return None
    return None


SGR_FLAGS = [(name[1:], str(code)) for code, name in g.TEXT.items() if name[0] == "+"]


def char_style(c):
    """SGR sequence that reproduces a pyte Char's attributes."""
    parts = ["0"] + [code for attr, code in SGR_FLAGS if getattr(c, attr)]
    parts += filter(None, (_color(c.fg, FG_CODE, 38), _color(c.bg, BG_CODE, 48)))
    return ("\x1b[" + ";".join(parts) + "m").encode()


def goto(pos):
    return b"\x1b[%d;%dH" % (pos[0] + 1, pos[1] + 1)


def move(frm, to, cols):
    """Relative cursor movement.  Column `cols` stands for the last column
    with a wrap pending; stepping back first cancels that."""
    (y, x), out = frm, b""
    if x >= cols:
        out, x = b"\x1b[D", cols - 2
    dy, dx = to[0] - y, to[1] - x
    if dy:
        out += b"\x1b[%d%s" % (abs(dy), b"B" if dy > 0 else b"A")
    if dx:
        out += b"\x1b[%d%s" % (abs(dx), b"C" if dx > 0 else b"D")
    return out


def sgr_resets(params):
    """Does SGR `params` reset every attribute, so that earlier SGRs no
    longer matter?  (The 0 in 38;5;0 is a colour, not a reset.)"""
    fields = params.split(b";")
    i = 0
    while i < len(fields):
        f = fields[i]
        if f in (b"", b"0"):
            return True
        i += 1
        if f in (b"38", b"48", b"58"):          # colour: skip its arguments
            i += {b"5": 2, b"2": 4}.get(fields[i] if i < len(fields) else b"", 0)
    return False


# --------------------------------------------------------------------------
# Shadow terminal: pyte plus the few features it lacks that matter here
# --------------------------------------------------------------------------

MAX_PEN = 32        # SGRs to replay before falling back to pyte's attributes


class PenCursor(Cursor):
    """pyte's cursor plus its pen: the SGR parameters set since the last
    full reset, exactly as the server sent them.  Replaying the pen is how
    the real terminal gets its colours back after we draw; pyte's attrs
    can't hold all of it (dim, undercurl, underline colour ...).  None if
    there are too many to replay.  Saving the cursor saves the pen too."""
    __slots__ = ("pen",)

    def __init__(self, x, y, attrs=Char(" ")):
        super().__init__(x, y, attrs)
        self.pen = ()


class ShadowScreen(pyte.Screen):
    def __init__(self, columns, lines):
        self.scroll_count = 0
        self._last_char = ""
        super().__init__(columns, lines)

    def reset(self):
        super().reset()
        self.cursor = PenCursor(0, 0)
        self.savepoints = []
        self.g1_charset = cs.LAT1_MAP   # as in xterm; pyte defaults to line drawing
        self._main = None
        self.region_scrolled = True

    @property
    def in_alt(self):
        return self._main is not None

    # Alternate screen (vim, less, htop ...). pyte has no alternate buffer.
    # As in xterm, each screen has its own saved cursor.
    def _alt(self, on, save):
        if on == self.in_alt:
            return
        if on:
            if save:
                self.save_cursor()
            self._main = (self.buffer, self.savepoints)
            self.buffer = defaultdict(self.buffer.default_factory)
            self.savepoints = []
        else:
            (self.buffer, self.savepoints), self._main = self._main, None
            if save:
                self.restore_cursor()
        self.dirty.update(range(self.lines))
        self.region_scrolled = True

    # A terminal has one saved cursor, not pyte's stack: saving again
    # replaces it, and restoring doesn't use it up.  Nor does it save
    # whether the cursor is hidden.
    def save_cursor(self):
        super().save_cursor()
        del self.savepoints[:-1]

    def restore_cursor(self):
        hidden = self.cursor.hidden
        if self.savepoints:
            sp = self.savepoints[-1]
            self.savepoints.append(sp._replace(cursor=copy.copy(sp.cursor)))
        super().restore_cursor()
        self.cursor.hidden = hidden

    def resize(self, lines=None, columns=None):
        lines = lines or self.lines
        if lines < self.lines:
            # Terminals get shorter by losing the lines below the cursor,
            # scrolling lines off the top only to keep the cursor in view.
            # pyte always takes them off the top.
            top = max(0, self.cursor.y + 1 - lines)
            self.buffer = defaultdict(self.buffer.default_factory,
                                      {y - top: line for y, line in self.buffer.items()
                                       if top <= y < top + lines})
            self.cursor.y -= top
            self.lines = lines
            self.set_margins()
            self.dirty.update(range(lines))
        # pyte saves and restores the cursor around its own work.
        saved, self.savepoints = self.savepoints, []
        super().resize(lines, columns)
        self.savepoints = saved

    def select_graphic_rendition(self, *attrs, private=False):
        if private:
            # Sub-parameters pyte can't parse, passed on by Predictor._token:
            # the pen gets the lot, pyte the fields without any.
            params = bytes(attrs)
            if b":" in params:
                self._add_pen(params)
                plain = [int(f or 0) for f in params.split(b";") if b":" not in f]
                if plain:
                    super().select_graphic_rendition(*plain)
            return
        self._add_pen(b";".join(b"%d" % a for a in attrs))
        super().select_graphic_rendition(*attrs)

    def _add_pen(self, params):
        pen = self.cursor.pen
        if sgr_resets(params):
            pen = () if params == b"0" else (params,)
        elif pen is not None:
            pen = pen + (params,) if len(pen) < MAX_PEN else None
        self.cursor.pen = pen

    def _own_modes(self, modes, kwargs, on):
        """Handle the private modes pyte gets wrong; the rest are for pyte."""
        if not kwargs.get("private"):
            return modes
        rest = []
        for m in modes:
            if m in (47, 1047, 1049):
                self._alt(on, m == 1049)
            elif m != 3:
                # 3 is 80/132 columns (DECCOLM), which terminals ignore these
                # days, but pyte resizes for.  `reset` sends it.
                rest.append(m)
        return rest

    def set_mode(self, *modes, **kwargs):
        modes = self._own_modes(modes, kwargs, True)
        if modes:
            super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        modes = self._own_modes(modes, kwargs, False)
        if modes:
            super().reset_mode(*modes, **kwargs)

    # Track scrolling so predictions can move with the text.
    def _region(self):
        return self.margins or Margins(0, self.lines - 1)

    def index(self):
        top, bottom = self._region()
        if self.cursor.y == bottom:
            if (top, bottom) == (0, self.lines - 1):
                self.scroll_count += 1
            else:
                self.region_scrolled = True
        super().index()

    def reverse_index(self):
        if self.cursor.y == self._region().top:
            self.region_scrolled = True
        super().reverse_index()

    def _scroll(self, count, y, step):
        saved, self.cursor.y = self.cursor.y, y
        for _ in range(min(count or 1, self.lines)):
            step()
        self.cursor.y = saved

    def scroll_up(self, count=None, **kwargs):  # CSI S
        if not kwargs.get("private"):
            self._scroll(count, self._region().bottom, self.index)

    def scroll_down(self, count=None, **kwargs):  # CSI T
        if not kwargs.get("private"):
            self._scroll(count, self._region().top, self.reverse_index)

    def draw(self, data):
        if data:
            self._last_char = data[-1]
        super().draw(data)

    def repeat_char(self, count=None, **kwargs):  # CSI b (REP)
        if self._last_char and not kwargs.get("private"):
            self.draw(self._last_char * min(count or 1, 1000))

    # CSI s / CSI u.  pyte passes a 0 when there are no parameters; with
    # parameters, CSI s sets left and right margins instead.
    def sco_save(self, *args, **kwargs):
        if not any(args) and not kwargs.get("private"):
            self.save_cursor()

    def sco_restore(self, *args, **kwargs):
        if not any(args) and not kwargs.get("private"):
            self.restore_cursor()

    def write_process_input(self, data):
        pass  # the real terminal answers queries, not us


class ShadowStream(pyte.ByteStream):
    csi = dict(pyte.ByteStream.csi)
    csi.update({"S": "scroll_up", "T": "scroll_down", "b": "repeat_char",
                "s": "sco_save", "u": "sco_restore"})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Real terminals honour ESC ( 0 and SO/SI (line drawing) in UTF-8
        # too, but pyte ignores them while use_utf8 is set.  feed() below
        # always decodes UTF-8 itself, so the flag can be off.
        self.use_utf8 = False

    def feed(self, data):
        pyte.Stream.feed(self, self.utf8_decoder.decode(data))

    def _send_to_parser(self, data):
        # pyte's parser itself raises on a few things, such as a digit
        # other than 0-9 in a CSI.  It starts afresh; drop the sequence.
        try:
            return super()._send_to_parser(data)
        except (TypeError, ValueError, IndexError, KeyError):
            return True

    def select_other_charset(self, code):
        pass  # ESC % @ / ESC % G: stay with UTF-8


def _tolerant(fn):
    """pyte raises on some unusual-but-valid sequences (e.g. extra
    parameters); ignore the sequence rather than lose the whole stream."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except (TypeError, ValueError, IndexError, KeyError):
            return None
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

    cells: {(y, x): (text, sgr_bytes) or Char} cells it changes on screen
    head:  (y, x, ch) for a typed character - the cell whose echo confirms it
    hard(pos): must this cell match before the keystroke counts as processed?
           (a typed character's own cell, or everything a backspace changes;
           text we merely guessed would shift along is not required)
    bcur/ecur: predicted cursor before / after the keystroke
    """
    __slots__ = ("kind", "cells", "bcur", "ecur", "t", "epoch")

    def __init__(self, kind, cells, bcur, ecur, t, epoch):
        self.kind, self.cells = kind, cells
        self.bcur, self.ecur, self.t, self.epoch = bcur, ecur, t, epoch

    @property
    def head(self):
        return self.bcur + (self.cells[self.bcur][0],) if self.kind == "char" else None

    def hard(self, pos):
        return self.kind == "bs" or pos == self.bcur


class Predictor:
    HARD_TIMEOUT = 5.0       # give up on an unconfirmed prediction
    GRACE = 0.05             # wait this long for the rest of a split echo
    DSR_TIMEOUT = 1.0        # stop waiting for an answer to our cursor query
    DSR_FORGET = 5.0         # ... and stop expecting one at all
    STRING_TIMEOUT = 5.0     # give up on an unfinished string sequence, as tmux does

    MARKS = {"underline": b"\x1b[4m", "grey": b"\x1b[90m", "none": b""}
    MOVES = {b"\x1b[D": -1, b"\x1bOD": -1, b"\x1b[C": 1, b"\x1bOC": 1}

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
        self.paste_tail = b""    # may be the start of the end-of-paste marker
        self.shown = {}          # cells we have drawn over: (y,x) -> cell
        self.cursor_at = None    # where we left the real cursor, if moved
        self.held = b""          # output the terminal is in the middle of
        self.string_since = None        # when the string sequence in held started
        self.fed = 0
        self.quarantine = False         # no drawing until resynced after a resize
        self.dsr_pending = None         # (sent, fed then) for the query we wait on
        self.dsr_sent = []              # when queries still to be answered were sent
        self.dsr_tries = 0
        self.mismatch_since = None
        self.block = None        # (fed, candidates) for the cursor the application draws
        self.caret = None        # its cell, when the pending keys are typed at it

    # ---- logging ---------------------------------------------------------
    def log(self, msg):
        if self.log_file:
            self.log_file.write("%.3f %s\n" % (time.monotonic(), msg))
            self.log_file.flush()

    # ---- server output ---------------------------------------------------
    def on_output(self, data, now):
        """Pass server output through, with our drawing lifted off first.

        The shadow screen gets only complete sequences; while the terminal
        is in the middle of one, we draw nothing."""
        out = self._render(hide=True) + data
        safe, self.held = split_safe(self.held + data)
        if self.held[1:2] not in STRING_KINDS:
            self.string_since = None
        elif safe or self.string_since is None:
            self.string_since = now             # a new one
        if safe:
            self._feed(safe)
            self._evaluate(now)
        return out + self._render()

    def _feed(self, data):
        sc = self.screen
        sc.scroll_count = 0
        sc.region_scrolled = False
        data = self._fast_forward(data)
        self.stream.feed(IGNORED_C0_RE.sub(b"", OUT_TOKEN_RE.sub(self._token, data)))
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
        skipped part is just text, colours and line breaks, and takes the
        cursor to the bottom row: then the rest scrolls everything else
        away, even what was on the screen before."""
        sc = self.screen
        rows = sc.lines
        if (sc.margins is not None
                or data.count(b"\r\n") - rows < max(1, rows - 1 - sc.cursor.y)):
            return data
        pos = len(data)
        for _ in range(rows + 1):
            pos = data.rfind(b"\r\n", 0, pos)
        head, tail = data[:pos + 2], data[pos + 2:]
        if NOT_SKIPPABLE_RE.search(head):
            return data
        # Replay the colours.  Every escape in head is an SGR, and only the
        # ones from the last full reset on matter.
        start = max(head.rfind(b"\x1b[0m"), head.rfind(b"\x1b[m"), 0)
        sgr = b"".join(map(self._token, OUT_TOKEN_RE.finditer(head, start)))
        self.stream.feed(b"\r" + b"\n" * rows + sgr)
        sc.region_scrolled = True
        return tail

    def _token(self, m):
        """Strip sequences pyte would misparse, and translate the few it
        would get wrong."""
        if m.group("str") is not None:      # OSC / DCS / ... : pyte doesn't need them
            return b""
        body, final = m.group("csi"), m.group("final")
        if body is None:                    # any other escape sequence
            return m.group(0) if m.group("esc") else b""
        ctl, p = b"", CSI_BODY_RE.fullmatch(body)
        if p is None:
            # Controls in the middle take effect at once.  Anything else
            # (DEL, non-ASCII) makes it a sequence that pyte could choke on.
            ctl = bytes(c for c in body if c < 0x20)
            p = CSI_BODY_RE.fullmatch(bytes(c for c in body if c >= 0x20))
        if p is None or not final:          # (or it was cut short)
            return ctl
        params, inter = p.groups()
        seq = b"\x1b[" + params + inter + final
        if final == b"m" and not inter and params[:1] not in (b"<", b"=", b">", b"?"):
            if b":" in params:
                # pyte can't parse sub-parameters (4:3 is undercurl), so send
                # the raw bytes on to the screen's pen as a private SGR, which
                # keeps them in order with everything else.
                return ctl + b"\x1b[?" + b";".join(b"%d" % c for c in params) + b"m"
            return ctl + seq
        if params[:1] == b"?" and final not in b"hlJK":
            return ctl                      # private sequences pyte doesn't know
        if inter or params[:1] in (b"<", b"=", b">"):
            if inter == b"!" and final == b"p":   # DECSTR soft reset
                return ctl + b"\x1b[m"
            return ctl
        if b":" in params:
            return ctl
        return ctl + seq

    def _cell(self, y, x):
        return self.screen.buffer[y][x].data

    def _cursor(self):
        """Where typed text goes: the terminal's cursor or, if the
        application has hidden it, the cursor it draws itself.  None if
        there is no telling."""
        sc = self.screen
        if not sc.cursor.hidden:
            return (sc.cursor.y, sc.cursor.x)
        if self.block is None or self.block[0] != self.fed:
            self.block = (self.fed, self._find_blocks())
        found = self.block[1]
        return found[0] if len(found) == 1 else None

    def _find_blocks(self):
        """Ink applications (Claude Code, for one) hide the cursor and show
        a reverse-video cell instead.  Find up to two reverse-video cells
        standing on their own (bars don't count): the cursor, if it is the
        only one."""
        sc = self.screen
        found = []
        if mo.DECSCNM in sc.mode:               # the whole screen is reversed
            return found
        for y, line in sc.buffer.items():
            if y >= sc.lines:
                continue
            for x, c in line.items():
                if (c.reverse and x < sc.columns
                        and not line[x - 1].reverse and not line[x + 1].reverse):
                    found.append((y, x))
                    if len(found) == 2:
                        return found
        return found

    def _evaluate(self, now):
        sc = self.screen
        if self.keys:
            cur = self._cursor()
            # Find the newest keystroke whose predicted screen state (the
            # cursor, plus every required cell of it and the keystrokes
            # before it) is what the server is showing now.
            hit = None
            state = {}
            for i, k in enumerate(self.keys):
                for pos, cell in k.cells.items():
                    state[pos] = (cell[0], k.hard(pos))
                if k.ecur == cur and all(self._cell(*pos) == text
                                         for pos, (text, hard) in state.items() if hard):
                    hit = i
            if hit is not None:
                done = self.keys[:hit + 1]
                del self.keys[:hit + 1]
                self.confirmed_epoch = max([self.confirmed_epoch]
                                           + [k.epoch for k in done if k.kind == "char"])
                if done[-1].kind == "char":
                    self._sample_rtt(now - done[-1].t)
                self.mismatch_since = None
            elif cur != self.keys[0].bcur:
                # Probably wrong - but the server may have written its echo
                # in several pieces, so give the rest a moment to arrive.
                if self.mismatch_since is None:
                    self.mismatch_since = now
                    if self.log_file:
                        y = (cur or self.keys[0].bcur)[0]
                        self.log("mismatch: cursor=%s row=%r keys=%s" % (
                            cur, "".join(self._cell(y, x) for x in range(min(sc.columns, 40))),
                            [(k.kind, k.head, k.bcur, k.ecur) for k in self.keys]))
            elif now - self.keys[0].t > self._late():
                self._reset("keystroke had no visible effect")
            else:
                self.mismatch_since = None
        if not self.keys:
            self.blocked = False

    def _late(self):
        if self.srtt is None:
            return 1.0
        return max(2.5 * self.srtt, self.srtt + 0.08)

    def _sample_rtt(self, rtt):
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
        if any(k.bcur[0] + dy < 0 for k in self.keys):
            return self._reset("prediction scrolled off screen")
        for k in self.keys:
            k.cells = {mv(p): c for p, c in k.cells.items()}
            k.bcur, k.ecur = mv(k.bcur), mv(k.ecur)
        if self.input_start:
            self.input_start = mv(self.input_start)
            if self.input_start[0] < 0:
                self.input_start = None

    # ---- keyboard input ---------------------------------------------------
    def filter_input(self, data, now):
        """Remove the answers to our own cursor-position queries, including
        late ones to queries we gave up on, which the remote side mustn't
        see either.

        Returns (data to forward, bytes to write to the terminal)."""
        self.dsr_sent = [t for t in self.dsr_sent if now - t < self.DSR_FORGET]
        out = b""
        while self.dsr_sent:
            m = CPR_RE.search(data)
            if not m:
                break
            data = data[:m.start()] + data[m.end():]
            self.dsr_sent.pop(0)
            if not self.dsr_sent:           # the answer to the latest query
                if self.dsr_pending:
                    out = self._resync(m, now)
                break
        return data, out

    def _resync(self, m, now):
        """Take the terminal's answer m to our cursor-position query."""
        _t, fed_then = self.dsr_pending
        self.dsr_pending = None
        if fed_then != self.fed:
            # Output arrived in between, so the answer may be stale; ask again.
            self.dsr_tries += 1
            return self._dsr_query(now) if self.dsr_tries < 5 else b""
        sc = self.screen
        y = min(int(m.group(1)) - 1, sc.lines - 1)
        x = min(int(m.group(2)) - 1, sc.columns - 1)
        if not (x == sc.columns - 1 and sc.cursor.x >= sc.columns - 1):
            sc.cursor.x = x
        sc.cursor.y = y
        if not sc.in_alt:
            # After a resize the terminal may have reflowed, or we may have
            # missed output; forget what we thought was to the right of and
            # below the cursor.
            for xx in range(sc.cursor.x, sc.columns):
                sc.buffer[y].pop(xx, None)
            for yy in range(y + 1, sc.lines):
                sc.buffer.pop(yy, None)
            self.block = None
        self.quarantine = False
        self.log("cursor resynced to %d,%d" % (y, x))
        return b""

    def _dsr_query(self, now):
        self.dsr_pending = (now, self.fed)
        self.dsr_sent.append(now)
        return b"\x1b[6n"

    def on_input(self, data, now):
        data, self.paste_tail = self.paste_tail + data, b""
        i, n = 0, len(data)
        while i < n:
            b = data[i]
            if self.in_paste:
                j = data.find(PASTE_END, i)
                if j < 0:
                    self.paste_tail = data[1 - len(PASTE_END):]
                    break
                self.in_paste = False
                i = j + len(PASTE_END)
            elif b == 0x1B:
                m = KEY_SEQ_RE.match(data, i)
                seq = m.group(0) if m else data[i:i + 2]    # lone Esc or Alt+key
                i += len(seq)
                if seq == b"\x1b[200~":
                    self.in_paste = True
                    self._barrier("paste")
                elif seq in self.MOVES:
                    self._key_move(self.MOVES[seq], now)
                elif seq not in (b"\x1b[I", b"\x1b[O"):     # focus in/out
                    self._barrier("key %r" % seq)
            elif b in (0x7F, 0x08):
                self._key_backspace(now)
                i += 1
            elif b < 0x20:
                self._barrier("key %r" % data[i:i + 1])
                i += 1
            else:
                ln = 1 if b < 0x80 else 2 if b < 0xE0 else 3 if b < 0xF0 else 4
                try:
                    ch = data[i:i + ln].decode("utf-8")
                except UnicodeDecodeError:
                    self._barrier("undecodable input")
                else:
                    self._key_char(ch, now)
                i += ln
        return self._render() + self._maybe_query(now)

    def _barrier(self, why):
        """A key whose effect we can't predict: stop until the server catches up."""
        self.log("barrier: %s (input from %s)" % (why, self.input_start))
        self.blocked = True
        self.epoch += 1
        self.input_start = None

    def _next_cursor(self):
        """Where the next keystroke goes: after the pending ones or, if there
        are none, at the cursor.  Starting afresh also settles which cursor
        that is: self.caret is the cell of the one the application draws,
        or None for the terminal's own.  None if the key can't be predicted."""
        if self.blocked:
            return None
        if self.keys:
            return self.keys[-1].ecur
        cur = self._cursor()
        if cur is None:
            self._barrier("cursor hidden; lone reverse-video cells at %s" % self.block[1])
            return None
        sc = self.screen
        self.caret = sc.buffer[cur[0]][cur[1]] if sc.cursor.hidden else None
        return cur

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

    def _narrow(self, row, lo, hi):
        for xx in range(max(lo, 0), min(hi, len(row) - 1) + 1):
            d = row[xx][0]
            if d == "" or (len(d) == 1 and wcwidth(d) > 1):
                return False
        return True

    def _pen_bytes(self):
        """SGR that gives the real terminal the server's pen back."""
        cur = self.screen.cursor
        if cur.pen is None:                     # too much to replay
            return char_style(cur.attrs)
        return b"\x1b[0m" + b"".join(b"\x1b[" + p + b"m" for p in cur.pen)

    def _ink(self, ch):
        """A cell showing `ch` as typed text: in the server's pen or, with a
        drawn cursor, in the cursor's colours."""
        if self.caret is None:
            return (ch, self._pen_bytes())
        return self.caret._replace(data=ch, reverse=False)

    def _key_char(self, ch, now):
        pos = self._next_cursor()
        if pos is None:
            return
        sc = self.screen
        y, x = pos
        if (wcwidth(ch) != 1 or x >= sc.columns - 1
                or self.caret is None and sc.cursor.x >= sc.columns):
            return self._barrier("character at %s" % (pos,))
        row = self._row(y)
        end = self._run_end(row, x)
        if self.caret is not None:
            end = max(end, x)                   # a drawn cursor moves right too
        if end >= sc.columns - 1 or not self._narrow(row, x - 1, end + 1):
            return self._barrier("character at %s, line to %d" % (pos, end))
        cells = {}
        for xx in range(end + 1, x, -1):       # insert: shift the rest right
            cells[(y, xx)] = row[xx - 1]
        cells[(y, x)] = self._ink(ch)
        if self.input_start is None or self.input_start[0] != y:
            self.input_start = (y, x)
        self.keys.append(Key("char", cells, (y, x), (y, x + 1), now, self.epoch))

    def _typed(self, y, x):
        """Is (y, x) inside the text we saw being typed (not the prompt)?"""
        st = self.input_start
        return st is not None and st[0] == y and x >= st[1]

    def _key_backspace(self, now):
        pos = self._next_cursor()
        if pos is None:
            return
        y, x = pos
        if not self._typed(y, x - 1):
            return self._barrier("backspace at %s" % (pos,))
        row = self._row(y)
        end = self._run_end(row, x)
        if self.caret is not None:
            end = max(end, x)                   # a drawn cursor moves left too
        if not self._narrow(row, x - 1, end + 1):
            return self._barrier("backspace at %s, line to %d" % (pos, end))
        cells = {}
        for xx in range(x - 1, max(end, x - 1) + 1):   # delete: shift left
            cells[(y, xx)] = row[xx + 1] if xx + 1 <= end else self._ink(" ")
        self.keys.append(Key("bs", cells, (y, x), (y, x - 1), now, self.epoch))

    def _key_move(self, d, now):
        pos = self._next_cursor()
        if pos is None:
            return
        y, x = pos
        nx = x + d
        row = self._row(y)
        if (not self._typed(y, nx) or nx >= self.screen.columns - 1
                or d > 0 and self._run_end(row, x) < x):   # nothing to move over
            return self._barrier("arrow at %s" % (pos,))
        cells = {}
        if self.caret is not None:              # move the drawn cursor
            cells = {(y, x): row[x]._replace(reverse=False),
                     (y, nx): row[nx]._replace(reverse=True)}
        self.keys.append(Key("mv", cells, (y, x), (y, nx), now, self.epoch))

    # ---- timers and resizes ----------------------------------------------
    def next_deadline(self, now):
        waits = []
        if self.keys:
            waits.append(self.keys[0].t + self.HARD_TIMEOUT - now)
        if self.dsr_pending:
            waits.append(self.dsr_pending[0] + self.DSR_TIMEOUT - now)
        if self.mismatch_since is not None:
            waits.append(self.mismatch_since + self.GRACE - now)
        if self.string_since is not None:
            waits.append(self.string_since + self.STRING_TIMEOUT - now)
        return max(0.0, min(waits)) if waits else None

    def on_tick(self, now):
        out = b""
        if self.string_since is not None and now - self.string_since >= self.STRING_TIMEOUT:
            # Most likely a stray DCS or OSC start.  tmux gives up on it by
            # now, and other terminals ended it at the first ESC, so the
            # shadow screen has missed what came since.
            self.held, self.string_since = b"", None
            out = self._lose_track("string sequence never finished", now)
        changed = False
        if self.mismatch_since is not None and now - self.mismatch_since >= self.GRACE:
            self._reset("server did something else")
            changed = True
        if self.keys and now - self.keys[0].t >= self.HARD_TIMEOUT:
            self._reset("timed out")
            changed = True
        if self.dsr_pending and now - self.dsr_pending[0] >= self.DSR_TIMEOUT:
            self.dsr_pending = None
        return out + (self._render() if changed else b"")

    def on_resize(self, rows, cols, now):
        # The terminal has already resized, so take our drawing off with
        # moves relative to the cursor, and forget it.  Otherwise the cursor
        # position we ask for below would be where we left the real cursor.
        out = self.lift()
        self.shown.clear()
        self.cursor_at = None
        sc = self.screen
        sc.resize(rows, cols)
        sc.cursor.x = min(sc.cursor.x, cols - 1)
        sc.cursor.y = min(sc.cursor.y, rows - 1)
        return out + self._lose_track("window resized", now)

    def _lose_track(self, why, now):
        """The shadow screen may be wrong: draw nothing until we have asked
        the terminal where the cursor is."""
        self.block = None
        self._reset(why)
        self.quarantine = True
        self.dsr_tries = 0
        self.dsr_pending = None
        return self._maybe_query(now)

    def _maybe_query(self, now):
        """Ask where the cursor is, once the terminal is between sequences."""
        if self.quarantine and not (self.dsr_pending or self.held):
            return self._dsr_query(now)
        return b""

    def finish(self):
        """Take our drawing off the screen."""
        return self._render(hide=True)

    # ---- drawing ----------------------------------------------------------
    def _password_prompt(self, cur):
        sc = self.screen
        y = cur[0]
        line = sc.buffer[y]
        text = "".join(line[x].data for x in range(min(cur[1], sc.columns)))
        if y > 0:
            prev = sc.buffer[y - 1]
            text = "".join(prev[x].data for x in range(sc.columns)) + text
        return PASSWORD_RE.search(text) is not None

    def _can_draw(self):
        if not self.showing or self.held or self.quarantine:
            return False
        sc = self.screen
        if mo.DECOM in sc.mode or mo.IRM in sc.mode:
            return False
        if (sc.g1_charset if sc.charset else sc.g0_charset) != cs.LAT1_MAP:
            return False
        # There's no cursor while a frame is half drawn: go by the keystrokes.
        return not self._password_prompt(self._cursor() or self.keys[0].bcur)

    def _redraw(self, pos):
        """(start, bytes, end): bytes that redraw the shadow's cell at pos,
        where they must start (one to the left for the right half of a
        wide char) and where they leave the cursor."""
        y, x = pos
        line = self.screen.buffer[y]
        c = line[x]
        if c.data == "" and x > 0:              # right half of a wide char
            x -= 1
            c = line[x]
        width = 2 if line[x + 1].data == "" else 1
        return ((y, x), char_style(c) + (c.data or " ").encode("utf-8", "replace"),
                (y, x + width))

    def _restore_cell(self, pos):
        start, data, _ = self._redraw(pos)
        return goto(start) + data

    def lift(self):
        """Bytes that take our drawing off the screen and give the real
        terminal back the server's cursor and pen, without changing
        anything here.

        Unlike _render(hide=True), every move is relative to where we left
        the real cursor, so this still works after the terminal has resized
        and scrolled or rewrapped the screen: the text on the cursor's line
        keeps its place relative to the cursor."""
        sc = self.screen
        server = (sc.cursor.y, sc.cursor.x)
        at = self.cursor_at or server
        if not self.shown and at == server:
            return b""
        out = bytearray()
        for pos in sorted(self.shown):
            start, data, end = self._redraw(pos)
            out += move(at, start, sc.columns) + data
            at = end
        if server[1] >= sc.columns:
            # Pending autowrap: rewrite the last cell, as _render does.
            start, data, _ = self._redraw((server[0], sc.columns - 1))
            out += move(at, start, sc.columns) + data
        else:
            out += move(at, server, sc.columns)
        return bytes(out) + self._pen_bytes()

    def _render(self, hide=False):
        sc = self.screen
        desired, dcur = {}, None
        # _can_draw isn't free, so only ask once a trusted key could be drawn.
        if (not hide and self.keys and self.keys[0].epoch <= self.confirmed_epoch
                and self._can_draw()):
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
        for pos, want in desired.items():
            if self.shown.get(pos) == want:
                continue
            c = sc.buffer[pos[0]][pos[1]]
            if isinstance(want, Char):
                text, style, same = want.data, char_style(want), c == want
            else:
                (text, style), same = want, c.data == want[0]
            if pos not in self.shown and text.strip() and same:
                continue                         # already looks right
            out += goto(pos) + style + mark + text.encode()
            self.shown[pos] = want
        shadow_cur = (sc.cursor.y, sc.cursor.x)
        # A hidden cursor stays put: the application draws its own.
        target = shadow_cur if sc.cursor.hidden else dcur or shadow_cur
        if not out and (self.cursor_at or shadow_cur) == target:
            return b""
        if target[1] >= sc.columns:
            # Pending autowrap: rewriting the last cell is the only way to
            # put the terminal back in that state.
            out += self._restore_cell((target[0], sc.columns - 1)) + self._pen_bytes()
        elif out:
            out += goto(target) + self._pen_bytes()
        else:
            out += goto(target)
        self.cursor_at = None if target == shadow_cur else target
        return bytes(out)


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------

def get_size(fd):
    try:
        cols, rows = os.get_terminal_size(fd)
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


class Guarded:
    """A Predictor that can't break the session: the first exception it
    raises is logged, its drawing is taken off the screen, and from then on
    everything passes straight through.  With no predictor at all
    (--predict=never) everything passes through anyway."""

    def __init__(self, pred, log=None):
        self.pred, self.log = pred, log
        self.leftover = b""

    def _call(self, name, *args):
        """pred.name(*args), or None if there is no predictor (any more)."""
        if self.pred is None:
            return None
        # What would clean up after the predictor, worked out before the
        # call: a failing call may have changed its state half way.
        lift = b""
        try:
            lift = self.pred.lift()
            return getattr(self.pred, name)(*args)
        except Exception:
            if self.log:
                self.log.write(traceback.format_exc())
                self.log.flush()
            self.pred, self.leftover = None, lift
            return None

    def _leftover(self):
        out, self.leftover = self.leftover, b""
        return out

    def next_deadline(self, now):
        return self._call("next_deadline", now)

    def on_output(self, data, now):
        out = self._call("on_output", data, now)
        return self._leftover() + data if out is None else out

    def filter_input(self, data, now):
        r = self._call("filter_input", data, now)
        return (data, self._leftover()) if r is None else r

    def on_input(self, data, now):
        return self._call("on_input", data, now) or self._leftover()

    def on_tick(self, now):
        return self._call("on_tick", now) or self._leftover()

    def on_resize(self, rows, cols, now):
        return self._call("on_resize", rows, cols, now) or self._leftover()

    def finish(self):
        return self._call("finish") or self._leftover()


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
    pred = Guarded(None)
    tty.setraw(0)
    try:
        child, master = pty.fork()
        if child == 0:
            try:
                os.execvp(cmd[0], cmd)
            except OSError as e:
                os.write(2, ("localecho: %s: %s\r\n" % (cmd[0], e.strerror)).encode())
            os._exit(127)
        set_size(master, rows, cols)

        leftover = b""
        if opts["predict"] != "never":
            # The command starts up meanwhile; its output waits in the pty.
            pos, leftover = query_cursor()
            p = Predictor(rows, cols, mode=opts["predict"], mark=opts["mark"], log=log)
            if pos is None:
                write_all(1, b"\x1b[H\x1b[2J")
                pos = (0, 0)
                p.dsr_sent.append(time.monotonic())     # the answer may come yet
            p.screen.cursor.y = min(pos[0], rows - 1)
            p.screen.cursor.x = min(pos[1], cols - 1)
            pred = Guarded(p, log)

        # Input the command hasn't taken yet.  Writing it must never block:
        # a command can't read more input while it is stuck writing output
        # that we have stopped reading (a big paste into anything chatty).
        to_child = bytearray(leftover)
        os.set_blocking(master, False)

        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_w, False)
        signal.set_wakeup_fd(wake_w)
        resized = [False]
        signal.signal(signal.SIGWINCH, lambda s, f: resized.__setitem__(0, True))

        while True:
            now = time.monotonic()
            # While the command is behind with its input, leave the rest of
            # a paste with the terminal, as it would be without us.
            readers = [master, wake_r] + ([0] if len(to_child) < 65536 else [])
            ready, _, _ = select.select(readers, [master] if to_child else [], [],
                                        pred.next_deadline(now))
            now = time.monotonic()
            if wake_r in ready:
                os.read(wake_r, 512)
            if resized[0]:
                resized[0] = False
                rows, cols = get_size(1)
                set_size(master, rows, cols)
                write_all(1, pred.on_resize(rows, cols, now))
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError as e:
                    if e.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    break
                write_all(1, pred.on_output(data, now))
            if 0 in ready:
                data = os.read(0, 4096)
                if not data:
                    break
                data, extra = pred.filter_input(data, now)
                write_all(1, extra)
                if data:
                    to_child += data
                    write_all(1, pred.on_input(data, now))
            if to_child:
                try:
                    del to_child[:os.write(master, to_child)]
                except BlockingIOError:
                    pass
            write_all(1, pred.on_tick(now))
    finally:
        try:
            write_all(1, pred.finish())
        except OSError:
            pass                        # the terminal may be gone
        try:
            termios.tcsetattr(0, termios.TCSAFLUSH, saved)
        except termios.error:
            pass                        # likewise
        if child:
            # If we are leaving first (a signal, or the terminal went away),
            # this hangs up on the command so that it exits too.
            os.close(master)
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
