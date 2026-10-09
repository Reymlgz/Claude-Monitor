<h1 align="center">Claude Monitor</h1>

```text
  ____ _                 _       __  __             _ _
 / ___| | __ _ _   _  __| | ___ |  \/  | ___  _ __ (_) |_ ___  _ __
| |   | |/ _` | | | |/ _` |/ _ \| |\/| |/ _ \| '_ \| | __/ _ \| '__|
| |___| | (_| | |_| | (_| |  __/| |  | | (_) | | | | | || (_) | |
 \____|_|\__,_|\__,_|\__,_|\___||_|  |_|\___/|_| |_|_|\__\___/|_|
```

**See everything Claude is running on your computer, how much CPU and memory it uses, how many tokens it has burned, and kill it with one key.**

> **Works on macOS and Linux only.** Windows is not supported. Linux support is newer and less tested than macOS.

```text
Claude Monitor
▲ HEAVY: Claude is likely heating your Mac and draining the battery.  Biggest: Google Chrome (PID 4821, 94% CPU)
Usage (Claude Code, from local transcripts · $ = API-price equivalent)
  5h window      ███░░░░░░░░░░░░░░░░░   15%  23.3M tok ~$13.72  of your busiest 5h window (151.7M) · 81.0K tok/min
  window time    ███████████████████░   96%  4h48m of 5h · resets 15:00 (in 11m)
  today          ████░░░░░░░░░░░░░░░░   20%  50.7M tok ~$19.90  of your busiest day (254.3M, Sun 04)
  models today   ███████████████████░        ■ sonnet-5-5 94% 47.5M ~$17.66  ■ opus-5-5 6% 3.2M ~$2.24
  projects today ██████████████████░░        ■ my-web-app 92% 46.7M ~$17.15  ■ claudemonitor 6% 3.3M ~$2.33
  last 14 days   ▂·▁·▃▁·▃█▁▃▅▇▂             7 days 834.0M ~$274.23   30 days 1.0B ~$358.86

  Automated browser   6 proc  CPU ██████████  94.0%   MEM  1.4G
  Claude Code         5 proc  CPU █░░░░░░░░░   6.2%   MEM  762M
  Claude app         16 proc  CPU ░░░░░░░░░░   3.2%   MEM  482M

     PID TYPE              CPU                MEM     RUNNING  WHAT
▶  4821  Automated browser ██████████  94.0%   812M       12:04  Google Chrome
  46285* Claude Code       █░░░░░░░░   5.4%   478M       17:21  claude
   8018  Claude app        ░░░░░░░░░   3.2%   482M    23:45:12  Claude app ▸ 16 processes (Enter to expand)
   ...
↑/↓ select   ⏎ expand   x kill   f force-kill   A kill ALL   r refresh   q quit    * = this session
```

The screen above is an illustration. Your own numbers will differ.

## Why this exists

A Claude Code session started Chrome tabs in the background to test animations. They kept running, the laptop's fans spun up and the battery drained fast, and nothing showed that Claude was the cause. `claude doctor` only checks that the installation is healthy, and `claude agents` only manages background sessions. Neither shows which processes use your CPU and memory.

Claude Monitor finds those processes, shows what each one costs, and lets you stop them.

## What it detects

| Type | What it is |
| --- | --- |
| **Claude Code** | Claude Code CLI sessions and everything they spawned (shells, tools) |
| **MCP server** | MCP servers started by Claude (`npx`, `uvx`, `node` ...) |
| **Automated browser** | Chrome/Chromium that Claude launched: headless, remote-debugging, Playwright or Puppeteer, including their tab and GPU helper processes |
| **Chrome bridge** | The Claude-in-Chrome native messaging host |
| **Claude app** | The Claude desktop app and its helper processes |

Any process started by one of these is included too, whatever its name.

## Install

Requires **macOS or Linux** and Python 3.9 or newer. There are no other dependencies. It does not run on Windows.

```sh
git clone https://github.com/Reymlgz/Claude-Monitor.git
cd Claude-Monitor
./install.sh
```

`install.sh` creates a private virtualenv at `~/.claudemonitor-venv` and links the `claudemonitor` command into `~/.local/bin`. Make sure that folder is on your `PATH`. The script prints the line to add if it isn't.

It does not use `pip install` directly because on Homebrew Python (macOS) `pip` may not exist, and `pip3 install` is blocked as an "externally managed environment". If you have `pipx`, `pipx install .` works as well.

**Uninstall:**

```sh
rm ~/.local/bin/claudemonitor
rm -rf ~/.claudemonitor-venv
```

## Usage

```sh
claudemonitor              # interactive screen, with the startup animation
claudemonitor --no-anim    # skip the animation
claudemonitor --list       # print every process and the token usage once, then exit (good for scripts)
claudemonitor --version
```

The interactive screen refreshes itself every few seconds.

### Reading the screen

