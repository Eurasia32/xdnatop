"""Sampling model: turns raw collector output into utilization numbers."""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field

from .collectors import (
    DeviceStatic,
    FdinfoScan,
    IoctlCollector,
    IoctlSample,
    ProcessUsage,
    RuntimePm,
    read_runtime_pm,
    scan_fdinfo,
)
from .uapi import HwContext

UTIL_FDINFO = "fdinfo"  # Δ drm-engine busy time / Δt, per client (exact)
UTIL_PM = "pm"  # runtime-PM active share (device only; includes autosuspend tail)
UTIL_NONE = "none"  # nothing usable yet

# Above this autosuspend delay the PM-derived number is dominated by the idle tail.
PM_AUTOSUSPEND_HINT_MS = 100


@dataclass
class ProcessRow:
    pid: int
    user: str
    command: str
    busy_ns: int
    util: float  # 0..100, NPU time share over the last interval; NaN if not measurable
    memory_bytes: int
    contexts: list[HwContext] = field(default_factory=list)
    cmds_per_s: float = 0.0  # completed commands per second over the last interval

    @property
    def has_util(self) -> bool:
        return self.util == self.util  # not NaN

    @property
    def columns(self) -> int:
        return sum(c.num_col for c in self.contexts if c.active)


@dataclass
class Sample:
    ts: float
    pm: RuntimePm
    fdinfo: FdinfoScan
    ioctl: IoctlSample
    # derived
    interval: float = 0.0
    util: float = 0.0  # device NPU utilization, % of wall time, capped at 100
    util_source: str = UTIL_NONE
    pm_active: float = 0.0  # % of interval the PCI device was runtime-active
    column_util: float = 0.0  # % of AIE columns held by active contexts
    cmds_per_s: float = 0.0  # device-wide completed commands per second
    processes: list[ProcessRow] = field(default_factory=list)

    @property
    def active_contexts(self) -> list[HwContext]:
        return [c for c in self.ioctl.contexts if c.active]

    @property
    def error(self) -> str:
        return self.ioctl.error


def _ctx_key(ctx: HwContext) -> tuple[int, int]:
    """Identity of a hardware context across samples.

    (pid, context_id) only: hwctx_id is the firmware slot and changes when the
    runqueue connects or migrates the context (observed 0 -> 5 at first
    connect on xrt-amdxdna 2.21). A context destroyed and recreated with the
    same context_id is caught by the monotonic completions check in the caller.
    """
    return (ctx.pid, ctx.context_id)


def _ratio(delta: int, interval_ns: float) -> float:
    if interval_ns <= 0 or delta < 0:
        return 0.0
    return max(0.0, min(100.0, delta * 100.0 / interval_ns))


def derive(current: Sample, previous: Sample | None, columns: int, fdinfo_busy: bool | None = None) -> None:
    """Fill the derived fields of ``current`` from the delta against ``previous``.

    ``fdinfo_busy`` says whether the driver exports drm-engine busy time
    (None = not known yet). Without it the device number falls back to the
    runtime-PM active share and per-process utilization is NaN.
    """
    # Busy time is tracked per DRM client (drm-client-id is unique for the
    # driver's lifetime), never per pid: pids get reused and one process may
    # open the device several times.
    prev_busy: dict[tuple[int, int], int] = {}
    prev_compl: dict[tuple[int, int, int], int] = {}
    if previous is not None:
        current.interval = max(0.0, current.ts - previous.ts)
        for pid, proc in previous.fdinfo.processes.items():
            for cid, client in proc.clients.items():
                prev_busy[(pid, cid)] = client.busy_ns
        for ctx in previous.ioctl.contexts:
            prev_compl[_ctx_key(ctx)] = ctx.completions
    interval = current.interval
    interval_ns = interval * 1e9

    if fdinfo_busy is None:
        fdinfo_busy = any(p.has_engine_stats for p in current.fdinfo.processes.values()) or None

    # Command rates span the time between two real context-table fetches (the
    # table is only refreshed every few polls in PM-derived mode); when the
    # table was not refreshed this poll, the previous rate is carried over.
    ctx_interval = 0.0
    if previous is not None and current.ioctl.ctx_ts > previous.ioctl.ctx_ts > 0:
        ctx_interval = current.ioctl.ctx_ts - previous.ioctl.ctx_ts
    elif previous is not None and previous.ioctl.ctx_ts == 0 and current.ioctl.ctx_ts == 0:
        ctx_interval = interval  # ctx_ts unset (tests / no ioctl): fall back to the poll interval
    prev_rate = {r.pid: r.cmds_per_s for r in previous.processes} if previous is not None else {}

    def ctx_rate(ctxs: list[HwContext], carry: float) -> float:
        if previous is None:
            return 0.0
        if ctx_interval <= 0:
            return carry
        delta = 0
        for ctx in ctxs:
            old = prev_compl.get(_ctx_key(ctx))
            if old is not None and ctx.completions >= old:
                delta += ctx.completions - old
        return delta / ctx_interval

    def busy_delta(pid: int, proc: ProcessUsage) -> int:
        delta = 0
        for cid, client in proc.clients.items():
            old = prev_busy.get((pid, cid))
            if old is not None and client.busy_ns >= old:
                delta += client.busy_ns - old
            # Unknown client (new since last sample) or a counter that went
            # backwards (client id reused after a driver reload): no delta.
        return delta

    ctx_by_pid: dict[int, list[HwContext]] = {}
    for ctx in current.ioctl.contexts:
        ctx_by_pid.setdefault(ctx.pid, []).append(ctx)

    rows: list[ProcessRow] = []
    total_delta = 0
    nan = float("nan")
    for pid, proc in current.fdinfo.processes.items():
        delta = busy_delta(pid, proc)
        total_delta += delta
        ctxs = ctx_by_pid.pop(pid, [])
        if fdinfo_busy and previous is not None:
            util = _ratio(delta, interval_ns)
        elif fdinfo_busy:
            util = 0.0
        else:
            util = nan
        rows.append(
            ProcessRow(
                pid=pid,
                user=proc.user,
                command=proc.command,
                busy_ns=proc.busy_ns,
                util=util,
                memory_bytes=proc.memory_bytes,
                contexts=ctxs,
                cmds_per_s=ctx_rate(ctxs, prev_rate.get(pid, 0.0)),
            )
        )
    # Contexts whose owner we could not read via fdinfo (other users without root).
    for pid, ctxs in ctx_by_pid.items():
        rows.append(
            ProcessRow(
                pid=pid,
                user="",
                command="",
                busy_ns=0,
                util=nan,
                memory_bytes=0,
                contexts=ctxs,
                cmds_per_s=ctx_rate(ctxs, prev_rate.get(pid, 0.0)),
            )
        )
    rows.sort(key=lambda r: (-(r.util if r.has_util else -1.0), -r.cmds_per_s, -r.busy_ns, r.pid))
    current.processes = rows
    current.cmds_per_s = ctx_rate(current.ioctl.contexts, previous.cmds_per_s if previous is not None else 0.0)

    if previous is not None:
        a = current.pm.active_ms - previous.pm.active_ms
        s = current.pm.suspended_ms - previous.pm.suspended_ms
        if a >= 0 and s >= 0 and a + s > 0:
            current.pm_active = a * 100.0 / (a + s)
        else:
            current.pm_active = 100.0 if current.pm.status == "active" else 0.0
    else:
        current.pm_active = 100.0 if current.pm.status == "active" else 0.0

    if fdinfo_busy:
        current.util_source = UTIL_FDINFO
        current.util = _ratio(total_delta, interval_ns) if previous is not None else 0.0
    elif fdinfo_busy is False:
        current.util_source = UTIL_PM
        current.util = current.pm_active
    else:
        current.util_source = UTIL_NONE
        current.util = 0.0

    if columns > 0:
        used = sum(c.num_col for c in current.active_contexts)
        current.column_util = max(0.0, min(100.0, used * 100.0 / columns))


