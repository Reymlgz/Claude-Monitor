"""Startup ASCII animation. Any key skips it."""

import math
import select
import shutil
import sys
import time

LOGO = [
    r"  ____ _                 _       __  __             _ _             ",
    r" / ___| | __ _ _   _  __| | ___ |  \/  | ___  _ __ (_) |_ ___  _ __ ",
    r"| |   | |/ _` | | | |/ _` |/ _ \| |\/| |/ _ \| '_ \| | __/ _ \| '__|",
    r"| |___| | (_| | |_| | (_| |  __/| |  | | (_) | | | | | || (_) | |   ",
    r" \____|_|\__,_|\__,_|\__,_|\___||_|  |_|\___/|_| |_|_|\__\___/|_|   ",
]
ORANGE = "\x1b[38;5;208m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def _skip_pressed() -> bool:
    return bool(select.select([sys.stdin], [], [], 0)[0])


def _pulse(x: int, t: float, width: int) -> int:
    """Row offset (-2..2) of a heartbeat line at column x."""
    head = (t * 60) % (width + 20)
    d = head - x
    if 0 <= d < 14:
        return round([0, 0, 0, -1, -2, 1, 2, -1, 0, 0, 1, 0, 0, 0][int(d)])
    return 0


def play() -> None:
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        return
    cols, rows = shutil.get_terminal_size()
    width = min(cols, 72)
    pad = " " * max(0, (cols - width) // 2)
    top = max(0, (rows - (len(LOGO) + 9)) // 2)
    out = sys.stdout
    out.write("\x1b[2J")

    total, fps = 2.4, 30
    frames = int(total * fps)
    for f in range(frames):
        if _skip_pressed():
            break
        t = f / fps
        buf = ["\x1b[H" + "\n" * top]
        # logo reveals left-to-right
        reveal = int(width * min(1.0, t / 1.2))
        for line in LOGO:
            buf.append(f"{pad}{ORANGE}{line[:reveal]}{RESET}\x1b[K\n")
        buf.append("\n")
        # heartbeat line (5 rows tall)
        grid = [[" "] * width for _ in range(5)]
        for x in range(width):
            off = _pulse(x, t, width)
            grid[2 + off][x] = "*" if off else "-"
        for r in grid:
            buf.append(f"{pad}{ORANGE}{''.join(r)}{RESET}\x1b[K\n")
        dots = "." * (1 + int(t * 4) % 3)
        buf.append(f"\n{pad}{DIM}scanning for Claude processes{dots}{RESET}\x1b[K\n")
        out.write("".join(buf))
        out.flush()
        time.sleep(1 / fps)
    out.write("\x1b[2J\x1b[H")
    out.flush()
