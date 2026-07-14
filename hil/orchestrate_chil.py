"""CHIL orchestration -- reuse the SIL scenario oracle against the plant.

For each of the seven scenarios this turns the profile (hil.profiles) plus the
fault hints into a plant-in-the-loop run (hil.chil_runner) and asserts with the
*exact* check function from tests/sil/scenarios.py. It also runs the three CHIL
fault injections from prompt section 7.

Two layers, same checks:
  * `run_scenario_pil` / `run_all` drive the software plant-in-the-loop and run
    here with no rig or docker stack -- this is what we validate in this sandbox.
  * `build_rig_actions` reuses tests/sil/orchestrate.build_actions so the *rig*
    run drives the real controller HTTP API + docker fault injection unchanged;
    the only CHIL difference is the plant (Typhoon schematic via hil.schematic)
    and the profile push (Typhoon SCADA via hil.profiles timelines).
"""

from __future__ import annotations

import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

# tests/sil is not a package; add it so we can import the oracle + action planner.
_REPO = Path(__file__).resolve().parents[1]
_SIL = _REPO / "tests" / "sil"
if str(_SIL) not in sys.path:
    sys.path.insert(0, str(_SIL))

import scenarios as sil_scenarios  # noqa: E402  (tests/sil/scenarios.py -- the oracle)

from hil import profiles  # noqa: E402
from hil.chil_runner import PlantInTheLoop  # noqa: E402
from hil.plant.models import SiteModel  # noqa: E402

_SETTLE_CYCLES = 5


@dataclass
class ScenarioResult:
    name: str
    passed: bool
    error: str | None
    n_samples: int
    summary: dict = field(default_factory=dict)
    series: list[dict] = field(default_factory=list)


def _mode_runs(modes: list[str]) -> str:
    """Compress a mode series to its transitions, e.g. RUN->HOLD->SAFE."""
    out = [modes[0]]
    for m in modes[1:]:
        if m != out[-1]:
            out.append(m)
    return "->".join(out)


def _summary(series: list[dict]) -> dict:
    if not series:
        return {}
    loops = [r["loop_duration_ms"] for r in series]
    return {
        "modes": _mode_runs([r["mode"] for r in series]),
        "final_pcc_error_kw": round(series[-1]["pcc_error_kw"], 3),
        "worst_pcc_error_kw": round(max(abs(r["pcc_error_kw"]) for r in series), 3),
        "min_derate_factor": round(min(r["derate_factor"] for r in series), 3),
        "min_curtail_factor": round(min(r["curtail_factor"] for r in series), 3),
        "pi_output_pstdev": round(statistics.pstdev([r["pi_output_kw"] for r in series]), 3),
        "loop_ms_max": round(max(loops), 3),
        "loop_ms_p95": round(sorted(loops)[max(0, int(len(loops) * 0.95) - 1)], 3),
    }


def _new_pil(stim: profiles.ScenarioStimulus) -> tuple[PlantInTheLoop, SiteModel]:
    site = SiteModel()
    site.battery.soc_pct = stim.soc_init_pct
    if stim.pv_peak_kw is not None:
        # Model a PV plant large enough to export past the feed limit; raise the
        # inverter apparent-power clamp to match the nameplate under test.
        site.pv.peak_kw = stim.pv_peak_kw
        site.pv.max_kva = stim.pv_peak_kw
    overrides = None
    if stim.droop_enabled:
        overrides = {"droop": {"enabled": True, "p_f_droop": {"enabled": True}}}
    pil = PlantInTheLoop(site, ems_overrides=overrides)
    return pil, site