- **Usage panel:** how many tokens Claude Code has used and what that would cost on the API. See [Token usage](#token-usage).
- **Verdict line:** `● Calm`, `▲ Busy` or `▲ HEAVY`. It turns heavy when Claude's processes together use 100% of one CPU core or more, or 15% of your RAM or more. It names the biggest process when one stands out.
- **Summary by type:** one line per type with its process count, CPU bar and memory.
- **Process list:** sorted by CPU use, busiest first. The selected row is highlighted with a `▶`. Idle processes are dimmed so the ones that matter stand out. While you are moving the selection, the row order is frozen for 8 seconds (only the numbers update) so rows don't jump under your cursor, then it sorts by CPU again.
- **Claude app** appears as a single collapsed row (see [Claude app row](#claude-app-row)). The summary and verdict still count every one of its processes.
- **CPU** is per core, so 200% means two full cores. It is a live sample, not the lifetime average that `ps` shows.
- **MEM** is the process's resident memory (RSS).

### Keys

| Key | Action |
| --- | --- |
| `↑` / `↓` (or `k` / `j`) | Select a process |
| `Enter` / `→` / `←` | Expand or collapse the Claude app row |
| `x` | Stop the selected process politely (SIGTERM) |
| `f` | Force-kill the selected process (SIGKILL) |
| `A` | Kill **all** Claude processes, including every Claude app helper. You must type `yes` and press Enter. Esc cancels |
| `r` | Refresh now |
| `q` | Quit |

### Claude app row

The Claude desktop app runs as many helper processes (renderers, GPU, plugins). They are shown as **one row** with the combined CPU and memory. Select it and press `Enter` to see the individual helpers. `x` or `f` on the collapsed row quits the whole app cleanly. Killing a single helper can crash or blank the app, so Claude Monitor warns you and asks for `y` first.

### Token usage

Claude Code saves every conversation to `~/.claude/projects/` (or the folders in `$CLAUDE_CONFIG_DIR`), including how many tokens each reply used. Claude Monitor adds those up. Nothing is sent anywhere.

The usage panel is always on the main screen, above the processes, and refreshes every 10 seconds. It shows:

- **5h window:** Claude plans meter usage in 5-hour windows. The bar shows this window's tokens as a percentage of your **busiest 5-hour window in the last 30 days**, plus tokens, cost and tokens per minute. It turns yellow at 60% and red at 90%. Your plan's real limit isn't stored on disk, so your own record is used as the yardstick.
- **Window time:** how much of the 5 hours has passed and when the window resets.
- **Today:** today's tokens as a percentage of your busiest day in the last 30 days.
- **Models today** and **projects today:** one bar split by share, with a colored legend for the top three, so you can see which model or which repo is eating the budget.
- **Last 14 days:** one bar per day, plus totals for 7 and 30 days.

On terminals shorter than 32 rows only the 5h window and today bars are shown, so the process list keeps its room. Lines wider than the terminal are cut with `…`. `claudemonitor --list` prints the same panel after the process table.

Good to know:

- **Cost is an estimate of what the same tokens would cost on the API**, using Anthropic's list prices per model, cache writes and cache reads included. On a Pro or Max subscription you don't pay this. It is a way to compare days and projects, not a bill. Models without a known price are counted in tokens and marked with `+` next to the cost.
- **The 5-hour window is estimated** from your message times (it starts at the hour of the first message after the previous window ended). Your plan's real limit and reset time are only shown inside Claude itself.
- Only Claude Code is counted. Usage from claude.ai, the desktop app's chat or the API with your own key doesn't leave transcripts on disk.
- Only the last 30 days are read. The first read takes well under a second, and after that only newly appended lines are parsed.

### Safety

- Processes marked `*` are the session you are running Claude Monitor from (or its parents). **Kill all skips them**, so it won't close your own terminal session. Killing one individually asks for a `y` first.
- Kill all asks you to type the full word `yes`. Anything else cancels and nothing is killed.
- Kill all sends SIGTERM first, waits one second, then force-kills whatever is still alive.
- Claude Monitor only signals processes your own user owns. It never uses `sudo`.

## Platforms

| OS | Status |
| --- | --- |
| macOS | Supported and tested |
| Linux | Supported, but newer and less tested. It reads CPU and memory from `/proc`. Bug reports welcome |
| Windows | Not supported |

## Limitations

- **Your everyday Chrome tabs.** Pages opened through the Claude-in-Chrome extension live inside your normal Chrome process, so they can't be told apart from your own tabs. Only Chrome instances that Claude launched itself are listed. If your normal Chrome is the problem, use Chrome's Task Manager (Shift+Esc) or Activity Monitor.
- **Detection is heuristic.** It works from process names, command lines and parent/child relationships. Unusual setups can be missed or, rarely, mislabelled. Check the process list before you kill anything.
- **Process names can change** between Claude, Chrome and Playwright releases.

## How it works

1. `ps` lists every process with its parent, memory and command line.
2. A short CPU sample is taken (`top` on macOS, `/proc` on Linux) for live CPU%.
3. Processes matching Claude patterns become roots, and everything they spawned is added as well.
4. Token usage is read from Claude Code's transcript files (`~/.claude/projects/**/*.jsonl`), one entry per reply, de-duplicated by message and request id.
5. The list is drawn in the terminal with plain ANSI codes and refreshed in a background thread, so the screen never freezes while sampling.

The code is small:

```text
claudemonitor/
  procs.py   # find and classify processes, CPU and memory sampling
  usage.py   # read Claude Code transcripts, add up tokens and estimated cost
  cli.py     # interactive screen, keys, kill logic
  anim.py    # startup animation
```

## Contributing

Issues and pull requests are welcome. Please include your OS and the output of `claudemonitor --list` in bug reports, with anything private removed. Its usage section lists your project folder names and how much you've used, so cut or rename those if they're private. Detection patterns live at the top of `claudemonitor/procs.py`, so new process names are usually a one-line fix.

## License

MIT. See [LICENSE](LICENSE).
