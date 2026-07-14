"""Profile injection -- one parametrized entry point per CHIL scenario.

Each builder returns a `ScenarioStimulus`: the plant initial conditions (initial
SoC, PV size, droop flag, PCC setpoint) plus an `apply(t, site)` callable that
drives the schematic inputs over time -- irradiance ramps / clouds for PV, load
steps for the flexible load, frequency/voltage excursions at the grid source,
and SoC preconditioning. The software plant-in-the-loop (hil.chil_runner) calls
`apply` each cycle; on the rig the same timelines are pushed through the Typhoon
SCADA API (see write_scenario_csv / hil.schematic).

The seven names match tests/sil/scenarios.py exactly, so the orchestrator can
look a stimulus up by scenario name and assert with that scenario's `check`.

NOTE (HIL finding): the curtailment scenario needs net export beyond the PCC
feed-in limit (999 kVA), but the example site's PV is 600 kVA and a full battery
cannot charge to export -- so feed-limit curtailment cannot bind at nameplate.
The curtailment builder therefore models a PV plant sized to the surplus the
scenario describes (pv_peak_kw), and the gap is logged as a finding for the site
/ firmware owner (see hil/README.md and the CHIL report).
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from hil.plant.models import SiteModel

Apply = Callable[[float, SiteModel], None]


@dataclass
class ScenarioStimulus:
    name: str
    duration_s: int
    apply: Apply
    soc_init_pct: float = 50.0
    droop_enabled: bool = False
    pcc_setpoint_kw: float = 0.0
    pv_peak_kw: float | None = None  # override PV nameplate for this scenario
    note: str = ""
    # Sparse timeline rows (t_s, channel, value) for the Typhoon SCADA side.
    timeline: list[tuple[float, str, float]] = field(default_factory=list)


# -- small stimulus helpers --------------------------------------------------


def _ramp(t: float, t0: float, t1: float, v0: float, v1: float) -> float:
    if t <= t0:
        return v0
    if t >= t1:
        return v1
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


# -- the seven scenarios -----------------------------------------------------


def tracking(load_kw: float = 250.0) -> ScenarioStimulus:
    """Steady import; the PI should drive PCC error to ~0 by discharging."""
    return ScenarioStimulus(
        name="tracking",
        duration_s=30,
        soc_init_pct=50.0,
        apply=lambda t, s: (s.load.set_base_kw(load_kw), s.pv.set_irradiance(0.0)),
        note=f"steady {load_kw:g} kW import, mid SoC, battery free to track",
        timeline=[(0, "load_kw", load_kw), (0, "irradiance", 0.0)],
    )


def saturation_derate(load_kw: float = 600.0) -> ScenarioStimulus:
    """Low SoC + sustained import: battery pins at the discharge gate and the
    low-SoC warning band derates the flexible load."""
    return ScenarioStimulus(
        name="saturation_derate",
        duration_s=40,
        soc_init_pct=6.0,
        apply=lambda t, s: (s.load.set_base_kw(load_kw), s.pv.set_irradiance(0.0)),
        note="precondition SoC~6% (below 20% warn), sustained import",
        timeline=[(0, "soc_init", 6.0), (0, "load_kw", load_kw)],
    )


def curtailment(pv_peak_kw: float = 1200.0) -> ScenarioStimulus:
    """High SoC + PV surplus past the feed limit: PV is curtailed.

    pv_peak_kw models a plant large enough to export beyond the 999 kVA feed
    limit (the example site's 600 kVA PV cannot -- logged as a HIL finding).
    The battery starts full (at max SoC) so it cannot charge to absorb the
    export; otherwise the PI would charge it and the PCC never crosses the feed
    limit. scenarios.py describes this as "full battery"."""
    def apply(t: float, s: SiteModel) -> None:
        s.load.set_base(0.0)
        s.pv.set_irradiance(_ramp(t, 0, 15, 0.85, 1.0))  # ramp to surplus

    return ScenarioStimulus(
        name="curtailment",
        duration_s=40,
        soc_init_pct=95.0,
        pv_peak_kw=pv_peak_kw,
        apply=apply,
        note=f"full battery (SoC 95%, no charge headroom), PV peak {pv_peak_kw:g} kW "
             f"ramped past the 999 kVA feed limit",
        timeline=[(0, "soc_init", 95.0), (0, "irradiance", 0.85), (15, "irradiance", 1.0)],
    )


def droop(load_kw: float = 200.0) -> ScenarioStimulus:
    """Droop enabled; inject a frequency/voltage excursion so the PI moves."""
    def apply(t: float, s: SiteModel) -> None:
        s.load.set_base_kw(load_kw)
        s.pv.set_irradiance(0.0)
        # 49.5..50.5 Hz excursion; 0.9..1.1 pu voltage wobble.
        if t < 8:
            f, vpu = 50.0, 1.0
        elif t < 14:
            f, vpu = 50.4, 1.05
        elif t < 20:
            f, vpu = 49.6, 0.95
        else:
            f, vpu = 50.0, 1.0
        s.grid.set_frequency(f)
        s.grid.set_voltage_pu(vpu)

    return ScenarioStimulus(
        name="droop",
        duration_s=30,
        soc_init_pct=50.0,
        droop_enabled=True,
        apply=apply,
        note="P-f/Q-V droop on; f 50->50.4->49.6->50 Hz, V 1.0->1.05->0.95 pu",
        timeline=[
            (0, "frequency_hz", 50.0), (8, "frequency_hz", 50.4),
            (14, "frequency_hz", 49.6), (20, "frequency_hz", 50.0),
            (8, "voltage_pu", 1.05), (14, "voltage_pu", 0.95), (20, "voltage_pu", 1.0),
        ],
    )


def stale_data(load_kw: float = 250.0) -> ScenarioStimulus:
    """Steady plant; the stale path is injected by the orchestrator (the writer
    stall), driving RUN -> HOLD -> SAFE."""
    return ScenarioStimulus(
        name="stale_data",
        duration_s=20,
        soc_init_pct=50.0,
        apply=lambda t, s: (s.load.set_base_kw(load_kw), s.pv.set_irradiance(0.0)),
        note="steady; aggregate path stalled mid-run (fault: influx_stall)",
        timeline=[(0, "load_kw", load_kw)],
    )


def config_reload(load_kw: float = 250.0) -> ScenarioStimulus:
    """Steady plant; the hot reload is injected by the orchestrator."""
    return ScenarioStimulus(
        name="config_reload",
        duration_s=25,
        soc_init_pct=50.0,
        apply=lambda t, s: (s.load.set_base_kw(load_kw), s.pv.set_irradiance(0.0)),
        note="steady; PUT /config/assets mid-run, expect seamless RUN",
        timeline=[(0, "load_kw", load_kw)],
    )


def scale_50(load_kw: float = 250.0) -> ScenarioStimulus:
    """Steady plant; the scale orchestrator replicates to 50 servers and times
    the loop. The control law is O(1) in asset count, so the per-cycle budget is
    dominated by Modbus I/O (measured on the rig; see hil.scale_bench)."""
    return ScenarioStimulus(
        name="scale_50",
        duration_s=30,
        soc_init_pct=50.0,
        apply=lambda t, s: (s.load.set_base_kw(load_kw), s.pv.set_irradiance(0.0)),
        note="steady; 50 replicated Modbus servers, assert loop_duration_ms < 250",
        timeline=[(0, "load_kw", load_kw)],
    )


BUILDERS: dict[str, Callable[[], ScenarioStimulus]] = {
    "tracking": tracking,
    "saturation_derate": saturation_derate,
    "curtailment": curtailment,
    "droop": droop,
    "stale_data": stale_data,
    "config_reload": config_reload,
    "scale_50": scale_50,
}


def with_fixed_load(stim: ScenarioStimulus, fixed_load_kw: float) -> ScenarioStimulus:
    """Compose a non-controllable fixed load onto any scenario (opt-in).

    Returns a copy whose `apply` also pins `SiteModel.fixed_load` to
    `fixed_load_kw` each cycle, and whose timeline carries a `fixed_load_kw`
    channel for the Typhoon SCADA side. Existing scenarios are unchanged unless
    wrapped, so the seven-scenario oracle in tests/sil/scenarios.py is unaffected.

        stim = with_fixed_load(tracking(load_kw=250.0), 100.0)  # 250 flex + 100 fixed
    """
    base_apply = stim.apply

    def apply(t: float, s: SiteModel) -> None:
        base_apply(t, s)
        s.fixed_load.set_base_kw(fixed_load_kw)

    return replace(
        stim,
        apply=apply,
        timeline=stim.timeline + [(0.0, "fixed_load_kw", float(fixed_load_kw))],
        note=stim.note + f"; +{fixed_load_kw:g} kW fixed load",
    )


def by_name(name: str) -> ScenarioStimulus:
    if name not in BUILDERS:
        raise KeyError(f"unknown scenario '{name}'")
    return BUILDERS[name]()


def write_scenario_csv(stim: ScenarioStimulus, path: str | Path) -> Path:
    """Emit the sparse timeline as a CSV for the Typhoon SCADA injector.
    Columns: time_s, channel, value (channels: load_kw, fixed_load_kw, irradiance,
    frequency_hz, voltage_pu, soc_init)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["time_s", "channel", "value"])
        for t, channel, value in sorted(stim.timeline):
            w.writerow([t, channel, value])
    return path


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "profiles"
    for nm in BUILDERS:
        p = write_scenario_csv(by_name(nm), out / f"{nm}.csv")
        print(f"wrote {p}")
