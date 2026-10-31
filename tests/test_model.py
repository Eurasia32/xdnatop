import unittest

from xdnatop.collectors import FdinfoClient, FdinfoScan, IoctlSample, ProcessUsage, RuntimePm
from xdnatop.model import Sample, derive
from xdnatop.uapi import AMDXDNA_HWCTX_STATE_ACTIVE, HwContext


def ctx(pid, num_col=2, active=True):
    return HwContext(
        context_id=0,
        hwctx_id=0,
        pid=pid,
        start_col=0,
        num_col=num_col,
        submissions=0,
        completions=0,
        migrations=0,
        preemptions=0,
        errors=0,
        priority=0,
        heap_usage=0,
        suspensions=0,
        state=AMDXDNA_HWCTX_STATE_ACTIVE if active else 0,
        gops=0,
        fps=0,
        latency=0,
        fatal_error_type=0,
    )


def sample(ts, busy, contexts=(), pm=("active", 0, 0)):
    scan = FdinfoScan()
    for pid, ns in busy.items():
        scan.processes[pid] = ProcessUsage(
            pid=pid, command="app", clients={pid: FdinfoClient(client_id=pid, engines_ns={"npu-amdxdna": ns})}
        )
    return Sample(
        ts=ts,
        pm=RuntimePm(status=pm[0], active_ms=pm[1], suspended_ms=pm[2]),
        fdinfo=scan,
        ioctl=IoctlSample(contexts=list(contexts)),
    )


class DeriveTest(unittest.TestCase):
    def test_utilization_from_busy_delta(self):
        prev = sample(0.0, {10: 0, 11: 0})
        derive(prev, None, columns=5)
        cur = sample(2.0, {10: 1_000_000_000, 11: 500_000_000}, contexts=[ctx(10, 3)])
        derive(cur, prev, columns=5)
        self.assertAlmostEqual(cur.util, 75.0)
        self.assertEqual([r.pid for r in cur.processes], [10, 11])
        self.assertAlmostEqual(cur.processes[0].util, 50.0)
        self.assertAlmostEqual(cur.processes[1].util, 25.0)
        self.assertEqual(cur.processes[0].columns, 3)
        self.assertAlmostEqual(cur.column_util, 60.0)

    def test_new_process_has_no_delta_and_counter_reset_is_zero(self):
        prev = sample(0.0, {10: 5_000})
        derive(prev, None, columns=5)
        cur = sample(1.0, {10: 1_000, 12: 900_000_000})
        derive(cur, prev, columns=5)
        self.assertEqual(cur.util, 0.0)
        self.assertEqual({r.pid: r.util for r in cur.processes}, {10: 0.0, 12: 0.0})

    def test_pm_active_share(self):
        prev = sample(0.0, {}, pm=("active", 100, 100))
        derive(prev, None, columns=5)
        cur = sample(1.0, {}, pm=("suspended", 175, 125))
        derive(cur, prev, columns=5)
        self.assertAlmostEqual(cur.pm_active, 75.0)
        self.assertEqual(cur.util, 0.0)

    def test_contexts_without_fdinfo_still_listed(self):
        cur = sample(1.0, {}, contexts=[ctx(99)])
        derive(cur, None, columns=5)
        self.assertEqual(cur.processes[0].pid, 99)
        self.assertEqual(cur.processes[0].command, "")


if __name__ == "__main__":
    unittest.main()


class FallbackTest(unittest.TestCase):
    def test_pm_derived_util_when_driver_has_no_engine_stats(self):
        from xdnatop.model import UTIL_PM

        prev = sample(0.0, {}, pm=("active", 100, 100))
        prev.fdinfo.processes[10] = ProcessUsage(pid=10, command="app", clients={10: FdinfoClient(client_id=10)})
        derive(prev, None, columns=5, fdinfo_busy=False)
        cur = sample(1.0, {}, pm=("active", 160, 140))
        cur.fdinfo.processes[10] = ProcessUsage(pid=10, command="app", clients={10: FdinfoClient(client_id=10)})
        derive(cur, prev, columns=5, fdinfo_busy=False)
        self.assertEqual(cur.util_source, UTIL_PM)
        self.assertAlmostEqual(cur.util, 60.0)
        self.assertFalse(cur.processes[0].has_util)

    def test_fdinfo_busy_autodetected_from_engine_keys(self):
        from xdnatop.model import UTIL_FDINFO

        cur = sample(1.0, {10: 5})
        derive(cur, None, columns=5)
        self.assertEqual(cur.util_source, UTIL_FDINFO)

    def test_commands_per_second(self):
        def c(pid, cid, compl):
            x = ctx(pid)
            return HwContext(**{**x.__dict__, "context_id": cid, "completions": compl})

        prev = sample(0.0, {10: 0}, contexts=[c(10, 0, 100), c(10, 1, 50), c(20, 0, 7)])
        derive(prev, None, columns=5)
        cur = sample(2.0, {10: 0}, contexts=[c(10, 0, 140), c(10, 1, 70), c(20, 0, 3)])
        derive(cur, prev, columns=5)
        by_pid = {r.pid: r for r in cur.processes}
        self.assertAlmostEqual(by_pid[10].cmds_per_s, 30.0)
        self.assertAlmostEqual(by_pid[20].cmds_per_s, 0.0)  # counter went backwards: ignored
        self.assertAlmostEqual(cur.cmds_per_s, 30.0)


