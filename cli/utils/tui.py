"""
Minimal arrow-key select menu, no dependency beyond the standard library.
"""

import os
import select as select_syscall
import sys
import termios
import tty

from canyonos.theme import GREEN

UP_KEYS = ("\x1b[A", "\x1bOA", "k")
DOWN_KEYS = ("\x1b[B", "\x1bOB", "j")
CANCEL_KEYS = ("\x03", "\x1b")
DELETE_KEYS = ("d", "D")
QUIT_KEYS = ("q", "Q")

# The brand green as a raw truecolor escape: this menu writes ANSI directly
# rather than going through rich, but shares the CLI's one palette.
_GREEN = "\x1b[38;2;{};{};{}m".format(*(int(GREEN[i:i + 2], 16) for i in (1, 3, 5)))

# Sentinel returned (paired with the hovered value) when the delete key is
# pressed and `deletable=True`. Callers check `result[0] is DELETE_ACTION`.
DELETE_ACTION = object()

# Sentinel returned when the quit key is pressed and `quittable=True`. Distinct
# from None (which callers use for a single-level cancel/back) so a caller can
# unwind an entire nested session. Callers check `result is QUIT_ACTION`.
QUIT_ACTION = object()


def _read_key(fd):
    # Reads straight off the fd (not sys.stdin) so this stays in sync with
    # the select() call below -- stdin's own buffering can silently swallow
    # an arrow key's trailing bytes before select() ever sees them queued.
    ch = os.read(fd, 1).decode()
    if ch == "\x1b":
        # An arrow key arrives as a multi-byte escape sequence; a bare Esc
        # press has nothing queued right behind it.
        if select_syscall.select([fd], [], [], 0.01)[0]:
            ch += os.read(fd, 1).decode()
            if ch[-1] in ("[", "O"):
                ch += os.read(fd, 1).decode()
    return ch


def select_menu(options, title, deletable=False, quittable=False):
    """Arrow-key single-select over `options` (a list of (value, label) pairs).

    Returns the chosen value, or None if there's nothing to choose from or
    the user cancelled (Esc/Ctrl-C).

    If `deletable` is True, pressing the delete key ('d') over an item returns
    the tuple `(DELETE_ACTION, hovered_value)` so the caller can act on the
    currently-hovered item instead of selecting it.

    If `quittable` is True, pressing the quit key ('q') returns the sentinel
    `QUIT_ACTION` -- distinct from None -- so the caller can unwind an entire
    nested session rather than just this one menu.
    """
    if not options or not sys.stdin.isatty():
        return None

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    out = sys.stderr
    idx = 0
    n = len(options)

    def frame():
        lines = [f"\x1b[1m{title}\x1b[0m", ""]
        for i, (_, label) in enumerate(options):
            lines.append(f"{_GREEN}❯ {label}\x1b[0m" if i == idx else f"  {label}")
        hint = "↑/↓ move · 1-9 jump · enter select"
        if deletable:
            hint += " · d delete"
        if quittable:
            hint += " · q quit"
        hint += " · esc cancel"
        lines.append(f"\x1b[2m{hint}\x1b[0m")
        return "\r\n".join(lines)

    prev_frame = None
    try:
        tty.setraw(fd)
        out.write("\x1b[?25l")
        while True:
            text = frame()
            if prev_frame is not None:
                # How far back up to move is read off the frame we actually
                # wrote last time, not recomputed separately -- it can't drift
                # out of sync with what's really on screen.
                out.write(f"\r\x1b[{prev_frame.count(chr(10))}A\x1b[J")
            out.write(text)
            out.flush()
            prev_frame = text

            key = _read_key(fd)
            if key in ("\r", "\n"):
                return options[idx][0]
            if key in CANCEL_KEYS:
                return None
            if key in UP_KEYS:
                idx = (idx - 1) % n
            elif key in DOWN_KEYS:
                idx = (idx + 1) % n
            elif deletable and key in DELETE_KEYS:
                return (DELETE_ACTION, options[idx][0])
            elif quittable and key in QUIT_KEYS:
                return QUIT_ACTION
            elif key.isdigit() and key != "0" and int(key) <= n:
                return options[int(key) - 1][0]
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        out.write("\x1b[?25h\r\n")
        out.flush()
