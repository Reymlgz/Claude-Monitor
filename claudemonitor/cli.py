from __future__ import annotations

import argparse
import atexit
import os
import re
import select
import shutil
import signal
import sys
import termios
import threading
import time
import tty

from . import __version__, anim
from .procs import Proc, scan
from .usage import Report, Totals, UsageReader

ORANGE = "\x1b[38;5;208m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
GREEN = "\x1b[32m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
REV = "\x1b[7m"
RESET = "\x1b[0m"


KIND_COLOR = {
    "Automated browser": "\x1b[38;5;203m",  # red-ish: the usual culprit
    "Claude Code": ORANGE,
    "MCP server": "\x1b[38;5;75m",
    "Chrome bridge": "\x1b[38;5;141m",
    "Claude app": "\x1b[38;5;150m",
}


def heat(value: float, warn: float, bad: float) -> str:
    return RED if value >= bad else YELLOW if value >= warn else GREEN


def fmt_mem(kb: int) -> str:
    mb = kb / 1024
    return f"{mb / 1024:.1f}G" if mb >= 1024 else f"{mb:.0f}M"


def fmt_tokens(n: float) -> str:
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n:.0f}"


def fmt_cost(t: Totals) -> str:
    return f"${t.cost:,.2f}" + ("+" if t.unpriced else "")


