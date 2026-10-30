"""curses dashboard, laid out after nvtop."""

from __future__ import annotations

import contextlib
import curses
import os
import signal
import time

from .model import PM_AUTOSUSPEND_HINT_MS, UTIL_FDINFO, UTIL_PM, Monitor, ProcessRow, Sample

BLOCKS = " ▁▂▃▄▅▆▇█"


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GiB"


def clip(s: str, width: int) -> str:
    if width <= 0:
        return ""
    return s[:width].ljust(width) if len(s) > width else s.ljust(width)


def bar(pct: float, width: int) -> str:
    width = max(0, width)
    filled = int(round(width * max(0.0, min(100.0, pct)) / 100.0))
    return "█" * filled + "░" * (width - filled)


def sparkline(values: list[float], width: int, height: int) -> list[str]:
    """Render a block-character graph, newest sample on the right."""
    if width <= 0 or height <= 0:
        return []
    vals = list(values)[-width:]
    vals = [0.0] * (width - len(vals)) + vals
    rows: list[str] = []
    for r in range(height):
        top = (height - r) * 100.0 / height
        bottom = (height - r - 1) * 100.0 / height
        line = []
        for v in vals:
            if v >= top:
                line.append(BLOCKS[-1])
            elif v <= bottom:
                line.append(" ")
            else:
                frac = (v - bottom) / (top - bottom)
                line.append(BLOCKS[max(1, min(8, int(round(frac * 8))))])
        rows.append("".join(line))
    return rows


def _put(win: curses.window, y: int, x: int, s: str, attr: int = 0) -> None:
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    with contextlib.suppress(curses.error):
        win.addnstr(y, max(0, x), s, max(0, w - max(0, x) - 1), attr)


def _box(win: curses.window, y: int, x: int, h: int, w: int, title: str) -> None:
    if h < 2 or w < 4:
        return
    try:
        sub = win.derwin(h, w, y, x)
        sub.box()
        sub.addnstr(0, 2, f" {title} ", max(0, w - 4), curses.A_BOLD)
    except curses.error:
        pass


class Colors:
    def __init__(self) -> None:
        self.enabled = curses.has_colors()
        if not self.enabled:
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)

    def pct(self, p: float) -> int:
        if not self.enabled:
            return curses.A_BOLD
        if p >= 80:
            return curses.color_pair(3) | curses.A_BOLD
        if p >= 40:
            return curses.color_pair(2) | curses.A_BOLD
        return curses.color_pair(1) | curses.A_BOLD

    @property
    def title(self) -> int:
        return curses.color_pair(5) | curses.A_BOLD if self.enabled else curses.A_REVERSE

    @property
    def accent(self) -> int:
        return curses.color_pair(4) if self.enabled else 0


PROC_HEADER = "   PID USER      NPU%   CMD/s  COLS  MEM      CTX  SUBMIT    COMPL   ERR  COMMAND"


def process_line(row: ProcessRow, width: int) -> str:
    subs = sum(c.submissions for c in row.contexts)
    comps = sum(c.completions for c in row.contexts)
    errs = sum(c.errors for c in row.contexts)
    util = f"{row.util:5.1f}" if row.has_util else "    -"
    head = (
        f"{row.pid:>6} {clip(row.user or '?', 9)} {util} {row.cmds_per_s:7.1f} {row.columns:>5}  "
        f"{clip(fmt_bytes(row.memory_bytes), 8)} {len(row.contexts):>4} {subs:>8} {comps:>8} {errs:>5}  "
    )
    return head + clip(row.command or "(fdinfo not readable)", max(0, width - len(head)))


