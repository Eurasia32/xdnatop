import ctypes
import unittest

from xdnatop import uapi


class UapiTest(unittest.TestCase):
    def test_struct_sizes_match_header(self):
        self.assertEqual(ctypes.sizeof(uapi.GetInfo), 16)
        self.assertEqual(ctypes.sizeof(uapi.GetArray), 24)
        self.assertEqual(ctypes.sizeof(uapi.HwctxEntry), 144)
        self.assertEqual(ctypes.sizeof(uapi.Sensor), 168)
        self.assertEqual(ctypes.sizeof(uapi.ClockMetadata), 48)
        self.assertEqual(ctypes.sizeof(uapi.AieMetadata), 64)
        self.assertEqual(ctypes.sizeof(uapi.ResourceInfo), 40)

    def test_ioctl_numbers(self):
        # _IOWR('d', 0x40 + nr, size)
        self.assertEqual(uapi.IOCTL_GET_INFO, 0xC0106447)
        self.assertEqual(uapi.IOCTL_GET_ARRAY, 0xC018644A)

    def test_decode_hwctx_entries_full_and_prefix(self):
        e = uapi.HwctxEntry(
            context_id=3,
            start_col=1,
            num_col=4,
            hwctx_id=7,
            pid=1234,
            command_submissions=10,
            command_completions=9,
            errors=1,
            state=uapi.AMDXDNA_HWCTX_STATE_ACTIVE,
            gops=5,
        )
        raw = bytes(e)
        full = uapi.decode_hwctx_entries(raw * 2, 144, 2)
        self.assertEqual(len(full), 2)
        self.assertEqual(full[1].pid, 1234)
        self.assertEqual(full[1].num_col, 4)
        self.assertTrue(full[1].active)
        self.assertEqual(full[1].gops, 5)
        # Older kernels report the 64-byte amdxdna_drm_query_hwctx prefix.
        short = uapi.decode_hwctx_entries(raw[:64], 64, 1)
        self.assertEqual(short[0].submissions, 10)
        self.assertEqual(short[0].state, 0)


if __name__ == "__main__":
    unittest.main()


class LegacyLayoutTest(unittest.TestCase):
    def test_query_hwctx_is_prefix_of_entry(self):
        self.assertEqual(ctypes.sizeof(uapi.QueryHwctx), 64)
        q = uapi.QueryHwctx(context_id=1, start_col=0, num_col=4, pid=42, command_completions=9)
        [e] = uapi.decode_hwctx_entries(bytes(q), 64, 1)
        self.assertEqual((e.context_id, e.pid, e.num_col, e.completions, e.state), (1, 42, 4, 9, 0))
