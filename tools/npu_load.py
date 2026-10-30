#!/usr/bin/env python3
"""Sustained NPU load generator for testing xdnatop.

Replays the xrt-smi "throughput" validate recipe (validate.xclbin + nop.elf,
kernel instance DPU) for a chosen duration instead of a fixed iteration count.

The test binaries ship inside XRT's xrt_smi_<platform>.a archive:

    mkdir /tmp/bins && cd /tmp/bins && ar x /opt/xilinx/xrt/share/amdxdna/bins/xrt_smi_phx.a

Run with the Python that matches the installed pyxrt (see /opt/xilinx/xrt/python):

    python3.13 tools/npu_load.py --bins /tmp/bins --seconds 30 --inflight 8
"""

from __future__ import annotations

import argparse
import os
import sys
import time


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bins", required=True, help="directory with validate.xclbin and nop.elf")
    p.add_argument("--seconds", type=float, default=30.0)
    p.add_argument("--inflight", type=int, default=8, help="commands kept in flight")
    p.add_argument(
        "--duty", type=float, default=1.0, help="fraction of time to submit (0..1); sleeps the rest per 100ms slot"
    )
    p.add_argument("--xrt-python", default="/opt/xilinx/xrt/python")
    p.add_argument("--device", type=int, default=0)
    args = p.parse_args()

    sys.path.insert(0, args.xrt_python)
    try:
        import pyxrt
    except ImportError as exc:
        print(f"pyxrt not importable ({exc}); use the python version pyxrt was built for", file=sys.stderr)
        return 2

    dev = pyxrt.device(args.device)
    uuid = dev.register_xclbin(pyxrt.xclbin(os.path.join(args.bins, "validate.xclbin")))
    hwctx = pyxrt.hw_context(dev, uuid)
    module = pyxrt.module(pyxrt.elf(os.path.join(args.bins, "nop.elf")))
    kernel = pyxrt.ext.kernel(hwctx, module, "DPU")
    bos = [pyxrt.ext.bo(hwctx, 8) for _ in range(5)]

    def make_run():
        run = pyxrt.run(kernel)
        run.set_arg(0, 3)  # opcode, as in recipe_throughput.json
        for idx, bo in zip(range(3, 8), bos, strict=True):
            run.set_arg(idx, bo)
        return run

    runs = [make_run() for _ in range(max(1, args.inflight))]
    print(f"pid {os.getpid()}: {len(runs)} in flight for {args.seconds:.0f}s, duty {args.duty:.2f}", flush=True)
    done = 0
    t0 = time.monotonic()
    last = t0
    while time.monotonic() - t0 < args.seconds:
        slot = time.monotonic()
        for r in runs:
            r.start()
        for r in runs:
            r.wait2()
            done += 1
        if args.duty < 1.0:
            busy = time.monotonic() - slot
            time.sleep(max(0.0, busy * (1.0 - args.duty) / max(args.duty, 1e-3)))
        now = time.monotonic()
        if now - last >= 5.0:
            print(f"  {done / (now - t0):8.0f} op/s", flush=True)
            last = now
    print(f"done: {done} commands, {done / (time.monotonic() - t0):.0f} op/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
