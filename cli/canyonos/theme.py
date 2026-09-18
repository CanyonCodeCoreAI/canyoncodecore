"""
CanyonOS standard color palette.

The green->white gradient introduced by the `canyonos init` banner, reused
across the CLI so everything shares one look. `GREEN` is the primary brand
color; `WHITE` the secondary; `GRADIENT` the full ramp for multi-line output.
Both flip to a dark-on-light variant when the terminal background is light.
"""

import os
import re
import select
import sys
import termios
import tty

GREEN = "#2BD17E"

_GRADIENT_DARK = ["#2BD17E", "#55DA98", "#80E3B2", "#AAEDCB", "#D5F6E5", "#FFFFFF"]
_GRADIENT_LIGHT = ["#2BD17E", "#1F9C61", "#177249", "#0F4E31", "#082A1A", "#000000"]


def _is_light_background():
    if not sys.stdin.isatty():
        return False
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b]11;?\x07")
        sys.stdout.flush()
        # 100ms is fine locally but can be exceeded by a real SSH round-trip;
        # 400ms gives a laggy remote session a real chance to answer before
        # we give up and assume dark.
        reply = (
            os.read(fd, 32).decode(errors="ignore")
            if select.select([fd], [], [], 0.4)[0]
            else ""
        )
    finally:
        # A reply that arrives just after our timeout (or a second stray one)
        # would otherwise sit in the tty buffer and get echoed as literal text
        # ahead of the next command once we restore cooked/echo mode below --
        # drain anything still pending, non-blockingly, before that happens.
        while select.select([fd], [], [], 0)[0]:
            if not os.read(fd, 1024):
                break
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    m = re.search(r"rgb:([0-9a-f]{2})\S*/([0-9a-f]{2})\S*/([0-9a-f]{2})", reply, re.I)
    return (
        bool(m)
        and (0.299 * int(m[1], 16) + 0.587 * int(m[2], 16) + 0.114 * int(m[3], 16))
        > 128
    )


# Primary -> secondary ramp (used for the init banner, top to bottom).
GRADIENT = _GRADIENT_LIGHT if _is_light_background() else _GRADIENT_DARK
WHITE = GRADIENT[-1]