def fmt_left(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def bar(value: float, full: float, width: int = 10) -> str:
    n = min(width, round(value / full * width)) if value > 0.5 else 0
    return "\u2588" * n + "\u2591" * (width - n)


def short_name(cmd: str) -> str:
    """A readable label instead of a 200-char command line."""
    apps = re.findall(r"/([^/]+)\.app/Contents/MacOS/([^/ ]+(?: [^/ -]+)*)", cmd)
    if apps:
        return apps[-1][1].split(" --")[0]
    parts = cmd.split()
    exe = os.path.basename(parts[0]) if parts else cmd
    if exe in ("node", "npm", "npx", "uv", "uvx", "python", "python3") and len(parts) > 1:
        args = [a for a in parts[1:] if not a.startswith("-")]
        if args:
            return f"{exe} {os.path.basename(args[0])}"
    return exe


def verdict(procs: list[Proc]) -> str:
    cpu = sum(p.cpu for p in procs)
    mem = sum(p.mem_pct for p in procs)
    if not procs:
        return f"{GREEN}\u25cf All quiet. Nothing Claude-related is running.{RESET}"
    top = max(procs, key=lambda p: p.cpu)
    if cpu >= 100 or mem >= 15:
        head = f"{RED}{BOLD}\u25b2 HEAVY: Claude is likely heating your Mac and draining the battery.{RESET}"
    elif cpu >= 30 or mem >= 8:
        head = f"{YELLOW}{BOLD}\u25b2 Busy: Claude is using a noticeable amount of resources.{RESET}"
    else:
        head = f"{GREEN}{BOLD}\u25cf Calm: Claude is barely using any resources.{RESET}"
    if top.cpu >= 10:
        head += f"  {DIM}Biggest: {short_name(top.command)} (PID {top.pid}, {top.cpu:.0f}% CPU){RESET}"
    return head


def group_lines(procs: list[Proc]) -> list[str]:
    groups: dict[str, list[Proc]] = {}
    for p in procs:
        groups.setdefault(p.kind, []).append(p)
    lines = []
    for kind, ps in sorted(groups.items(), key=lambda kv: -sum(p.cpu for p in kv[1])):
        cpu, kb = sum(p.cpu for p in ps), sum(p.rss_kb for p in ps)
        color = KIND_COLOR.get(kind, "")
        lines.append(
            f"  {color}{kind:<17}{RESET} {len(ps):>3} proc  "
            f"CPU {heat(cpu, 30, 80)}{bar(cpu, 100)} {cpu:>5.1f}%{RESET}   "
            f"MEM {fmt_mem(kb):>6}"
        )
    return lines


def build_rows(procs: list[Proc], expanded: bool, rank: dict[int, int] | None = None) -> list[Proc]:
    """Collapse all Claude app processes into one row; optionally list them under it."""
    app = [p for p in procs if p.kind == "Claude app"]
    rows = [p for p in procs if p.kind != "Claude app"]
    children: list[Proc] = []
    if app:
        ids = {p.pid for p in app}
        root = next((p for p in app if p.ppid not in ids), app[0])
        arrow, hint = ("\u25be", "Enter to collapse") if expanded else ("\u25b8", "Enter to expand")
        group = Proc(root.pid, root.ppid, sum(p.cpu for p in app), sum(p.rss_kb for p in app),
                     sum(p.mem_pct for p in app), root.uptime, root.command, "Claude app",
                     any(p.protected for p in app))
        group.members = app
        group.label = f"Claude app {arrow} {len(app)} processes ({hint})"
        rows.append(group)
        if expanded:
            children = sorted((p for p in app if p is not root), key=lambda p: -p.cpu)
            for c in children:
                c.is_child = True
                c.label = "  \u2514 " + short_name(c.command)
    if rank is not None:
        rows.sort(key=lambda p: rank.get(p.pid, len(rank)))
    else:
        rows.sort(key=lambda p: (p.cpu, p.rss_kb), reverse=True)
    if children:
        i = next(i for i, p in enumerate(rows) if p.members)
        rows[i + 1:i + 1] = children
    return rows


def row_text(p: Proc, width: int, selected: bool) -> str:
    name = p.label or short_name(p.command)
    name_w = max(12, width - 62)
    name = name if len(name) <= name_w else name[: name_w - 1] + "\u2026"
    idle = p.cpu < 1 and p.mem_pct < 0.5
    color = KIND_COLOR.get(p.kind, "")
    base = REV if selected else (DIM if idle else "")
    mark = "*" if p.protected else " "
    if selected:
        plain = (f"{p.pid:>7}{mark} {p.kind:<17} {bar(p.cpu, 100, 8)} {p.cpu:>5.1f}% "
                 f"{fmt_mem(p.rss_kb):>6} {p.uptime:>11}  {name}")
        return f"{BOLD}{REV}\u25b6{plain}{' ' * max(0, width - len(plain) - 1)}{RESET}"
    line = (
        f"{base} {p.pid:>7}{mark}{RESET}{base} {color}{p.kind:<17}{RESET}{base} "
        f"{'' if idle else heat(p.cpu, 30, 80)}{bar(p.cpu, 100, 8)} {p.cpu:>5.1f}%{RESET}{base} "
        f"{'' if idle else heat(p.mem_pct, 3, 8)}{fmt_mem(p.rss_kb):>6}{RESET}{base} "
        f"{p.uptime:>11}  {name}{RESET}"
    )
    return line


FULL, EMPTY = "\u2588", "\u2591"
SPARK = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
SHARE_COLORS = [ORANGE, "\x1b[38;5;75m", "\x1b[38;5;141m", "\x1b[38;5;150m"]  # last one is "everything else"


def spark(values: list[float]) -> str:
    top = max(values, default=0) or 1
    return "".join("\u00b7" if v <= 0 else SPARK[min(7, int(v / top * 7.999))] for v in values)


def clip(line: str, width: int) -> str:
    """Cut a colored line to `width` visible characters without breaking escape codes."""
    toks = re.findall(r"\x1b\[[0-9;]*m|.", line, re.S)
    if sum(not t.startswith("\x1b") for t in toks) <= width:
        return line
    out, seen = [], 0
    for tok in toks:
        if not tok.startswith("\x1b"):
            if seen == width - 1:
                break
            seen += 1
        out.append(tok)
    return "".join(out) + "\u2026" + RESET


def pct_bar(label: str, pct: float, text: str, warn: float = 60, bad: float = 90) -> str:
    color = heat(pct, warn, bad)
    return f"  {label:<15}{color}{bar(pct, 100, 20)} {pct:>4.0f}%{RESET}  {text}"


def share_bar(label: str, groups: dict[str, Totals], width: int = 20) -> str:
    """One bar split by share of tokens, then a legend: biggest three, the rest lumped together."""
    items = sorted(groups.items(), key=lambda kv: -kv[1].tokens)
    total = sum(t.tokens for _, t in items) or 1
    parts = items[:3]
    rest = items[3:]
    if rest:
        other = Totals()
        for _, t in rest:
            other.tokens += t.tokens
            other.cost += t.cost
        parts.append((f"{len(rest)} more", other))
    cells, legend, used = [], [], 0
    for i, (name, t) in enumerate(parts):
        n = width - used if i == len(parts) - 1 else round(t.tokens / total * width)
        n = min(n, width - used)
        used += n
        color = SHARE_COLORS[min(i, len(SHARE_COLORS) - 1)]
        cells.append(f"{color}{FULL * n}{RESET}")
        name = name.removeprefix("claude-")
        name = name if len(name) <= 18 else name[:17] + "\u2026"
        legend.append(f"{color}\u25a0{RESET} {name} {BOLD}{t.tokens / total * 100:.0f}%{RESET} "
                      f"{DIM}{fmt_tokens(t.tokens)} ~{fmt_cost(t)}{RESET}")
    return f"  {label:<15}{''.join(cells)}{EMPTY * (width - used)}        {'  '.join(legend)}"


def usage_lines(rep: Report | None, full: bool = True) -> list[str]:
    """Token usage from Claude Code's transcripts; `full=False` keeps it to three lines."""
    head = f"{ORANGE}{BOLD}Usage{RESET} {DIM}(Claude Code, from local transcripts · $ = API-price equivalent){RESET}"
    if rep is None:
        return [f"{ORANGE}{BOLD}Usage{RESET}  {DIM}reading Claude Code transcripts…{RESET}"]
    if not rep.found:
        return [f"{ORANGE}{BOLD}Usage{RESET}  {DIM}no Claude Code transcripts found in ~/.claude/projects{RESET}"]
    lines = [head]
    if rep.window_start is not None:
        end = rep.window_start + 5 * 3600
        pct = rep.window.tokens / rep.peak_window * 100 if rep.peak_window else 0
        lines.append(pct_bar("5h window", pct,
                             f"{BOLD}{fmt_tokens(rep.window.tokens)}{RESET} tok ~{fmt_cost(rep.window)}  "
                             f"{DIM}of your busiest 5h window ({fmt_tokens(rep.peak_window)}) · "
                             f"{fmt_tokens(rep.burn_per_min)} tok/min{RESET}"))
        if full:
            gone = time.time() - rep.window_start
            lines.append(pct_bar("window time", gone / (5 * 3600) * 100,
                                 f"{DIM}{fmt_left(gone)} of 5h · resets "
                                 f"{time.strftime('%H:%M', time.localtime(end))} (in {fmt_left(end - time.time())}){RESET}",
                                 warn=101, bad=101))
    else:
        lines.append(f"  {'5h window':<15}{DIM}{bar(0, 100, 20)}    -   nothing sent in the last 5 hours{RESET}")
    day, peak = rep.peak_day
    pct = rep.today.tokens / peak * 100 if peak else 0
    when = time.strftime("%a %d", time.strptime(day, "%Y-%m-%d")) if day else "-"
    lines.append(pct_bar("today", pct,
                         f"{BOLD}{fmt_tokens(rep.today.tokens)}{RESET} tok ~{fmt_cost(rep.today)}  "
                         f"{DIM}of your busiest day ({fmt_tokens(peak)}, {when}){RESET}", warn=101, bad=101))
    if not full:
        return lines
    if rep.by_model:
        lines.append(share_bar("models today", rep.by_model))
    if rep.by_project:
        lines.append(share_bar("projects today", rep.by_project))
    lines.append(f"  {'last 14 days':<15}{ORANGE}{spark([t.tokens for _, t in rep.daily])}{RESET}"
                 f"{' ' * 13}7 days {BOLD}{fmt_tokens(rep.week.tokens)}{RESET} ~{fmt_cost(rep.week)}   "
                 f"30 days {BOLD}{fmt_tokens(rep.month.tokens)}{RESET} ~{fmt_cost(rep.month)}")
    return lines


def render(rows: list[Proc], procs: list[Proc], sel: int, status: str, busy: bool,
           rep: Report | None = None) -> str:
    cols, term_rows = shutil.get_terminal_size()
    out = ["\x1b[H"]
    out.append(f"{ORANGE}{BOLD}Claude Monitor{RESET}"
               f"{DIM}{'  refreshing…' if busy else ''}{RESET}\x1b[K\n")
    out.append(clip(verdict(procs), cols) + "\x1b[K\n")
    usage = usage_lines(rep, full=term_rows >= 32)  # short terminals keep room for processes
    for line in usage:
        out.append(clip(line, cols) + "\x1b[K\n")
    out.append("\x1b[K\n")
    groups = group_lines(procs)
    for g in groups:
        out.append(clip(g, cols) + "\x1b[K\n")
    out.append("\x1b[K\n")
    out.append(f"{BOLD}{'PID':>8} {'TYPE':<17} {'CPU':<15} {'MEM':>6} {'RUNNING':>11}  WHAT{RESET}\x1b[K\n")
    visible = max(1, term_rows - 7 - len(usage) - 1 - len(groups) - 2)
    start = min(max(0, sel - visible + 1), max(0, len(rows) - visible))
    for i, p in enumerate(rows[start:start + visible], start):
        out.append(clip(row_text(p, cols, i == sel), cols) + "\x1b[K\n")
    if len(rows) > visible:
        out.append(f"{DIM}  … {len(rows) - visible} more (scroll with ↑/↓){RESET}\x1b[K\n")
    out.append("\x1b[J")
    out.append(f"\x1b[{term_rows - 1};1H{clip(status, cols)}\x1b[K")
    out.append(f"\x1b[{term_rows};1H" + clip(f"{DIM}\u2191/\u2193 select   {RESET}\u23ce{DIM} expand   {RESET}x{DIM} kill   {RESET}f{DIM} force-kill   {RESET}A{DIM} kill ALL   {RESET}r{DIM} refresh   {RESET}q{DIM} quit    * = this session{RESET}", cols) + "\x1b[K")
    return "".join(out)


def kill_pids(pids: list[int], sig: int) -> int:
    n = 0
    for pid in pids:
        try:
            os.kill(pid, sig)
            n += 1
        except (ProcessLookupError, PermissionError):
            pass
    return n


def kill_all(procs: list[Proc]) -> int:
    pids = [p.pid for p in procs if not p.protected]
    n = kill_pids(pids, signal.SIGTERM)
    time.sleep(1)
    kill_pids(pids, signal.SIGKILL)  # stragglers; dead pids are ignored
    return n


class Sampler(threading.Thread):
    """Scans in the background so the UI never blocks on `top`."""

    def __init__(self):
        super().__init__(daemon=True)
        self.procs: list[Proc] = []
        self.busy = True
        self.version = 0
        self._wake = threading.Event()

    def run(self):
        while True:
            self.busy = True
            result = scan()
            self.procs, self.busy = result, False
            self.version += 1
            self._wake.wait(3)
            self._wake.clear()

    def refresh(self):
        self._wake.set()


class UsageSampler(threading.Thread):
    """Re-reads the transcripts every few seconds; only new lines are parsed."""

    def __init__(self):
        super().__init__(daemon=True)
        self.report: Report | None = None
        self.version = 0
        self._reader = UsageReader()
        self._wake = threading.Event()

    def run(self):
        while True:
            try:
                self.report = self._reader.report()
            except Exception:  # a usage glitch must never take down the process monitor
                pass
            self.version += 1
            self._wake.wait(10)
            self._wake.clear()

    def refresh(self):
        self._wake.set()


def interactive() -> None:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    def restore():
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?7h\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    anim.play()
    sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J")  # ?7l: no line wrap, so nothing can scroll
    tty.setcbreak(fd)
    atexit.register(restore)

    sampler = Sampler()
    sampler.start()
    usage = UsageSampler()
    usage.start()
    useen = -1
    sel, status, seen, confirm = 0, "", -1, None
    last_key, order = 0.0, []
    typed = None  # text typed so far while confirming "kill all"; None when not asking
    n_kill = 0
    procs: list[Proc] = []  # flat list of every process
    rows: list[Proc] = []  # what's on screen (Claude app collapsed into one row)
    expanded, rebuild = False, False
    dirty = True
    try:
        while True:
            if sampler.version != seen or rebuild:
                keep = rows[sel].pid if rows and sel < len(rows) else None
                if sampler.version != seen:
                    seen = sampler.version
                    procs = sampler.procs
                navigating = time.monotonic() - last_key < 8 and order
                # while navigating keep rows where they are, only update the numbers
                rows = build_rows(procs, expanded, {pid: i for i, pid in enumerate(order)} if navigating else None)
                order = [r.pid for r in rows if not r.is_child]
                sel = next((i for i, r in enumerate(rows) if r.pid == keep), min(sel, max(0, len(rows) - 1)))
                rebuild, dirty = False, True
            if usage.version != useen:
                useen, dirty = usage.version, True
            if dirty:
                sys.stdout.write(render(rows, procs, sel, status, sampler.busy, usage.report))
                sys.stdout.flush()
                dirty = False
            if not select.select([sys.stdin], [], [], 0.25)[0]:
                continue
            data = os.read(fd, 64).decode(errors="ignore")
            dirty = True
            last_key = time.monotonic()
            for key in re.findall(r"\x1b\[[0-9;]*[A-Za-z~]|\x1bO[A-Z]|.", data, re.S):
                key = {"\x1bOA": "\x1b[A", "\x1bOB": "\x1b[B"}.get(key, key)
                if typed is not None:
                    if key in ("\r", "\n"):
                        if typed.strip().lower() == "yes":
                            status = f"{GREEN}Killed {kill_all(procs)} processes.{RESET}"
                            sampler.refresh()
                        else:
                            status = f"Cancelled: you typed {typed!r}, not 'yes'. Nothing was killed."
                        typed = None
                    elif key == "\x1b" or key == "\x03":
                        typed, status = None, "Cancelled. Nothing was killed."
                    elif key in ("\x7f", "\b"):
                        typed = typed[:-1]
                    elif key.isprintable():
                        typed += key
                    if typed is not None:
                        status = kill_all_prompt(n_kill, typed)
                    continue
                if confirm:
                    if key.lower() == "y":
                        status = confirm()
                        sampler.refresh()
                    else:
                        status = "Cancelled."
                    confirm = None
                    continue
                target = rows[sel] if rows else None
                if key in ("q", "\x03"):
                    return
                elif key in ("\x1b[A", "k"):
                    sel = max(0, sel - 1)
                elif key in ("\x1b[B", "j"):
                    sel = min(len(rows) - 1, sel + 1) if rows else 0
                elif key in ("\r", "\n", "\x1b[C", "\x1b[D") and target and target.members:
                    expanded = key != "\x1b[D" and (not expanded or key == "\x1b[C")
                    rebuild = True
                elif key == "r":
                    status = "Refreshing…"
                    sampler.refresh()
                    usage.refresh()
                elif key in ("x", "f") and target:
                    sig = signal.SIGTERM if key == "x" else signal.SIGKILL
                    if target.protected:
                        status = f"{YELLOW}PID {target.pid} is this session (or its parent). Press y to kill it anyway.{RESET}"
                        confirm = lambda t=target, s=sig: _kill_one(t, s)
                    elif target.is_child:
                        status = (f"{YELLOW}Killing one Claude app helper can crash or blank the app. "
                                  f"Press y to kill it anyway, or select the Claude app row to quit it cleanly.{RESET}")
                        confirm = lambda t=target, s=sig: _kill_one(t, s)
                    else:
                        status = _kill_one(target, sig)
                        sampler.refresh()
                elif key == "A" and procs:
                    killable = [p for p in procs if not p.protected]
                    n_kill, typed = len(killable), ""
                    status = kill_all_prompt(n_kill, typed)
    finally:
        restore()
        atexit.unregister(restore)


def kill_all_prompt(n: int, typed: str) -> str:
    return (f"{RED}{BOLD}Kill ALL {n} processes?{RESET} Type {BOLD}yes{RESET} and press Enter "
            f"(Esc cancels): {typed}\u2588")


def _kill_one(p: Proc, sig: int) -> str:
    pids = [m.pid for m in p.members] or [p.pid]
    n = kill_pids(pids, sig)
    name = "SIGKILL" if sig == signal.SIGKILL else "SIGTERM"
    what = f"Claude app ({n} processes)" if p.members else f"PID {p.pid}"
    return f"{GREEN}Sent {name} to {what}.{RESET}" if n else f"{RED}Could not signal {what}.{RESET}"


def print_once() -> None:
    procs = scan()
    if not procs:
        print("Nothing Claude-related is running.")
    else:
        print(f"{'PID':>7}  {'TYPE':<17} {'CPU%':>6} {'MEM%':>5} {'RSS':>6}  COMMAND")
        for p in procs:
            print(f"{p.pid:>7}  {p.kind:<17} {p.cpu:>6.1f} {p.mem_pct:>5.1f} {fmt_mem(p.rss_kb):>6}  {p.command[:80]}")
        print(f"\n{len(procs)} processes, CPU {sum(p.cpu for p in procs):.1f}%, "
              f"memory {sum(p.mem_pct for p in procs):.1f}%")
    print()
    for line in usage_lines(UsageReader().report()):
        print(re.sub(r"\x1b\[[0-9;]*m", "", line))


def main() -> None:
    ap = argparse.ArgumentParser(prog="claudemonitor", description="See and kill everything Claude is running on your machine.")
    ap.add_argument("--list", action="store_true", help="print the process table and token usage once, then exit")
    ap.add_argument("--no-anim", action="store_true", help="skip the startup animation")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    if sys.platform not in ("darwin", "linux"):
        sys.exit("claudemonitor supports macOS and Linux only.")
    if args.no_anim:
        anim.play = lambda: None
    if args.list or not sys.stdin.isatty():
        print_once()
    else:
        try:
            interactive()
        except KeyboardInterrupt:
            pass
