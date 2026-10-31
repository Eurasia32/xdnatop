# xdnatop

`xdnatop` is an nvtop-like terminal monitor for AMD XDNA NPUs (Ryzen AI, `amdxdna` driver) on Linux.

It reads the same kernel interfaces nvtop uses for GPUs instead of parsing `xrt-smi` output:

| Source | What it provides | Privileges |
|---|---|---|
| `/proc/<pid>/fdinfo/<fd>` (DRM usage stats) | `drm-engine-*` NPU busy time and `drm-total-memory` per client | own processes; root for other users |
| `DRM_IOCTL_AMDXDNA_GET_ARRAY` / `GET_INFO` | hardware contexts (pid, columns, submissions, completions, errors, state), power mode, clocks, AIE geometry, firmware | any user with access to `/dev/accel/accelN` |
| `/sys/class/accel/accelN/device` | device name, firmware, runtime PM state and counters | any user |

```
 xdnatop  NPU Phoenix  [0000:66:00.1]                     01:51:59  0.5s  q quit  ↑↓ select  k kill
┌─ Device ─────────────────────────┐ ┌─ Utilization ───────────────────────────────────────────────┐
│ driver   amdxdna   fw 1.5.5.391  │ │ NPU   56.1%  ██████████████████████░░░░░░░░░░░░░░░░░        │
│ AIE      v1.1  5 cols x 6 rows   │ │ COLS 100.0%  ███████████████████████████████████████        │
│ power    default   max 10 TOPS   │ │ PM   100.0%  ███████████████████████████████████████        │
│ clocks   MP-NPU:960MHz H:1800MHz │ │ CMD/s  16503.5   (completed commands, all contexts)         │
│ PM       active    active 100.0% │ │ NPU%  = Δ drm-engine busy time / wall time (fdinfo, exact)  │
│ contexts 1 total, 1 active       │ │ COLS% = AIE columns held by active hw contexts              │
│ clients  1                       │ │ PM%   = PCI runtime-active share of interval (sysfs)        │
└──────────────────────────────────┘ └─────────────────────────────────────────────────────────────┘
┌─ NPU utilization history ────────────────────────────────────────────────────────────────────────┐
│                                                                                             100% │
│                                                                                                  │
│                                                                                    ▂▃▃▃▃▃▃▃▃▃▃▃▃ │
│                                                                                    █████████████ │
│                                                                                    █████████████ │
│                                                                                    █████████  0% │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
┌─ Processes ──────────────────────────────────────────────────────────────────────────────────────┐
│    PID USER      NPU%   CMD/s  COLS  MEM      CTX  SUBMIT    COMPL   ERR  COMMAND                │
│  25702 lk         56.1 16503.5     5  64.1MiB     1   163920   163917     0  python3.13 tools/np │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 1 clients  interval 0.52s
```

## Metrics

- **NPU%** is the share of wall time the driver had at least one job in flight for a client:
  `Δ drm-engine-* / Δt` per client from DRM fdinfo; the device value is the sum, capped at 100.
  The engine key is `npu` on the in-tree driver (kernel 7.4+) and `npu-amdxdna` or
  `amdxdna_accel_driver` on the xrt-amdxdna DKMS builds; all are handled. On drivers without the
  export (in-tree 6.14 to 7.3) the meter reads `n/a`, the history graph switches to CMD/s, and the
  source is shown as `unavailable`. `xdnatop --util-source off` forces that mode for testing.
- **CMD/s** is completed commands per second from the hardware-context counters, per process and
  device-wide. It is throughput, not a time share, and works on every driver version.
- **COLS%** is the fraction of AIE columns currently held by active hardware contexts.
- **PM%** is the share of the interval the PCI device was runtime-active, from sysfs. It includes
  the monitor's own wakeups (every hardware-context query resumes the device; `--ctx-every N` or
  `--no-ioctl` reduce them). It is not a utilization proxy: gaps shorter than
  `autosuspend_delay_ms` never suspend the device, and when gaps are longer the kernel books the
  roughly 100 ms suspend and resume transitions as active time. Measured on NPU Phoenix, PM% read
  100 at every duty cycle from 20% to 100%, and lowering `autosuspend_delay_ms` to 20 cut a bursty
  workload from 32000 to 240 commands per second. Leave the delay alone.

