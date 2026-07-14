"""Controller runtime tests: LoopRunner threading/overrun and LiveSnapshotReader."""

import time

from common.points import GOOD, PointValue
from controller.control_loop import CycleResult, Snapshot
from controller.runtime import LiveSnapshotReader, LoopRunner


class FakeLoop:
    def __init__(self):
        self.calls = 0
        self.pcc_setpoint_kw = 0.0  # LoopRunner.status() reads this off the loop

    def run_once(self, now):
        self.calls += 1
        return CycleResult(
            mode="RUN",
            mode_changed=False,
            mode_reason="",
            battery_setpoint_kw=-60.0,
            reactive_setpoint_kvar=0.0,
            derate_factor=1.0,
            curtail_factor=1.0,
            pcc_error_kw=-100.0,
            pi_output_kw=-60.0,
            loop_duration_ms=0.1,
            data_fresh=True,
            battery_ok=True,
            published=("battery",),
        )


def test_runner_starts_and_stops():
    runner = LoopRunner(FakeLoop(), period_s=0.02)
    assert runner.state == "stopped"
    assert runner.start()
    assert not runner.start()  # already running
    assert runner.state == "running"
    time.sleep(0.1)
    assert runner.stop()
    assert not runner.stop()  # already stopped
    assert runner.state == "stopped"


def test_runner_executes_cycles():
    loop = FakeLoop()
    runner = LoopRunner(loop, period_s=0.02)
    runner.start()
    time.sleep(0.12)
    runner.stop()
    assert loop.calls >= 3  # several cycles ran


def test_runner_status_reports_last_cycle():
    runner = LoopRunner(FakeLoop(), period_s=0.02)
    runner.start()
    time.sleep(0.06)
    runner.stop()
    st = runner.status()
    assert st["mode"] == "RUN"
    assert st["pcc_error_kw"] == -100.0
    assert st["battery_setpoint_kw"] == -60.0


def test_runner_status_before_first_cycle():
    runner = LoopRunner(FakeLoop(), period_s=1.0)
    st = runner.status()  # never started
    assert st["loop_state"] == "stopped"
    assert "mode" not in st


def test_runner_survives_cycle_exception():
    class Boom:
        def __init__(self):
            self.calls = 0

        def run_once(self, now):
            self.calls += 1
            raise RuntimeError("bad cycle")

    boom = Boom()
    runner = LoopRunner(boom, period_s=0.02)
    runner.start()
    time.sleep(0.08)
    runner.stop()
    assert boom.calls >= 2  # kept going despite exceptions


class _FakeAgg:
    def read(self, classes, now=None):
        return {"battery": {"soc_pct": PointValue(55.0, now, GOOD)}}


def test_live_snapshot_reader_assembles_aggregate_and_pcc(dm):
    now = time.time()

    def pcc_query():
        return [
            ("active_power_kw", 120.0, now),
            ("frequency_hz", 50.1, now),
            ("not_a_real_field", 1.0, now),  # dropped: not canonical
        ]

    reader = LiveSnapshotReader(dm, "site-1", _FakeAgg(), pcc_query, timeout_s=2.0)
    snap = reader(now)
    assert isinstance(snap, Snapshot)
    assert snap.aggregates["battery"]["soc_pct"].value == 55.0
    assert snap.pcc["active_power_kw"].value == 120.0
    assert "not_a_real_field" not in snap.pcc  # non-canonical field rejected


def test_live_snapshot_reader_marks_stale(dm):
    now = time.time()
    old = now - 100.0

    def pcc_query():
        return [("active_power_kw", 120.0, old)]

    reader = LiveSnapshotReader(dm, "site-1", _FakeAgg(), pcc_query, timeout_s=2.0)
    snap = reader(now)
    assert snap.pcc["active_power_kw"].quality == "STALE"
