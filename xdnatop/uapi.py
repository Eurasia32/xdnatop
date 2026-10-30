"""ctypes bindings for the amdxdna DRM accel uapi (include/uapi/drm/amdxdna_accel.h).

Only the read-only query ioctls needed for monitoring are bound. Layouts mirror
both the in-tree kernel header and the xrt-amdxdna DKMS header; where the two
differ, parsing is done by prefix with the element size reported by the kernel.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
from dataclasses import dataclass

DRM_IOCTL_BASE = ord("d")
DRM_COMMAND_BASE = 0x40

DRM_AMDXDNA_GET_INFO = 7
DRM_AMDXDNA_GET_ARRAY = 10

# enum amdxdna_drm_get_param
DRM_AMDXDNA_QUERY_AIE_STATUS = 0
DRM_AMDXDNA_QUERY_AIE_METADATA = 1
DRM_AMDXDNA_QUERY_AIE_VERSION = 2
DRM_AMDXDNA_QUERY_CLOCK_METADATA = 3
DRM_AMDXDNA_QUERY_SENSORS = 4
DRM_AMDXDNA_QUERY_HW_CONTEXTS = 5
DRM_AMDXDNA_QUERY_FIRMWARE_VERSION = 8
DRM_AMDXDNA_GET_POWER_MODE = 9
DRM_AMDXDNA_QUERY_TELEMETRY = 10
DRM_AMDXDNA_QUERY_RESOURCE_INFO = 12

# params for struct amdxdna_drm_get_array
DRM_AMDXDNA_HW_CONTEXT_ALL = 0

AMDXDNA_HWCTX_STATE_IDLE = 0
AMDXDNA_HWCTX_STATE_ACTIVE = 1

POWER_MODE_NAMES = {
    0: "default",
    1: "low",
    2: "medium",
    3: "high",
    4: "turbo",
}

AMDXDNA_SENSOR_TYPE_POWER = 0
AMDXDNA_SENSOR_TYPE_COLUMN_UTILIZATION = 1


def _iowr(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (DRM_IOCTL_BASE << 8) | (DRM_COMMAND_BASE + nr)


class GetInfo(ctypes.Structure):
    _fields_ = [("param", ctypes.c_uint32), ("buffer_size", ctypes.c_uint32), ("buffer", ctypes.c_uint64)]


class GetArray(ctypes.Structure):
    _fields_ = [
        ("param", ctypes.c_uint32),
        ("element_size", ctypes.c_uint32),
        ("num_element", ctypes.c_uint32),
        ("pad", ctypes.c_uint32),
        ("buffer", ctypes.c_uint64),
    ]


class AieVersion(ctypes.Structure):
    _fields_ = [("major", ctypes.c_uint32), ("minor", ctypes.c_uint32)]


class AieTileMetadata(ctypes.Structure):
    _fields_ = [
        ("row_count", ctypes.c_uint16),
        ("row_start", ctypes.c_uint16),
        ("dma_channel_count", ctypes.c_uint16),
        ("lock_count", ctypes.c_uint16),
        ("event_reg_count", ctypes.c_uint16),
        ("pad", ctypes.c_uint16 * 3),
    ]


class AieMetadata(ctypes.Structure):
    _fields_ = [
        ("col_size", ctypes.c_uint32),
        ("cols", ctypes.c_uint16),
        ("rows", ctypes.c_uint16),
        ("version", AieVersion),
        ("core", AieTileMetadata),
        ("mem", AieTileMetadata),
        ("shim", AieTileMetadata),
    ]


class Clock(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * 16), ("freq_mhz", ctypes.c_uint32), ("pad", ctypes.c_uint32)]


class ClockMetadata(ctypes.Structure):
    _fields_ = [("mp_npu_clock", Clock), ("h_clock", Clock)]


class Sensor(ctypes.Structure):
    _fields_ = [
        ("label", ctypes.c_char * 64),
        ("input", ctypes.c_uint32),
        ("max", ctypes.c_uint32),
        ("average", ctypes.c_uint32),
        ("highest", ctypes.c_uint32),
        ("status", ctypes.c_char * 64),
        ("units", ctypes.c_char * 16),
        ("unitm", ctypes.c_int8),
        ("type", ctypes.c_uint8),
        ("pad", ctypes.c_uint8 * 6),
    ]


class FirmwareVersion(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_uint32),
        ("minor", ctypes.c_uint32),
        ("patch", ctypes.c_uint32),
        ("build", ctypes.c_uint32),
    ]


class PowerMode(ctypes.Structure):
    _fields_ = [("power_mode", ctypes.c_uint8), ("pad", ctypes.c_uint8 * 7)]


class ResourceInfo(ctypes.Structure):
    _fields_ = [
        ("npu_clk_max", ctypes.c_uint64),
        ("npu_tops_max", ctypes.c_uint64),
        ("npu_task_max", ctypes.c_uint64),
        ("npu_tops_curr", ctypes.c_uint64),
        ("npu_task_curr", ctypes.c_uint64),
    ]


class QueryHwctx(ctypes.Structure):
    """struct amdxdna_drm_query_hwctx (64 bytes), the pre-GET_ARRAY layout.

    It is a strict prefix of HwctxEntry except that ``hwctx_id`` is ``pad``.
    """

    _fields_ = [
        ("context_id", ctypes.c_uint32),
        ("start_col", ctypes.c_uint32),
        ("num_col", ctypes.c_uint32),
        ("pad", ctypes.c_uint32),
        ("pid", ctypes.c_int64),
        ("command_submissions", ctypes.c_uint64),
        ("command_completions", ctypes.c_uint64),
        ("migrations", ctypes.c_uint64),
        ("preemptions", ctypes.c_uint64),
        ("errors", ctypes.c_uint64),
    ]


class HwctxEntry(ctypes.Structure):
    """struct amdxdna_drm_hwctx_entry (in-tree layout, 144 bytes).

    The DKMS header lacks the trailing ``pad`` but has identical alignment, so
    the two layouts are byte-compatible.
    """

    _fields_ = [
        ("context_id", ctypes.c_uint32),
        ("start_col", ctypes.c_uint32),
        ("num_col", ctypes.c_uint32),
        ("hwctx_id", ctypes.c_uint32),
        ("pid", ctypes.c_int64),
        ("command_submissions", ctypes.c_uint64),
        ("command_completions", ctypes.c_uint64),
        ("migrations", ctypes.c_uint64),
        ("preemptions", ctypes.c_uint64),
        ("errors", ctypes.c_uint64),
        ("priority", ctypes.c_uint64),
        ("heap_usage", ctypes.c_uint64),
        ("suspensions", ctypes.c_uint64),
        ("state", ctypes.c_uint32),
        ("pasid", ctypes.c_uint32),
        ("gops", ctypes.c_uint32),
        ("fps", ctypes.c_uint32),
        ("dma_bandwidth", ctypes.c_uint32),
        ("latency", ctypes.c_uint32),
        ("frame_exec_time", ctypes.c_uint32),
        ("txn_op_idx", ctypes.c_uint32),
        ("ctx_pc", ctypes.c_uint32),
        ("fatal_error_type", ctypes.c_uint32),
        ("fatal_error_exception_type", ctypes.c_uint32),
        ("fatal_error_exception_pc", ctypes.c_uint32),
        ("fatal_error_app_module", ctypes.c_uint32),
        ("pad", ctypes.c_uint32),
    ]


IOCTL_GET_INFO = _iowr(DRM_AMDXDNA_GET_INFO, ctypes.sizeof(GetInfo))
IOCTL_GET_ARRAY = _iowr(DRM_AMDXDNA_GET_ARRAY, ctypes.sizeof(GetArray))


@dataclass(frozen=True)
class HwContext:
    context_id: int
    hwctx_id: int
    pid: int
    start_col: int
    num_col: int
    submissions: int
    completions: int
    migrations: int
    preemptions: int
    errors: int
    priority: int
    heap_usage: int
    suspensions: int
    state: int
    gops: int
    fps: int
    latency: int
    fatal_error_type: int

    @property
    def active(self) -> bool:
        return self.state == AMDXDNA_HWCTX_STATE_ACTIVE


def decode_hwctx_entries(buf: bytes, element_size: int, count: int) -> list[HwContext]:
    """Decode a GET_ARRAY HW_CONTEXT_ALL buffer.

    ``element_size`` is what the kernel reported. Fields beyond it are zero so
    older kernels that only know the shorter ``amdxdna_drm_query_hwctx`` layout
    (no state/gops/...) still decode.
    """
    entries: list[HwContext] = []
    full = ctypes.sizeof(HwctxEntry)
    for i in range(count):
        chunk = bytes(buf[i * element_size : (i + 1) * element_size])
        chunk = chunk[:full].ljust(full, b"\0")
        e = HwctxEntry.from_buffer_copy(chunk)
        entries.append(
            HwContext(
                context_id=e.context_id,
                hwctx_id=e.hwctx_id,
                pid=e.pid,
                start_col=e.start_col,
                num_col=e.num_col,
                submissions=e.command_submissions,
                completions=e.command_completions,
                migrations=e.migrations,
                preemptions=e.preemptions,
                errors=e.errors,
                priority=e.priority,
                heap_usage=e.heap_usage,
                suspensions=e.suspensions,
                state=e.state,
                gops=e.gops,
                fps=e.fps,
                latency=e.latency,
                fatal_error_type=e.fatal_error_type,
            )
        )
    return entries


class AmdxdnaDevice:
    """Thin wrapper over one /dev/accel/accelN file descriptor."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> AmdxdnaDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_info(self, param: int, out: ctypes.Structure | ctypes.Array) -> int:
        """Run DRM_IOCTL_AMDXDNA_GET_INFO; returns bytes written by the kernel."""
        args = GetInfo(param=param, buffer_size=ctypes.sizeof(out), buffer=ctypes.addressof(out))
        fcntl.ioctl(self.fd, IOCTL_GET_INFO, args)
        return args.buffer_size

    def get_array(self, param: int, element_size: int, max_elements: int) -> tuple[bytes, int, int]:
        """Run DRM_IOCTL_AMDXDNA_GET_ARRAY; returns (buffer, element_size, count)."""
        buf = ctypes.create_string_buffer(element_size * max_elements)
        args = GetArray(
            param=param,
            element_size=element_size,
            num_element=max_elements,
            pad=0,
            buffer=ctypes.addressof(buf),
        )
        fcntl.ioctl(self.fd, IOCTL_GET_ARRAY, args)
        return buf.raw, args.element_size, args.num_element

    def hw_contexts(self, max_elements: int = 64) -> list[HwContext]:
        """List all hardware contexts on the device.

        Uses GET_ARRAY(HW_CONTEXT_ALL) (in-tree since 6.16, all DKMS builds) and
        falls back to GET_INFO(QUERY_HW_CONTEXTS) with the older 64-byte
        ``amdxdna_drm_query_hwctx`` layout on 6.14/6.15 kernels.
        """
        esz = ctypes.sizeof(HwctxEntry)
        try:
            buf, esz_out, count = self.get_array(DRM_AMDXDNA_HW_CONTEXT_ALL, esz, max_elements)
            if count > max_elements:
                # Kernel reported more contexts than our buffer holds; retry sized to fit.
                max_elements = count
                buf, esz_out, count = self.get_array(DRM_AMDXDNA_HW_CONTEXT_ALL, esz, max_elements)
            return decode_hwctx_entries(buf, esz_out or esz, min(count, max_elements))
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTTY, errno.EOPNOTSUPP):
                raise
        legacy = ctypes.sizeof(QueryHwctx)
        arr = (QueryHwctx * max_elements)()
        written = self.get_info(DRM_AMDXDNA_QUERY_HW_CONTEXTS, arr)
        return decode_hwctx_entries(bytes(arr), legacy, min(max_elements, written // legacy))

    def clocks(self) -> dict[str, int]:
        md = ClockMetadata()
        self.get_info(DRM_AMDXDNA_QUERY_CLOCK_METADATA, md)
        return {clk.name.decode(errors="replace"): clk.freq_mhz for clk in (md.mp_npu_clock, md.h_clock) if clk.name}

    def power_mode(self) -> str:
        pm = PowerMode()
        self.get_info(DRM_AMDXDNA_GET_POWER_MODE, pm)
        return POWER_MODE_NAMES.get(pm.power_mode, f"unknown({pm.power_mode})")

    def firmware_version(self) -> str:
        fw = FirmwareVersion()
        self.get_info(DRM_AMDXDNA_QUERY_FIRMWARE_VERSION, fw)
        return f"{fw.major}.{fw.minor}.{fw.patch}.{fw.build}"

    def aie_metadata(self) -> AieMetadata:
        md = AieMetadata()
        self.get_info(DRM_AMDXDNA_QUERY_AIE_METADATA, md)
        return md

    def resource_info(self) -> ResourceInfo:
        ri = ResourceInfo()
        self.get_info(DRM_AMDXDNA_QUERY_RESOURCE_INFO, ri)
        return ri

    def sensors(self, max_sensors: int = 32) -> list[Sensor]:
        arr = (Sensor * max_sensors)()
        written = self.get_info(DRM_AMDXDNA_QUERY_SENSORS, arr)
        n = min(max_sensors, written // ctypes.sizeof(Sensor))
        return [s for s in arr[:n] if s.label]
