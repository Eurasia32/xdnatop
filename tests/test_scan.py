import os
import tempfile
import unittest

from xdnatop.collectors import scan_fdinfo

FDINFO = """pos:\t0
drm-driver:\tamdxdna_accel_driver
drm-client-id:\t{cid}
drm-engine-npu:\t{busy} ns
drm-total-memory:\t4 KiB
"""


class FakeProc:
    """Builds a /proc-like tree: fake fd symlinks plus fdinfo files."""

    def __init__(self, root):
        self.root = root

    def add(self, pid, fds, comm="app", fdinfo_mode=0o644):
        os.makedirs(f"{self.root}/{pid}/fd")
        os.makedirs(f"{self.root}/{pid}/fdinfo")
        with open(f"{self.root}/{pid}/comm", "w") as fh:
            fh.write(comm + "\n")
        for fd, (target, cid, busy) in fds.items():
            os.symlink(target, f"{self.root}/{pid}/fd/{fd}")
            if cid is not None:
                path = f"{self.root}/{pid}/fdinfo/{fd}"
                with open(path, "w") as fh:
                    fh.write(FDINFO.format(cid=cid, busy=busy))
                os.chmod(path, fdinfo_mode)


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = FakeProc(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dedup_by_client_id_and_device_filter(self):
        self.proc.add(100, {3: ("/dev/accel/accel0", 7, 1000), 4: ("/dev/accel/accel0", 7, 1000)})
        self.proc.add(200, {3: ("/dev/accel/accel1", 8, 5)})
        self.proc.add(300, {3: ("/dev/null", None, 0)})
        os.makedirs(f"{self.tmp.name}/self")  # non-numeric entries are skipped
        scan = scan_fdinfo(device_index=0, proc_root=self.tmp.name)
        self.assertEqual(list(scan.processes), [100])
        self.assertEqual(scan.processes[100].busy_ns, 1000)  # counted once, not twice
        self.assertEqual(scan.processes[100].command, "app")
        self.assertEqual(scan_fdinfo(proc_root=self.tmp.name).processes.keys(), {100, 200})

    def test_skip_pid_and_unreadable_fdinfo(self):
        self.proc.add(100, {3: ("/dev/accel/accel0", 7, 1)})
        self.proc.add(101, {3: ("/dev/accel/accel0", 9, 1)}, fdinfo_mode=0o000)
        scan = scan_fdinfo(proc_root=self.tmp.name, skip_pid=100)
        self.assertNotIn(100, scan.processes)
        if os.geteuid() != 0:  # root bypasses the mode bits
            self.assertEqual(scan.denied_pids, [101])
            self.assertNotIn(101, scan.processes)

    def test_unlistable_fd_dir_is_counted(self):
        self.proc.add(100, {3: ("/dev/accel/accel0", 7, 1)})
        os.chmod(f"{self.tmp.name}/100/fd", 0o000)
        try:
            scan = scan_fdinfo(proc_root=self.tmp.name)
            if os.geteuid() != 0:
                self.assertEqual(scan.unreadable_fd_dirs, 1)
                self.assertEqual(scan.processes, {})
        finally:
            os.chmod(f"{self.tmp.name}/100/fd", 0o755)

    def test_skip_pids_set(self):
        self.proc.add(100, {3: ("/dev/accel/accel0", 7, 1)})
        self.proc.add(101, {3: ("/dev/accel/accel0", 8, 1)})
        scan = scan_fdinfo(proc_root=self.tmp.name, skip_pids=frozenset({100, 101}))
        self.assertEqual(scan.processes, {})

    def test_missing_proc_root(self):
        scan = scan_fdinfo(proc_root=f"{self.tmp.name}/nope")
        self.assertEqual(scan.processes, {})
