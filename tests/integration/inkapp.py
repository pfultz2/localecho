#!/usr/bin/env python3
"""inkapp.py - a one-line editor that draws like an Ink app (Claude Code):
the terminal's cursor is hidden, the caret is a reverse-video cell, and
every change erases and rewrites the whole frame, leaving the real cursor
below it.  Enter prints what was typed and exits."""
import os
import sys
import tty

ERASE = "\x1b[2K\x1b[1A\x1b[2K\x1b[1A\x1b[2K\x1b[G"    # Ink's eraseLines(3)


def frame(text, at):
    under = text[at] if at < len(text) else " "
    return ("> %s\x1b[7m%s\x1b[27m%s\r\n%s\r\n"
            % (text[:at], under, text[at + 1:], "─" * 40))


def main():
    tty.setraw(0)
    text, at = "", 0
    out = "\x1b[?25l" + frame(text, at)
    while True:
        os.write(1, out.encode())
        data = os.read(0, 1024).decode("utf-8", "replace")
        i = 0
        while i < len(data):
            if data.startswith("\x1b[D", i):
                at, i = max(at - 1, 0), i + 3
            elif data.startswith("\x1b[C", i):
                at, i = min(at + 1, len(text)), i + 3
            elif data[i] == "\x7f":
                if at:
                    text, at = text[:at - 1] + text[at:], at - 1
                i += 1
            elif data[i] in "\r\x03":
                os.write(1, (ERASE + "got:%s\r\n\x1b[?25h" % text).encode())
                return
            else:
                if data[i] >= " ":
                    text, at = text[:at] + data[i] + text[at:], at + 1
                i += 1
        out = ERASE + frame(text, at)


if __name__ == "__main__":
    sys.exit(main())
