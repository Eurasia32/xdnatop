"""IoctlCollector protocol tests with fake workers (no hardware)."""

import os
import struct
import time
import unittest

from xdnatop.collectors import MAX_HELPER_MSG, DeviceStatic, IoctlCollector, IoctlSample, _frame, _write_all


def dev():
    return DeviceStatic(index=0, dev_path="/dev/accel/accel0", sysfs_path="/nonexistent")


def good_worker(rfd, wfd, _path):
    _write_all(wfd, _frame(("static", {"columns": 5, "fw_version": "1.2.3.4", "bogus_attr": 1})))
    while os.read(rfd, 1) == b"s":
        _write_all(wfd, _frame(("sample", IoctlSample(power_mode="high"))))


def open_error_worker(rfd, wfd, path):
    _write_all(wfd, _frame(("open_error", f"{path}: No such device")))


def hanging_worker(rfd, wfd, _path):
    time.sleep(30)  # simulates open() stuck in D state


def hang_after_handshake_worker(rfd, wfd, _path):
    _write_all(wfd, _frame(("static", {})))
    os.read(rfd, 1)
    time.sleep(30)


def garbage_worker(rfd, wfd, _path):
    _write_all(wfd, struct.pack("<I", 10) + b"not-pickle")


def oversized_worker(rfd, wfd, _path):
    _write_all(wfd, struct.pack("<I", MAX_HELPER_MSG + 1))


def wrong_shape_worker(rfd, wfd, _path):
    _write_all(wfd, struct.pack("<I", 0) + b"")  # zero-length body: pickle.loads(b"") fails


def wrong_kind_worker(rfd, wfd, _path):
    _write_all(wfd, _frame(("static", {})))
    os.read(rfd, 1)
    _write_all(wfd, _frame(("weird", 42)))


def dying_worker(rfd, wfd, _path):
    os._exit(3)


class HelperTest(unittest.TestCase):
    def collector(self, worker, timeout=1.0):
        c = IoctlCollector(dev(), timeout=timeout, worker=worker)
        self.addCleanup(c.close)
        return c

    def test_handshake_and_sample(self):
        d = dev()
        c = IoctlCollector(d, timeout=2.0, worker=good_worker)
        self.addCleanup(c.close)
        self.assertTrue(c.available)
        self.assertEqual((d.columns, d.fw_version), (5, "1.2.3.4"))
        self.assertFalse(hasattr(d, "bogus_attr"))
        s = c.sample(have_clients=True)
        self.assertEqual((s.power_mode, s.error, s.skipped), ("high", "", False))
        self.assertTrue(c.sample(have_clients=False).skipped)
        c.close()
        self.assertFalse(c.available)

    def test_open_error(self):
        c = self.collector(open_error_worker)
        self.assertFalse(c.available)
        self.assertIn("No such device", c.error)
        self.assertIn("No such device", c.sample().error)

    def test_timeout_during_open_abandons_helper(self):
        t0 = time.monotonic()
        c = self.collector(hanging_worker, timeout=0.3)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(c.hung)
        self.assertFalse(c.available)
        self.assertIn("did not return", c.sample().error)

    def test_timeout_during_sample(self):
        c = self.collector(hang_after_handshake_worker, timeout=0.3)
        self.assertTrue(c.available)
        s = c.sample(have_clients=True)
        self.assertIn("did not return", s.error)
        self.assertTrue(c.hung)

    def test_corrupt_and_oversized_and_wrong_messages(self):
        for worker, needle in (
            (garbage_worker, "corrupt"),
            (oversized_worker, "exceeds limit"),
            (wrong_shape_worker, "corrupt"),
            (dying_worker, "exited"),
        ):
            with self.subTest(worker=worker.__name__):
                c = self.collector(worker)
                self.assertFalse(c.available)
                self.assertIn(needle, c.error)

    def test_wrong_reply_kind(self):
        c = self.collector(wrong_kind_worker)
        self.assertTrue(c.available)
        self.assertIn("unexpected reply", c.sample().error)
        self.assertFalse(c.available)

    def test_disabled(self):
        c = IoctlCollector(dev(), enabled=False)
        self.assertFalse(c.available)
        self.assertIn("disabled", c.sample().error)