class ClientIdentityTest(unittest.TestCase):
    def multi(self, ts, clients, pid=10):
        s = sample(ts, {})
        s.fdinfo.processes[pid] = ProcessUsage(
            pid=pid,
            command="app",
            clients={cid: FdinfoClient(client_id=cid, engines_ns={"npu": ns}) for cid, ns in clients.items()},
        )
        return s

    def test_client_restart_keeps_other_clients_delta(self):
        prev = self.multi(0.0, {1: 1_000_000_000, 2: 1_000_000_000})
        derive(prev, None, columns=5)
        # client 2 closed and reopened as client 3 with a fresh counter; client 1 kept working
        cur = self.multi(1.0, {1: 1_500_000_000, 3: 100_000_000})
        derive(cur, prev, columns=5)
        self.assertAlmostEqual(cur.processes[0].util, 50.0)
        self.assertAlmostEqual(cur.util, 50.0)

    def test_pid_reuse_does_not_inherit_counter(self):
        prev = self.multi(0.0, {1: 5_000_000_000})
        derive(prev, None, columns=5)
        cur = self.multi(1.0, {9: 900_000_000})  # new process, same pid, new client id
        derive(cur, prev, columns=5)
        self.assertEqual(cur.processes[0].util, 0.0)

    def test_context_identity_survives_hwctx_slot_change(self):
        def c(cid, hw, compl):
            x = ctx(10)
            return HwContext(**{**x.__dict__, "context_id": cid, "hwctx_id": hw, "completions": compl})

        prev = sample(0.0, {10: 0}, contexts=[c(0, 0, 100)])
        derive(prev, None, columns=5)
        cur = sample(1.0, {10: 0}, contexts=[c(0, 5, 300)])  # connected to slot 5 since last sample
        derive(cur, prev, columns=5)
        self.assertEqual(cur.cmds_per_s, 200.0)
        recreated = sample(2.0, {10: 0}, contexts=[c(0, 5, 20)])  # same ids, counter restarted
        derive(recreated, cur, columns=5)
        self.assertEqual(recreated.cmds_per_s, 0.0)


class ForcedSourceTest(unittest.TestCase):
    def test_forced_pm_ignores_engine_stats(self):
        from xdnatop.model import UTIL_PM

        prev = sample(0.0, {10: 0}, pm=("active", 100, 100))
        derive(prev, None, columns=5, fdinfo_busy=False)
        cur = sample(1.0, {10: 900_000_000}, pm=("active", 130, 170))
        derive(cur, prev, columns=5, fdinfo_busy=False)
        self.assertEqual(cur.util_source, UTIL_PM)
        self.assertAlmostEqual(cur.util, 30.0)
        self.assertFalse(cur.processes[0].has_util)


class CtxRefreshCadenceTest(unittest.TestCase):
    def test_rate_spans_real_fetches_and_carries_when_skipped(self):
        def c(compl):
            x = ctx(10)
            return HwContext(**{**x.__dict__, "completions": compl})

        s0 = sample(0.0, {10: 0}, contexts=[c(0)])
        s0.ioctl.ctx_ts = 100.0
        derive(s0, None, columns=5)
        # four polls without a refresh: contexts carried, ctx_ts unchanged
        prev = s0
        for t in (1.0, 2.0, 3.0, 4.0):
            s = sample(t, {10: 0}, contexts=[c(0)])
            s.ioctl.ctx_ts = 100.0
            derive(s, prev, columns=5)
            self.assertEqual(s.cmds_per_s, 0.0)
            prev = s
        # refresh after 5 s shows 500 completions: 100/s, not 500/s
        s5 = sample(5.0, {10: 0}, contexts=[c(500)])
        s5.ioctl.ctx_ts = 105.0
        derive(s5, prev, columns=5)
        self.assertAlmostEqual(s5.cmds_per_s, 100.0)
        self.assertAlmostEqual(s5.processes[0].cmds_per_s, 100.0)
        # next poll skipped again: rate carried over instead of dropping to 0
        s6 = sample(6.0, {10: 0}, contexts=[c(500)])
        s6.ioctl.ctx_ts = 105.0
        derive(s6, s5, columns=5)
        self.assertAlmostEqual(s6.cmds_per_s, 100.0)
        self.assertAlmostEqual(s6.processes[0].cmds_per_s, 100.0)
