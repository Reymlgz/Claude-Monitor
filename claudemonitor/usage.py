"""Read Claude Code's own transcripts and add up the tokens it used."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

WINDOW = 5 * 3600  # Claude plans meter usage in 5-hour windows

# $ per million tokens: (input, output, cache-read multiplier of input).
# Cache writes are 1.25x input (5-minute) or 2x input (1-hour) on every model.
# Longest matching prefix wins. Unknown models are counted in tokens but not in $.
PRICES = {
    "claude-fable-5-1": (10.00, 50.00, 0.025),
    "claude-mythos-5-1": (10.00, 50.00, 0.025),
    "claude-fable-5": (10.00, 50.00, 0.1),
    "claude-mythos-5": (10.00, 50.00, 0.1),
    "claude-opus-5-5": (4.00, 20.00, 0.05),
    "claude-opus-5": (5.00, 25.00, 0.1),
    "claude-opus-4-8": (5.00, 25.00, 0.1),
    "claude-opus-4-7": (5.00, 25.00, 0.1),
    "claude-opus-4-6": (5.00, 25.00, 0.1),
    "claude-sonnet-5-5": (2.00, 10.00, 0.1),
    "claude-sonnet-5": (2.00, 10.00, 0.1),
    "claude-sonnet-4-6": (3.00, 15.00, 0.1),
    "claude-haiku-5-5": (0.10, 0.50, 0.1),  # $0.50 / $2.50 above 100K-token prompts
    "claude-haiku-4-5": (1.00, 5.00, 0.1),
}


@dataclass
class Entry:
    ts: float
    model: str
    inp: int
    out: int
    cw5: int  # cache write, 5-minute
    cw1: int  # cache write, 1-hour
    cr: int  # cache read
    project: str

    @property
    def tokens(self) -> int:
        return self.inp + self.out + self.cw5 + self.cw1 + self.cr

    @property
    def cost(self) -> float | None:
        key = max((k for k in PRICES if self.model.startswith(k)), key=len, default=None)
        if key is None:
            return None
        pin, pout, read = PRICES[key]
        if key == "claude-haiku-5-5" and self.inp + self.cw5 + self.cw1 + self.cr > 100_000:
            pin, pout = 0.50, 2.50
        return (self.inp * pin + self.out * pout + self.cw5 * pin * 1.25
                + self.cw1 * pin * 2 + self.cr * pin * read) / 1e6


@dataclass
class Totals:
    tokens: int = 0
    inp: int = 0
    out: int = 0
    cache_write: int = 0
    cache_read: int = 0
    cost: float = 0.0
    unpriced: int = 0  # messages from models without a known price
    messages: int = 0

    def add(self, e: Entry) -> None:
        self.tokens += e.tokens
        self.inp += e.inp
        self.out += e.out
        self.cache_write += e.cw5 + e.cw1
        self.cache_read += e.cr
        self.messages += 1
        c = e.cost
        if c is None:
            self.unpriced += 1
        else:
            self.cost += c


@dataclass
class Report:
    today: Totals = field(default_factory=Totals)
    week: Totals = field(default_factory=Totals)
    month: Totals = field(default_factory=Totals)
    window: Totals = field(default_factory=Totals)  # current 5-hour window
    window_start: float | None = None  # None when no window is active
    burn_per_min: float = 0.0  # tokens/minute in the current window
    peak_window: int = 0  # most tokens in any 5-hour window, last 30 days (current included)
    peak_day: tuple[str, int] = ("", 0)  # busiest day, last 30 days (today included)
    by_model: dict[str, Totals] = field(default_factory=dict)  # today
    by_project: dict[str, Totals] = field(default_factory=dict)  # today
    daily: list[tuple[str, Totals]] = field(default_factory=list)  # last 14 days, oldest first
    found: bool = False  # any transcripts at all


def config_dirs() -> list[Path]:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        dirs = [Path(d).expanduser() for d in env.split(",") if d.strip()]
    else:
        dirs = [Path.home() / ".claude", Path.home() / ".config" / "claude"]
    return [d / "projects" for d in dirs if (d / "projects").is_dir()]


def _project_name(path: Path, root: Path) -> str:
    # projects/-Users-me-code-app/<session>.jsonl -> "app"
    folder = path.relative_to(root).parts[0]
    return folder.rstrip("-").split("-")[-1] or folder


def _ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


class UsageReader:
    """Keeps parsed transcripts in memory and only reads what was appended since."""

    def __init__(self, days: int = 30):
        self.days = days
        self._files: dict[Path, tuple[int, list[Entry]]] = {}  # path -> (bytes read, entries)
        self._seen: set[str] = set()  # message id + request id, transcripts repeat them

    def _read(self, path: Path, root: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        offset, entries = self._files.get(path, (0, []))
        if size < offset:  # rewritten: start over
            offset, entries = 0, []
        if size == offset:
            return
        project = _project_name(path, root)
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            return
        end = data.rfind(b"\n") + 1  # leave a half-written last line for next time
        for raw in data[:end].splitlines():
            if b'"usage"' not in raw or b'"assistant"' not in raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            msg = rec.get("message") or {}
            usage = msg.get("usage")
            model = msg.get("model") or ""
            if rec.get("type") != "assistant" or not usage or model == "<synthetic>":
                continue
            key = f"{msg.get('id')}:{rec.get('requestId')}"
            if key in self._seen:
                continue
            self._seen.add(key)
            cc = usage.get("cache_creation") or {}
            cw = usage.get("cache_creation_input_tokens") or 0
            cw1 = cc.get("ephemeral_1h_input_tokens") or 0
            entries.append(Entry(
                ts=_ts(rec.get("timestamp", "")),
                model=model.split("[")[0],
                inp=usage.get("input_tokens") or 0,
                out=usage.get("output_tokens") or 0,
                cw5=max(0, cw - cw1),
                cw1=cw1,
                cr=usage.get("cache_read_input_tokens") or 0,
                project=os.path.basename((rec.get("cwd") or "").rstrip("/")) or project,
            ))
        self._files[path] = (offset + end, entries)

    def report(self) -> Report:
        now = time.time()
        cutoff = now - self.days * 86400
        roots = config_dirs()
        rep = Report(found=bool(roots))
        for root in roots:
            for path in root.rglob("*.jsonl"):
                try:
                    if path.stat().st_mtime < cutoff:
                        continue
                except OSError:
                    continue
                self._read(path, root)

        entries = sorted((e for _, es in self._files.values() for e in es if e.ts >= cutoff),
                         key=lambda e: e.ts)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        days = [datetime.fromtimestamp(today - i * 86400).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
        daily = {d: Totals() for d in days}
        windows: dict[float, int] = {}
        per_day: dict[str, int] = {}

        # 5-hour windows start at the hour of the first message after the previous one ended
        start = None
        for e in entries:
            if start is None or e.ts >= start + WINDOW:
                start = e.ts - e.ts % 3600
            windows[start] = windows.get(start, 0) + e.tokens
            rep.month.add(e)
            if e.ts >= now - 7 * 86400:
                rep.week.add(e)
            if e.ts >= today:
                rep.today.add(e)
                rep.by_model.setdefault(e.model, Totals()).add(e)
                rep.by_project.setdefault(e.project, Totals()).add(e)
            d = datetime.fromtimestamp(e.ts).strftime("%Y-%m-%d")
            per_day[d] = per_day.get(d, 0) + e.tokens
            if d in daily:
                daily[d].add(e)
        if start is not None and now < start + WINDOW:
            rep.window_start = start
            for e in entries:
                if e.ts >= start:
                    rep.window.add(e)
            minutes = max(1.0, (now - start) / 60)
            rep.burn_per_min = rep.window.tokens / minutes
        rep.daily = list(daily.items())
        rep.peak_window = max(windows.values(), default=0)
        rep.peak_day = max(per_day.items(), key=lambda kv: kv[1], default=("", 0))
        return rep
