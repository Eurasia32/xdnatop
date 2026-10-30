import unittest

from xdnatop.collectors import parse_fdinfo

SAMPLE = """pos:\t0
flags:\t02100002
mnt_id:\t29
ino:\t792
drm-driver:\tamdxdna_accel_driver
drm-client-id:\t744
drm-pdev:\t0000:66:00.1
drm-engine-npu-amdxdna:\t1500000 ns
drm-total-memory:\t64 KiB
drm-shared-memory:\t0
"""


class FdinfoTest(unittest.TestCase):
    def test_parse_drm_client(self):
        c = parse_fdinfo(SAMPLE)
        self.assertIsNotNone(c)
        self.assertEqual(c.client_id, 744)
        self.assertEqual(c.driver, "amdxdna_accel_driver")
        self.assertEqual(c.pdev, "0000:66:00.1")
        self.assertEqual(c.engines_ns, {"npu-amdxdna": 1500000})
        self.assertEqual(c.busy_ns, 1500000)
        self.assertEqual(c.memory["total-memory"], 64 * 1024)
        self.assertEqual(c.memory["shared-memory"], 0)

    def test_non_drm_fd_is_ignored(self):
        self.assertIsNone(parse_fdinfo("pos:\t0\nflags:\t02\n"))


if __name__ == "__main__":
    unittest.main()