def _half_cycles(stim: profiles.ScenarioStimulus, p: PlantInTheLoop) -> int:
    return max(1, int((stim.duration_s / p.update_period) // 2))


def _hot_reload(p: PlantInTheLoop) -> None:
    """Emulate PUT /config/assets: re-parse + re-validate the same config. The
    loop must not leave RUN -- the controller keeps its state across the reload."""
    import yaml
    from common.config_models import validate_asset_config
    from common.data_model import DataModel
    dm = DataModel.load(_REPO / "data_model.yaml")
    raw = yaml.safe_load((_REPO / "configs/asset_config.example.yaml").read_text())
    validate_asset_config(raw, dm)


def _apply_check(name: str, series: list[dict]) -> tuple[bool, str | None]:
    try:
        sil_scenarios.by_name(name).check(series)
        return True, None
    except AssertionError as e:
        return False, str(e)


def run_scenario_pil(name: str) -> ScenarioResult:
    """Run one scenario through the plant-in-the-loop and apply its SIL check."""
    stim = profiles.by_name(name)
    pil, _ = _new_pil(stim)
    half = _half_cycles(stim, pil)

    def on_cycle(i: int, t: float, p: PlantInTheLoop) -> None:
        # Fault timing mirrors tests/sil/orchestrate.build_actions.
        if name == "stale_data" and i >= _SETTLE_CYCLES:
            p.faults.stale_inputs = True  # writer stalled -> stale aggregates
        if name == "config_reload" and i == half:
            _hot_reload(p)  # re-validate config mid-run; must stay seamless

    series = pil.run(stim.duration_s, stimulus=stim.apply,
                     pcc_setpoint_kw=stim.pcc_setpoint_kw, on_cycle=on_cycle)
    passed, err = _apply_check(name, series)
    return ScenarioResult(name, passed, err, len(series), _summary(series), series)


def run_all() -> list[ScenarioResult]:
    return [run_scenario_pil(s.name) for s in sil_scenarios.SCENARIOS]


# --------------------------------------------------------- fault injections --


@dataclass
class FaultResult:
    name: str
    passed: bool
    detail: str
    series: list[dict] = field(default_factory=list)


def fault_influx_write_stall() -> FaultResult:
    """Writer stall -> RUN -> HOLD -> SAFE, then recovery back to RUN when the
    writer resumes (extends the stale_data scenario with a recovery tail)."""
    stim = profiles.stale_data()
    pil, _ = _new_pil(stim)

    def on_cycle(i, t, p: PlantInTheLoop):
        p.faults.stale_inputs = 5 <= i < 22  # stalled, then resumes

    series = pil.run(30, stimulus=stim.apply, on_cycle=on_cycle)
    modes = _mode_runs([r["mode"] for r in series])
    recovered = series[-1]["mode"] == "RUN"
    ok = ("HOLD" in modes) and ("SAFE" in modes) and recovered
    return FaultResult("influx_write_stall", ok,
                       f"modes {modes}; recovered_to_RUN={recovered}", series)


def fault_mqtt_broker_loss() -> FaultResult:
    """Broker loss: publishes fail for a window. The control loop must keep
    running (mode RUN) and, on recovery, the freshest setpoint is what is held.

    The deep queue + sequence-guard (publish only the newest, drop stale) lives
    in the core publisher, exercised by tests/integration; here we verify the
    *control loop* tolerates publish failure without leaving RUN and that the
    last command equals the latest computed setpoint."""
    stim = profiles.tracking()
    pil, site = _new_pil(stim)
    dropped: list[float] = []
    last_computed = {"v": 0.0}
    real_publish = pil._publish

    def flaky_publish(asset_class, values, now):
        if asset_class == "battery":
            last_computed["v"] = values.get("active_power_setpoint_kw", 0.0)
            if 5.0 <= now < 12.0:  # broker down -> publish fails, setpoint queued
                dropped.append(now)
                return False
        return real_publish(asset_class, values, now)

    pil._publish = flaky_publish  # type: ignore[method-assign]
    series = pil.run(20, stimulus=stim.apply)
    modes = {r["mode"] for r in series}
    freshest = abs(site.battery._p_setpoint - last_computed["v"]) < 1e-6
    ok = modes == {"RUN"} and len(dropped) > 0 and freshest
    return FaultResult("mqtt_broker_loss", ok,
                       f"modes={modes}; dropped_publishes={len(dropped)}; "
                       f"freshest_applied={freshest}", series)


def fault_asset_dropout_recovery() -> FaultResult:
    """PV drops out mid-run: aggregate excludes it (COMM_FAIL), controller keeps
    running; then recovers. Battery dropout must force SAFE."""
    stim = profiles.curtailment()
    pil, _ = _new_pil(stim)

    def on_cycle(i, t, p: PlantInTheLoop):
        p.faults.dropped_classes = {"pv"} if 8 <= i < 20 else set()
        p.faults.battery_comm_fail = 24 <= i < 28  # battery lost -> SAFE

    series = pil.run(35, stimulus=stim.apply, on_cycle=on_cycle)
    modes = [r["mode"] for r in series]
    ran_without_pv = bool(modes[8:20]) and all(m in ("RUN", "HOLD") for m in modes[8:20])
    safe_on_batt = "SAFE" in modes[24:28]
    recovered = modes[-1] == "RUN"
    ok = ran_without_pv and safe_on_batt and recovered
    return FaultResult("asset_dropout_recovery", ok,
                       f"pv-dropout kept running={ran_without_pv}; "
                       f"battery-dropout->SAFE={safe_on_batt}; recovered={recovered}; "
                       f"modes={_mode_runs(modes)}", series)


def run_faults() -> list[FaultResult]:
    return [
        fault_influx_write_stall(),
        fault_mqtt_broker_loss(),
        fault_asset_dropout_recovery(),
    ]


# ------------------------------------------------------- rig orchestration ---


def build_rig_actions(scenario_name: str):
    """Reuse the SIL action planner for the rig run (HTTP API + docker faults).
    The CHIL difference is only the plant + the profile push, handled out of band
    by the Typhoon SCADA injector (hil.profiles timelines)."""
    import orchestrate as sil_orchestrate  # tests/sil/orchestrate.py
    return sil_orchestrate.build_actions(sil_scenarios.by_name(scenario_name))


if __name__ == "__main__":
    print("== CHIL scenarios (plant-in-the-loop) ==")
    for r in run_all():
        flag = "PASS" if r.passed else "FAIL"
        tail = f"  ({r.error})" if r.error else ""
        print(f"  [{flag}] {r.name:<18} {r.summary.get('modes','')}{tail}")
    print("== fault injections ==")
    for fr in run_faults():
        print(f"  [{'PASS' if fr.passed else 'FAIL'}] {fr.name:<22} {fr.detail}")
