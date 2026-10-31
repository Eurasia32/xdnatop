"""Command-line entry point."""

from __future__ import annotations

import argparse
import curses
import sys
import time

from . import __version__
from .collectors import list_accel_devices
from .model import PM_AUTOSUSPEND_HINT_MS, UTIL_PM, Monitor
from .ui import PROC_HEADER, process_line, run


def print_once(mon: Monitor, interval: float, width: int = 120) -> int:
    mon.ctx_every = 1  # two polls only; both must query the context table for CMD/s
    mon.poll()
    time.sleep(interval)
    s = mon.poll()
    st = mon.static
    clocks = "  ".join(f"{k}: {v} MHz" for k, v in (s.ioctl.clocks or st.clocks).items())
    print(f"{st.name} [{st.pci_bdf}]  driver {st.driver}  fw {st.fw_version}  AIE v{st.aie_version}")
    print(f"columns {st.columns}  rows {st.rows}  power mode {s.ioctl.power_mode or st.power_mode}  {clocks}")
    print(
        f"NPU {s.util:5.1f}% ({s.util_source})   columns {s.column_util:5.1f}%   cmd/s {s.cmds_per_s:.1f}   "
        f"PM {s.pm.status} {s.pm_active:5.1f}%   interval {s.interval:.2f}s"
    )
    if s.util_source == UTIL_PM and s.pm.autosuspend_delay_ms > PM_AUTOSUSPEND_HINT_MS:
        print(
            f"note: NPU% is runtime-PM derived and includes a {s.pm.autosuspend_delay_ms}ms idle tail per job; "
            f"as root: echo 20 > {st.sysfs_path}/power/autosuspend_delay_ms",
            file=sys.stderr,
        )
    if s.error:
        print(f"warning: {s.error}", file=sys.stderr)
    print(PROC_HEADER)
    for row in s.processes:
        print(process_line(row, width))
    if s.fdinfo.denied_pids:
        print(f"{len(s.fdinfo.denied_pids)} client(s) not readable without root: {s.fdinfo.denied_pids}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xdnatop", description="nvtop-like monitor for AMD XDNA NPUs")
    p.add_argument("-i", "--interval", type=float, default=1.0, help="refresh interval in seconds (default 1.0)")
    p.add_argument("-d", "--device", type=int, default=None, help="accel device index (default: first amdxdna device)")
    p.add_argument("-1", "--once", action="store_true", help="print one sample and exit")
    p.add_argument("--list", action="store_true", help="list amdxdna devices and exit")
    p.add_argument(
        "--no-ioctl",
        action="store_true",
        help="fdinfo + sysfs only; never open the device (no hw context table, no device wakeups)",
    )
    p.add_argument(
        "--util-source",
        choices=["auto", "fdinfo", "pm"],
        default="auto",
        help="force the NPU%% source: fdinfo busy time or runtime-PM share (default: auto-detect)",
    )
    p.add_argument(
        "--ctx-every",
        type=int,
        default=5,
        help="in PM-derived mode, refresh the hw-context table every N polls (default 5)",
    )
    p.add_argument(
        "--ioctl-timeout",
        type=float,
        default=3.0,
        help="seconds to wait for an amdxdna ioctl before falling back to --no-ioctl mode (default 3)",
    )
    p.add_argument("--version", action="version", version=f"xdnatop {__version__}")
    args = p.parse_args(argv)

    devices = list_accel_devices()
    if args.list:
        for d in devices:
            print(f"{d.index}\t{d.dev_path}\t{d.pci_bdf}\t{d.name}\tfw {d.fw_version}")
        return 0 if devices else 1
    if not devices:
        print("no amdxdna device under /sys/class/accel (is the amdxdna module loaded?)", file=sys.stderr)
        return 1
    if args.device is None:
        dev = devices[0]
    else:
        match = [d for d in devices if d.index == args.device]
        if not match:
            print(f"accel{args.device} is not an amdxdna device", file=sys.stderr)
            return 1
        dev = match[0]

    interval = max(0.1, args.interval)
    mon = Monitor(
        dev,
        use_ioctl=not args.no_ioctl,
        ioctl_timeout=max(0.5, args.ioctl_timeout),
        ctx_every=args.ctx_every,
        util_source=args.util_source,
    )
    try:
        if args.once:
            return print_once(mon, interval)
        curses.wrapper(run, mon, interval)
        return 0
    finally:
        mon.close()