class Monitor:
    """Owns the collectors and the sample history for one device."""

    def __init__(
        self,
        dev: DeviceStatic,
        history_len: int = 600,
        use_ioctl: bool = True,
        ioctl_timeout: float = 3.0,
        ctx_every: int = 5,
        util_source: str = "auto",
    ) -> None:
        self.static = dev
        self.ctx_every = max(1, ctx_every)
        self.forced_source = util_source  # "auto", UTIL_FDINFO or UTIL_PM
        self.ioctl = IoctlCollector(dev, timeout=ioctl_timeout, enabled=use_ioctl)
        self.previous: Sample | None = None
        self.current: Sample | None = None
        self.history: deque[float] = deque(maxlen=history_len)
        self.pm_history: deque[float] = deque(maxlen=history_len)
        self._self_pid = os.getpid()
        # Learned from the first client seen: does this driver export busy time?
        # A forced source pins it up front.
        self.fdinfo_busy: bool | None = {UTIL_FDINFO: True, UTIL_PM: False}.get(util_source)
        self._polls = 0

    def poll(self) -> Sample:
        pm = read_runtime_pm(self.static.sysfs_path)
        own = {self._self_pid, self.ioctl.helper_pid} - {0}
        fdinfo = scan_fdinfo(self.static.index, skip_pids=frozenset(own))
        if self.fdinfo_busy is None and fdinfo.processes:
            self.fdinfo_busy = any(p.has_engine_stats for p in fdinfo.processes.values())
        have_clients = bool(fdinfo.processes or fdinfo.denied_pids or fdinfo.unreadable_fd_dirs)
        # In PM-derived mode our own ioctl wakes the device and inflates the number,
        # so only refresh the context table every few polls there.
        want_ctx = have_clients and (self.fdinfo_busy is not False or self._polls % self.ctx_every == 0)
        self._polls += 1
        if want_ctx:
            ioctl = self.ioctl.sample(True)
            ioctl.ctx_ts = time.monotonic()
        else:
            ioctl = IoctlSample(
                skipped=True,
                contexts=list(self.current.ioctl.contexts) if self.current else [],
                clocks=dict(self.current.ioctl.clocks) if self.current else dict(self.static.clocks),
                ctx_ts=self.current.ioctl.ctx_ts if self.current else 0.0,
            )
        sample = Sample(ts=time.monotonic(), pm=pm, fdinfo=fdinfo, ioctl=ioctl)
        derive(sample, self.current, self.static.columns, self.fdinfo_busy)
        self.previous, self.current = self.current, sample
        if self.previous is not None:
            self.history.append(sample.util)
            self.pm_history.append(sample.pm_active)
        return sample

    def close(self) -> None:
        self.ioctl.close()
