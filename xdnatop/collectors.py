"""Data collectors: sysfs, DRM fdinfo, and amdxdna ioctls.

No curses or presentation logic here so everything is unit-testable.
"""

from __future__ import annotations

import contextlib
import errno
import os
import pickle
import pwd
import re
import select
import signal
import struct
import time
from dataclasses import dataclass, field

from . import uapi

ACCEL_CLASS = "/sys/class/accel"
ACCEL_DEV_RE = re.compile(r"^/dev/accel/accel(\d+)$")
FDINFO_ENGINE_RE = re.compile(r"^drm-engine-(?P<name>[\w-]+)$")
FDINFO_MEM_RE = re.compile(r"^drm-(?P<kind>total|shared|resident|purgeable|active)-(?P<region>[\w-]+)$")
_MEM_UNITS = {"": 1, "B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}


def read_text(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return default


def read_int(path: str, default: int = 0) -> int:
    text = read_text(path)
    try:
        return int(text)
    except ValueError:
        return default


# --------------------------------------------------------------------------- sysfs


@dataclass
class DeviceStatic:
    index: int
    dev_path: str
    sysfs_path: str
    name: str = ""
    pci_bdf: str = ""
    driver: str = ""
    fw_version: str = ""
    aie_version: str = ""
    columns: int = 0
    rows: int = 0
    power_mode: str = ""
    clocks: dict[str, int] = field(default_factory=dict)
    tops_max: int = 0
    clk_max_mhz: int = 0


@dataclass
class RuntimePm:
    status: str = ""
    active_ms: int = 0
    suspended_ms: int = 0
    usage: int = 0
    autosuspend_delay_ms: int = 0


def list_accel_devices() -> list[DeviceStatic]:
    """Enumerate /sys/class/accel/accelN that are bound to amdxdna."""
    devices: list[DeviceStatic] = []
    try:
        names = sorted(os.listdir(ACCEL_CLASS))
    except OSError:
        return devices
    for name in names:
        m = re.match(r"accel(\d+)$", name)
        if not m:
            continue
        sysfs = os.path.join(ACCEL_CLASS, name, "device")
        driver = os.path.basename(os.path.realpath(os.path.join(sysfs, "driver")))
        if not os.path.exists(os.path.join(sysfs, "driver")):
            # Driver link disappears while an unbind is in progress (or stuck);
            # the amdxdna-specific vbnv attribute still identifies the device.
            driver = "amdxdna (unbinding)" if os.path.exists(os.path.join(sysfs, "vbnv")) else ""
        if not driver.startswith("amdxdna"):
            continue
        dev = DeviceStatic(index=int(m.group(1)), dev_path=f"/dev/accel/{name}", sysfs_path=sysfs)
        dev.driver = driver
        dev.name = read_text(os.path.join(sysfs, "vbnv"), "AMD NPU")
        dev.fw_version = read_text(os.path.join(sysfs, "fw_version"))
        dev.pci_bdf = os.path.basename(os.path.realpath(sysfs))
        devices.append(dev)
    return devices


def read_runtime_pm(sysfs_path: str) -> RuntimePm:
    power = os.path.join(sysfs_path, "power")
    return RuntimePm(
        status=read_text(os.path.join(power, "runtime_status"), "unknown"),
        active_ms=read_int(os.path.join(power, "runtime_active_time")),
        suspended_ms=read_int(os.path.join(power, "runtime_suspended_time")),
        usage=read_int(os.path.join(power, "runtime_usage")),
        autosuspend_delay_ms=read_int(os.path.join(power, "autosuspend_delay_ms")),
    )


# --------------------------------------------------------------------------- fdinfo


@dataclass
class FdinfoClient:
    client_id: int
    driver: str = ""
    pdev: str = ""
    engines_ns: dict[str, int] = field(default_factory=dict)
    memory: dict[str, int] = field(default_factory=dict)  # e.g. {"total-memory": bytes}

    @property
    def busy_ns(self) -> int:
        return sum(self.engines_ns.values())


def _parse_mem(value: str) -> int:
    parts = value.split()
    if not parts:
        return 0
    try:
        num = int(parts[0])
    except ValueError:
        return 0
    unit = parts[1] if len(parts) > 1 else ""
    return num * _MEM_UNITS.get(unit, 1)


def parse_fdinfo(text: str) -> FdinfoClient | None:
    """Parse one /proc/<pid>/fdinfo/<fd>; returns None if it is not a DRM fd."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    if "drm-client-id" not in fields:
        return None
    try:
        client = FdinfoClient(client_id=int(fields["drm-client-id"]))
    except ValueError:
        return None
    client.driver = fields.get("drm-driver", "")
    client.pdev = fields.get("drm-pdev", "")
    for key, value in fields.items():
        em = FDINFO_ENGINE_RE.match(key)
        if em:
            with contextlib.suppress(ValueError, IndexError):
                client.engines_ns[em.group("name")] = int(value.split()[0])
            continue
        mm = FDINFO_MEM_RE.match(key)
        if mm:
            client.memory[f"{mm.group('kind')}-{mm.group('region')}"] = _parse_mem(value)
    return client


@dataclass
class ProcessUsage:
    pid: int
    clients: dict[int, FdinfoClient] = field(default_factory=dict)
    command: str = ""
    user: str = ""

    @property
    def busy_ns(self) -> int:
        return sum(c.busy_ns for c in self.clients.values())

    @property
    def has_engine_stats(self) -> bool:
        """True if the driver exports drm-engine-* busy time for this process."""
        return any(c.engines_ns for c in self.clients.values())

    @property
    def memory_bytes(self) -> int:
        return sum(c.memory.get("total-memory", 0) for c in self.clients.values())


@dataclass
class FdinfoScan:
    processes: dict[int, ProcessUsage] = field(default_factory=dict)
    denied_pids: list[int] = field(default_factory=list)  # had an accel fd, fdinfo unreadable
    unreadable_fd_dirs: int = 0  # /proc/<pid>/fd itself not listable (other users, no root)


PROC_ROOT = "/proc"


def proc_command(pid: int, proc_root: str = PROC_ROOT) -> str:
    for path, limit in ((f"{proc_root}/{pid}/cmdline", 512), (f"{proc_root}/{pid}/comm", 64)):
        try:
            with open(path, "rb") as fh:
                data = fh.read(limit)
        except OSError:
            continue
        data = data.rstrip(b"\0").replace(b"\0", b" ").strip()
        if data:
            return data.decode(errors="replace")
    return ""


def proc_user(pid: int, proc_root: str = PROC_ROOT) -> str:
    try:
        uid = os.stat(f"{proc_root}/{pid}").st_uid
    except OSError:
        return ""
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def scan_fdinfo(
    device_index: int | None = None,
    skip_pid: int | None = None,
    proc_root: str = PROC_ROOT,
    skip_pids: frozenset[int] = frozenset(),
) -> FdinfoScan:
    """Find every process holding /dev/accel/accelN open and read its DRM fdinfo.

    Clients are de-duplicated by drm-client-id so dup()'d or inherited fds are
    counted once. Processes whose fdinfo cannot be read (EACCES/EPERM) are listed
    in ``denied_pids`` rather than silently dropped.
    """
    scan = FdinfoScan()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return scan
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == skip_pid or pid in skip_pids:
            continue
        fd_dir = f"{proc_root}/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except PermissionError:
            scan.unreadable_fd_dirs += 1
            continue
        except OSError:
            continue
        usage: ProcessUsage | None = None
        denied = False
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            m = ACCEL_DEV_RE.match(target)
            if not m or (device_index is not None and int(m.group(1)) != device_index):
                continue
            try:
                with open(f"{proc_root}/{pid}/fdinfo/{fd}", encoding="utf-8") as fh:
                    client = parse_fdinfo(fh.read())
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM):
                    denied = True
                continue
            if client is None:
                continue
            if usage is None:
                usage = ProcessUsage(pid=pid)
            usage.clients.setdefault(client.client_id, client)
        if usage is not None:
            usage.command = proc_command(pid, proc_root)
            usage.user = proc_user(pid, proc_root)
            scan.processes[pid] = usage
        elif denied:
            scan.denied_pids.append(pid)
    return scan


# --------------------------------------------------------------------------- ioctl


@dataclass
class IoctlSample:
    contexts: list[uapi.HwContext] = field(default_factory=list)
    power_mode: str = ""
    clocks: dict[str, int] = field(default_factory=dict)  # live DPM clocks, change with load
    tops_curr: int = 0
    error: str = ""
    skipped: bool = False  # no client had the device open, query not issued


# Largest helper message we accept: 64 contexts * 144 B plus pickle overhead is
# well under this; anything bigger means a corrupted stream.
MAX_HELPER_MSG = 1 << 20


def _frame(obj: object) -> bytes:
    data = pickle.dumps(obj)
    if len(data) > MAX_HELPER_MSG:
        raise ValueError(f"helper message too large: {len(data)} bytes")
    return struct.pack("<I", len(data)) + data


def _write_all(fd: int, data: bytes) -> None:
    """os.write on a blocking pipe may return short if interrupted by a signal."""
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _ioctl_worker(rfd: int, wfd: int, dev_path: str) -> None:  # pragma: no cover - runs in child
    """Child process: owns the device fd and answers requests from the parent.

    Every amdxdna GET_INFO/GET_ARRAY resumes the device and takes the driver's
    dev_lock. If the firmware wedges, those calls (and even open()) block in
    uninterruptible sleep. Isolating them here lets the parent time out and
    keep running on fdinfo + sysfs alone.
    """

    def send(obj: object) -> None:
        _write_all(wfd, _frame(obj))

    try:
        dev = uapi.AmdxdnaDevice(dev_path)
    except OSError as exc:
        send(("open_error", f"{dev_path}: {exc.strerror}"))
        return
    static: dict[str, object] = {}
    for key, fn in (
        ("fw_version", dev.firmware_version),
        ("aie", dev.aie_metadata),
        ("resource", dev.resource_info),
        ("power_mode", dev.power_mode),
        ("clocks", dev.clocks),
    ):
        try:
            v = fn()
        except OSError:
            continue
        if key == "aie":
            static.update(columns=v.cols, rows=v.rows, aie_version=f"{v.version.major}.{v.version.minor}")
        elif key == "resource":
            static.update(tops_max=v.npu_tops_max, clk_max_mhz=v.npu_clk_max)
        else:
            static[key] = v
    send(("static", static))
    while True:
        try:
            req = os.read(rfd, 1)
        except OSError:
            break
        if req != b"s":
            break
        out = IoctlSample()
        try:
            out.contexts = dev.hw_contexts()
        except OSError as exc:
            out.error = f"GET_ARRAY: {exc.strerror}"
        with contextlib.suppress(OSError):
            out.power_mode = dev.power_mode()
        with contextlib.suppress(OSError):
            out.clocks = dev.clocks()
        send(("sample", out))
    dev.close()


class IoctlCollector:
    """Runs the amdxdna ioctl queries in a forked helper with a timeout.

    Plain os.fork + pipes rather than multiprocessing: if the helper gets stuck
    in the kernel we must be able to abandon it, and multiprocessing's atexit
    handler would join it forever.
    """

    def __init__(self, dev: DeviceStatic, timeout: float = 3.0, enabled: bool = True, worker=None) -> None:
        self.static = dev
        self.timeout = timeout
        self._worker = worker or _ioctl_worker  # injectable for tests
        self.error = ""
        self.hung = False
        self._pid = 0
        self._rfd = -1
        self._wfd = -1
        if not enabled:
            self.error = "ioctl queries disabled (--no-ioctl)"
            return
        req_r, req_w = os.pipe()
        rsp_r, rsp_w = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            os.close(req_w)
            os.close(rsp_r)
            code = 0
            try:
                self._worker(req_r, rsp_w, dev.dev_path)
            except BaseException:  # noqa: BLE001 - never unwind into the parent's atexit handlers
                code = 1
            finally:
                os._exit(code)
        os.close(req_r)
        os.close(rsp_w)
        self._pid, self._rfd, self._wfd = pid, rsp_r, req_w
        msg = self._recv()
        if msg is None:
            return
        kind, payload = msg
        if kind == "open_error":
            self.error = str(payload)
            self._shutdown()
            return
        if kind != "static" or not isinstance(payload, dict):
            self.error = f"ioctl helper handshake failed ({kind})"
            self._shutdown()
            return
        for key, value in payload.items():
            if hasattr(dev, key):
                setattr(dev, key, value)

    def _read_exact(self, n: int, deadline: float) -> bytes | None:
        buf = b""
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self._rfd], [], [], remaining)[0]:
                return None
            chunk = os.read(self._rfd, n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def _recv(self):
        if self._rfd < 0:
            return None
        deadline = time.monotonic() + self.timeout
        try:
            header = self._read_exact(4, deadline)
            body = None
            if header:
                length = struct.unpack("<I", header)[0]
                if length > MAX_HELPER_MSG:
                    raise ValueError(f"helper message length {length} exceeds limit")
                body = self._read_exact(length, deadline)
        except (EOFError, OSError, ValueError) as exc:
            self.error = f"ioctl helper exited ({exc.__class__.__name__}: {exc})"
            self._shutdown()
            return None
        if body is None:
            self.hung = True
            self.error = f"amdxdna ioctl did not return within {self.timeout:.0f}s (driver dev_lock held? see dmesg)"
            self._abandon()
            return None
        try:
            msg = pickle.loads(body)
        except Exception as exc:  # noqa: BLE001 - any unpickling failure means a corrupt stream
            self.error = f"ioctl helper sent a corrupt message ({exc.__class__.__name__})"
            self._shutdown()
            return None
        if not (isinstance(msg, tuple) and len(msg) == 2 and isinstance(msg[0], str)):
            self.error = "ioctl helper sent an unexpected message"
            self._shutdown()
            return None
        return msg

    @property
    def available(self) -> bool:
        return self._rfd >= 0

    @property
    def helper_pid(self) -> int:
        """PID of the forked helper (0 if none). It holds the device open and must be excluded from scans."""
        return self._pid

    def sample(self, have_clients: bool = True) -> IoctlSample:
        out = IoctlSample()
        if self._rfd < 0:
            out.error = self.error
            return out
        if not have_clients:
            # Nothing has the device open, so there are no hw contexts to list;
            # skip the query so the monitor does not keep the NPU out of runtime suspend.
            out.skipped = True
            out.power_mode = str(self.static.power_mode)
            out.clocks = dict(self.static.clocks)
            return out
        try:
            os.write(self._wfd, b"s")
        except OSError as exc:
            self.error = f"ioctl helper: {exc.strerror}"
            self._shutdown()
            out.error = self.error
            return out
        msg = self._recv()
        if msg is None:
            out.error = self.error
            return out
        if msg[0] != "sample" or not isinstance(msg[1], IoctlSample):
            self.error = f"ioctl helper sent an unexpected reply ({msg[0]})"
            self._shutdown()
            out.error = self.error
            return out
        return msg[1]

    def _close_fds(self) -> None:
        for fd in (self._rfd, self._wfd):
            if fd >= 0:
                os.close(fd)
        self._rfd = self._wfd = -1

    def _abandon(self) -> None:
        # The child is most likely blocked in the kernel; SIGKILL is harmless there
        # (it is delivered once the driver releases it) and reaps it immediately if
        # it was merely slow. Do not wait on it either way.
        self._close_fds()
        if self._pid:
            with contextlib.suppress(OSError):
                os.kill(self._pid, signal.SIGKILL)
                os.waitpid(self._pid, os.WNOHANG)
        self._pid = 0

    def _shutdown(self) -> None:
        self._close_fds()  # EOF on the request pipe makes the child exit
        if self._pid:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                pid, _ = os.waitpid(self._pid, os.WNOHANG)
                if pid:
                    break
                time.sleep(0.02)
            else:
                try:
                    os.kill(self._pid, 9)
                    os.waitpid(self._pid, os.WNOHANG)
                except OSError:
                    pass
            self._pid = 0

    def close(self) -> None:
        self._shutdown()