def draw(stdscr: curses.window, mon: Monitor, sample: Sample, colors: Colors, interval: float, selected: int) -> None:
    h, w = stdscr.getmaxyx()
    st = mon.static
    stdscr.erase()

    _put(stdscr, 0, 0, clip(f" xdnatop  {st.name}  [{st.pci_bdf}]", w), colors.title)
    right = f"{time.strftime('%H:%M:%S')}  {interval:.1f}s  q quit  ↑↓ select  k kill "
    _put(stdscr, 0, max(0, w - len(right)), right, colors.title)

    # Device card ------------------------------------------------------------
    left_w = max(36, min(48, w // 3))
    card_h = 9
    _box(stdscr, 1, 0, card_h, left_w, "Device")
    clocks = "  ".join(f"{k.replace(' Clock', '')}:{v}MHz" for k, v in st.clocks.items())
    lines = [
        f"driver   {st.driver}   fw {st.fw_version}",
        f"AIE      v{st.aie_version}  {st.columns} cols x {st.rows} rows",
        f"power    {sample.ioctl.power_mode or st.power_mode or '?'}   max {st.tops_max} TOPS",
        f"clocks   {clocks or '?'}",
        f"PM       {sample.pm.status:<9} active {sample.pm_active:5.1f}%",
        f"contexts {len(sample.ioctl.contexts)} total, {len(sample.active_contexts)} active",
        f"clients  {len(sample.fdinfo.processes)}"
        + (f"  (+{len(sample.fdinfo.denied_pids)} unreadable)" if sample.fdinfo.denied_pids else ""),
    ]
    for i, line in enumerate(lines):
        _put(stdscr, 2 + i, 2, clip(line, left_w - 4))

    # Meters -----------------------------------------------------------------
    mx = left_w + 1
    mw = w - mx
    _box(stdscr, 1, mx, card_h, mw, "Utilization")
    bw = max(10, mw - 24)
    src = sample.util_source
    npu_label = "NPU  " if src == UTIL_FDINFO else "NPU* " if src == UTIL_PM else "NPU? "
    _put(stdscr, 2, mx + 2, npu_label, curses.A_BOLD)
    _put(stdscr, 2, mx + 7, f"{sample.util:5.1f}% ", colors.pct(sample.util))
    _put(stdscr, 2, mx + 15, bar(sample.util, bw), colors.pct(sample.util))
    _put(stdscr, 3, mx + 2, "COLS ", curses.A_BOLD)
    _put(stdscr, 3, mx + 7, f"{sample.column_util:5.1f}% ", colors.pct(sample.column_util))
    _put(stdscr, 3, mx + 15, bar(sample.column_util, bw), colors.pct(sample.column_util))
    _put(stdscr, 4, mx + 2, "PM   ", curses.A_BOLD)
    _put(stdscr, 4, mx + 7, f"{sample.pm_active:5.1f}% ", colors.accent)
    _put(stdscr, 4, mx + 15, bar(sample.pm_active, bw), colors.accent)
    _put(stdscr, 5, mx + 2, clip(f"CMD/s {sample.cmds_per_s:8.1f}   (completed commands, all contexts)", mw - 4))
    if src == UTIL_FDINFO:
        note = "NPU%  = Δ drm-engine busy time / wall time (fdinfo, exact)"
    elif src == UTIL_PM:
        note = "NPU*  = runtime-PM active share; driver has no fdinfo busy time"
        delay = sample.pm.autosuspend_delay_ms
        if delay > PM_AUTOSUSPEND_HINT_MS:
            note2 = f"autosuspend_delay_ms={delay}: NPU* includes a {delay}ms idle tail per burst; lower it as root"
        else:
            note2 = f"autosuspend_delay_ms={delay}"
    else:
        note = "NPU?  = no NPU client seen yet; source decided on first client"
    _put(stdscr, 6, mx + 2, clip(note, mw - 4))
    if src == UTIL_PM:
        _put(
            stdscr,
            7,
            mx + 2,
            clip(note2, mw - 4),
            colors.pct(100) if sample.pm.autosuspend_delay_ms > PM_AUTOSUSPEND_HINT_MS else 0,
        )
    else:
        _put(stdscr, 7, mx + 2, clip("COLS% = AIE columns held by active hw contexts", mw - 4))
    _put(stdscr, 8, mx + 2, clip("PM%   = PCI runtime-active share of interval (sysfs)", mw - 4))

    # History ----------------------------------------------------------------
    gy = card_h + 1
    gh = max(4, min(10, (h - gy - 8) // 2))
    _box(stdscr, gy, 0, gh + 2, w, "NPU utilization history")
    for i, line in enumerate(sparkline(list(mon.history), w - 4, gh)):
        _put(stdscr, gy + 1 + i, 2, line, colors.pct(sample.util))
    _put(stdscr, gy + 1, w - 6, "100%")
    _put(stdscr, gy + gh, w - 6, "  0%")

    # Processes --------------------------------------------------------------
    py = gy + gh + 2
    ph = h - py - 1
    _box(stdscr, py, 0, ph, w, "Processes")
    _put(stdscr, py + 1, 2, clip(PROC_HEADER, w - 4), curses.A_BOLD | curses.A_UNDERLINE)
    max_rows = max(0, ph - 3)
    if not sample.processes:
        msg = "no process has /dev/accel open"
        if sample.fdinfo.unreadable_fd_dirs:
            msg += f"  ({sample.fdinfo.unreadable_fd_dirs} processes not inspectable without root)"
        _put(stdscr, py + 2, 2, clip(msg, w - 4))
    start = max(0, min(selected, max(0, len(sample.processes) - max_rows)))
    for i, row in enumerate(sample.processes[start : start + max_rows]):
        attr = colors.pct(row.util) if row.util > 0 else 0
        if start + i == selected:
            attr |= curses.A_REVERSE
        _put(stdscr, py + 2 + i, 2, process_line(row, w - 4), attr)

    footer = f" {len(sample.processes)} clients  interval {sample.interval:.2f}s "
    if sample.error:
        footer += f" | {sample.error} "
    if sample.fdinfo.denied_pids:
        footer += f" {len(sample.fdinfo.denied_pids)} clients unreadable (run as root for all users) "
    _put(stdscr, h - 1, 0, clip(footer, w), curses.A_REVERSE)
    stdscr.refresh()


def _close_kill(pending: tuple[int, int] | None) -> None:
    if pending is not None:
        with contextlib.suppress(OSError):
            os.close(pending[1])
    return None


def run(stdscr: curses.window, mon: Monitor, interval: float) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    colors = Colors()
    selected = 0
    sample = mon.poll()
    next_poll = time.monotonic() + interval
    kill_pending: tuple[int, int] | None = None  # (pid, pidfd)
    while True:
        key = stdscr.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        if key in (ord("r"), ord("R")):
            next_poll = 0.0
        elif key == curses.KEY_UP:
            selected = max(0, selected - 1)
        elif key == curses.KEY_DOWN:
            selected = min(max(0, len(sample.processes) - 1), selected + 1)
        elif key in (ord("k"), ord("K")) and sample.processes:
            kill_pending = _close_kill(kill_pending)
            pid = sample.processes[min(selected, len(sample.processes) - 1)].pid
            # A pidfd pins the identity of the process we showed; if it exits and
            # the pid is reused before the user confirms, the signal goes nowhere.
            with contextlib.suppress(OSError):
                kill_pending = (pid, os.pidfd_open(pid))
        elif kill_pending is not None and key in (ord("y"), ord("Y")):
            with contextlib.suppress(OSError):
                signal.pidfd_send_signal(kill_pending[1], signal.SIGTERM)
            kill_pending = _close_kill(kill_pending)
        elif key != -1 and kill_pending is not None:
            kill_pending = _close_kill(kill_pending)

        now = time.monotonic()
        if now >= next_poll:
            sample = mon.poll()
            selected = min(selected, max(0, len(sample.processes) - 1))
            next_poll = now + interval
        draw(stdscr, mon, sample, colors, interval, selected)
        if kill_pending is not None:
            h, w = stdscr.getmaxyx()
            _put(
                stdscr,
                h - 1,
                0,
                clip(f" send SIGTERM to pid {kill_pending[0]}? y/n ", w),
                colors.pct(100) | curses.A_REVERSE,
            )
            stdscr.refresh()
        time.sleep(0.05)
