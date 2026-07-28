"""Controller runtime tests: LoopRunner threading/overrun and LiveSnapshotReader."""

import time

import yaml
from common.config_manager import EMS, ConfigManager
from common.points import GOOD, PointValue
from controller.control_loop import CycleResult, Snapshot
from controller.runtime import LiveSnapshotReader, LoopRunner, build_control_loop


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


# ------------------------------------------------------- apply_config (KNOWN_ISSUES #2)


def _pv(value, quality=GOOD):
    return PointValue(value, time.time(), quality)


def _importing_snapshot():
    return Snapshot(
        aggregates={
            "battery": {
                "soc_pct": _pv(50.0),
                "active_power_kw": _pv(0.0),
                "available_charge_power_kw": _pv(1000.0),
                "available_discharge_power_kw": _pv(1000.0),
            },
            "pv": {"active_power_kw": _pv(0.0)},
        },
        pcc={
            "active_power_kw": _pv(120.0),  # importing -> nonzero PI error -> integral moves
            "frequency_hz": _pv(50.0),
            "voltage_v": _pv(230.0),
        },
    )


def _cm_from_examples(dm, tmp_path, asset_config_raw, edge_ems_config_raw):
    asset_path = tmp_path / "asset_config.yaml"
    ems_path = tmp_path / "edge_ems_config.yaml"
    asset_path.write_text(yaml.safe_dump(asset_config_raw, sort_keys=False), encoding="utf-8")
    ems_path.write_text(yaml.safe_dump(edge_ems_config_raw, sort_keys=False), encoding="utf-8")
    return ConfigManager(dm, asset_path, ems_path)


def test_apply_config_updates_tunables_without_resetting_state(
    dm, tmp_path, asset_config_raw, edge_ems_config_raw
):
    cm = _cm_from_examples(dm, tmp_path, asset_config_raw, edge_ems_config_raw)
    assert cm.ems_config.controller.Kp == 0.5  # example.yaml default, sanity check

    loop = build_control_loop(
        cm, dm, read_snapshot=lambda now: _importing_snapshot(),
        publish=lambda *a: True, write_control=None,
    )
    runner = LoopRunner(loop, period_s=1.0)
    runner._loop.run_once(now=1.0)  # seed some rolling state (nonzero PI integral)
    integral_before = runner._loop.edge.state.integral
    assert integral_before != 0.0

    mutated = dict(edge_ems_config_raw)
    mutated["controller"] = dict(mutated["controller"], Kp=0.9, Ki=0.4)
    cm.update(EMS, mutated)
    runner.apply_config(cm)

    assert runner._loop.edge.params.kp == 0.9
    assert runner._loop.edge.params.ki == 0.4
    # rolling state must survive the reload untouched
    assert runner._loop.edge.state.integral == integral_before


def test_apply_config_noop_against_loop_handle_stub():
    # http_api._apply_to_running_loop must tolerate the test-only LoopHandle,
    # which has no apply_config -- this is exercised via getattr(..., None) at
    # the call site; here we just confirm LoopRunner (the real thing) has it.
    runner = LoopRunner(FakeLoop(), period_s=1.0)
    assert hasattr(runner, "apply_config")
