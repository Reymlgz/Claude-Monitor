"""Find every process on this machine that Claude started or is running."""

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field


@dataclass
class Proc:
    pid: int
    ppid: int
    cpu: float  # percent of one core
    rss_kb: int
    mem_pct: float  # percent of total RAM
    uptime: str
    command: str
    kind: str = ""
    protected: bool = False  # this tool or one of its ancestors
    label: str = ""  # display text overriding the command (used by grouped rows)
    members: list = field(default_factory=list)  # set on a group row: every process in it
    is_child: bool = False  # shown indented under an expanded group


def _run(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def total_memory_bytes() -> int:
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo") as f:
                return int(f.readline().split()[1]) * 1024  # "MemTotal: N kB"
        except (OSError, ValueError, IndexError):
            return 0
    try:
        return int(_run("sysctl", "-n", "hw.memsize").strip())
    except ValueError:
        return 0


def _proc_ticks() -> dict[int, int]:
    ticks = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                data = f.read()
            fields = data[data.rindex(")") + 2:].split()  # after "pid (comm) "
            ticks[int(name)] = int(fields[11]) + int(fields[12])  # utime + stime
        except (OSError, ValueError, IndexError):
            pass
    return ticks


def _instant_cpu() -> dict[int, float]:
    """Real current CPU%. `ps` reports a lifetime average, so sample over 1s."""
    if sys.platform.startswith("linux"):
        hz = os.sysconf("SC_CLK_TCK")
        before, t0 = _proc_ticks(), time.monotonic()
        time.sleep(1)
        after, dt = _proc_ticks(), time.monotonic() - t0
        return {pid: (t - before[pid]) / hz / dt * 100 for pid, t in after.items() if pid in before}
    out = _run("top", "-l", "2", "-s", "1", "-n", "500", "-o", "cpu", "-stats", "pid,cpu")
    blocks = re.split(r"^PID\s+%CPU\s*$", out, flags=re.M)
    cpu = {}
    if len(blocks) > 2:  # the last block is the second (accurate) sample
        for line in blocks[-1].splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit():
                try:
                    cpu[int(parts[0])] = float(parts[1])
                except ValueError:
                    pass
    return cpu


# Matched against the full command line (lowercased).
CLAUDE_CLI = re.compile(r"(^|/)claude(\s|$)|/claude/versions/|@anthropic-ai/claude-code|/claude-code/")
CLAUDE_APP = re.compile(r"/claude\.app/|/claude-desktop|/opt/claude")
CHROME_BRIDGE = re.compile(r"claude-in-chrome|chrome-native-host|claude-chrome")
MCP = re.compile(r"@modelcontextprotocol|playwright/mcp|mcp-server|chrome-devtools-mcp|[/ ]mcp[- ]")
BROWSER = re.compile(r"google chrome|google-chrome|/chrome(\s|$)|chromium|chrome-headless-shell|headless_shell|ms-playwright|puppeteer")
AUTOMATION = re.compile(r"--remote-debugging|--headless|--enable-automation|--user-data-dir=\S*(claude|playwright|puppeteer|mcp)|ms-playwright|puppeteer")


def _ancestors(pid: int, parent: dict[int, int]) -> set[int]:
    seen = set()
    while pid and pid not in seen:
        seen.add(pid)
        pid = parent.get(pid, 0)
    return seen


def scan(cpu_mode: str = "instant") -> list[Proc]:
    mem_total = total_memory_bytes()
    out = _run("ps", "-axo", "pid=,ppid=,%cpu=,rss=,etime=,command=")
    rows: dict[int, Proc] = {}
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid, ppid, cpu, rss = int(parts[0]), int(parts[1]), float(parts[2]), int(parts[3])
        except ValueError:
            continue
        mem_pct = (rss * 1024 / mem_total * 100) if mem_total else 0.0
        rows[pid] = Proc(pid, ppid, cpu, rss, mem_pct, parts[4], parts[5])

    if cpu_mode == "instant":
        for pid, c in _instant_cpu().items():
            if pid in rows:
                rows[pid].cpu = c

    parent = {p.pid: p.ppid for p in rows.values()}
    children: dict[int, list[int]] = {}
    for p in rows.values():
        children.setdefault(p.ppid, []).append(p.pid)

    me = os.getpid()
    protected = _ancestors(me, parent)

    # 1) roots: processes that are clearly Claude's
    roots: dict[int, str] = {}
    for p in rows.values():
        if p.pid == me:
            continue
        cmd = p.command.lower()
        if "claudemonitor" in cmd:
            continue
        if CHROME_BRIDGE.search(cmd):
            roots[p.pid] = "Chrome bridge"
        elif CLAUDE_APP.search(cmd):
            roots[p.pid] = "Claude app"
        elif CLAUDE_CLI.search(cmd.split(" --", 1)[0]) and "grep" not in cmd:
            roots[p.pid] = "Claude Code"
        elif MCP.search(cmd):
            roots[p.pid] = "MCP server"

    # 2) browsers that look automated (headless / remote debugging / playwright...)
    for p in rows.values():
        cmd = p.command.lower()
        if p.pid in roots or p.pid == me:
            continue
        if BROWSER.search(cmd) and "--type=" not in cmd and AUTOMATION.search(cmd):
            roots[p.pid] = "Automated browser"

    # 3) every descendant of a root belongs to Claude too
    found: dict[int, str] = dict(roots)
    stack = list(roots.items())
    while stack:
        pid, kind = stack.pop()
        for child in children.get(pid, []):
            if child in found or child == me:
                continue
            ckind = kind
            ccmd = rows[child].command.lower()
            if BROWSER.search(ccmd) and kind in ("Claude Code", "MCP server"):
                ckind = "Automated browser"
            found[child] = ckind
            stack.append((child, ckind))

    result = []
    for pid, kind in found.items():
        p = rows[pid]
        p.kind = kind
        p.protected = pid in protected
        result.append(p)
    result.sort(key=lambda p: (p.cpu, p.rss_kb), reverse=True)
    return result