Power is not shown: the `SENSORS` query returns no valid reading on current firmware and
`xrt-smi` reports `Estimated Power: N/A` for the same reason.

## Requirements

- Linux with the `amdxdna` driver: in-tree (kernel 6.14+) or the `xrt-amdxdna` DKMS build. NPU%
  needs the fdinfo busy-time export (DKMS, or in-tree from 7.4); older in-tree kernels get the
  context table, memory and CMD/s but no NPU%.
- Python 3.10+. No third-party packages.

## Usage

```sh
python3 -m xdnatop            # full-screen TUI
python3 -m xdnatop --once     # one sample to stdout
python3 -m xdnatop --no-ioctl # fdinfo + sysfs only
python3 -m xdnatop --list     # enumerate amdxdna devices
pip install .                # installs the `xdnatop` command
```

Keys: `q` quit, `r` refresh now, `↑`/`↓` select a process, `k` then `y` send SIGTERM to it.

## Hung driver protection

All amdxdna ioctls take the driver's `dev_lock` and resume the device. If the firmware stops
responding, `open()` and every query block in uninterruptible sleep. `xdnatop` runs them in a
forked helper and abandons it after `--ioctl-timeout` seconds (default 3), continuing with fdinfo
and sysfs only. The abandoned helper stays in `D` state until the driver releases it; that is a
driver condition, not something user space can clean up.

## Verified on hardware

NPU Phoenix (Ryzen 7040), kernel 7.3.0-rc2, xrt-amdxdna DKMS 2.21 with the `num_rqs` fix below,
load from `tools/npu_load.py` (the xrt-smi throughput recipe replayed for a chosen duration):

| load | xdnatop NPU% | xdnatop CMD/s | generator op/s |
|---|---|---|---|
| 8 commands in flight, 100% duty | 94.5 | 32693 | 32715 |
| 8 in flight, 50% duty | 40.2 | 13290 | 13288 |

COLS% read 100 (all 5 columns held by the one context); `k` + `y` in the TUI terminated the load
process through its pidfd. Two concurrent generators were split correctly per process.

The in-tree 7.3 driver was loaded on the same machine to test the no-busy-time path: xdnatop ran
(source `unavailable`), but the XRT 2.21 user space could not execute commands on it (mailbox
callback `-22`, TDR timeouts), so that path is covered by `--util-source off` on the DKMS driver
and by unit tests only.

## Development

```sh
python3 -m unittest -v
ruff check . && ruff format --check .
```

To exercise the monitor on real hardware, extract XRT's validate binaries and run the load
generator with the Python build that matches `/opt/xilinx/xrt/python`:

```sh
mkdir /tmp/bins && (cd /tmp/bins && ar x /opt/xilinx/xrt/share/amdxdna/bins/xrt_smi_phx.a)
python3.13 tools/npu_load.py --bins /tmp/bins --seconds 30 --duty 0.5   # or --gap-ms 100 for bursts
```

### xrt-amdxdna 2.21 on kernel 7.3+

The 2.21 DKMS driver initialises `drm_sched_init_args` without `.num_rqs`. Kernels from 7.3 on
reject that (`entity with out-of-bounds priority:2 num_user_rqs:0` in dmesg) inside a workqueue
that holds the driver's `dev_lock`, after which every `open()` of the device blocks forever and
the only way out is a reboot. Add `.num_rqs = DRM_SCHED_PRIORITY_COUNT,` to the initializer in
`driver/amdxdna/aie2_hwctx.c` and rebuild the module. The upstream in-tree driver already sets it.

## License

MIT, see `LICENSE`.
