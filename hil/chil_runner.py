"""Plant-in-the-loop runner: the real controller core against the SiteModel.

This closes the control loop *without* the rig or the docker stack, so the CHIL
control behaviour is verifiable here: it wires the genuine
controller.control_loop.ControlLoop (and the real EdgeController / ModeController
/ DroopController, built from the repo configs) to a hil.plant.SiteModel through
in-process read/publish callbacks, and records the `control` measurement series
exactly as core.py would write it to InfluxDB.

The same ControlLoop runs on the rig; there the read/publish callbacks are the
InfluxDB reader and the MQTT publisher, and the plant is the Typhoon schematic
behind the HIL Modbus servers. Here they are direct SiteModel access. Because
the controller code is identical, a green run here is strong evidence the rig
run will satisfy the same scenarios.py checks -- and it lets us tune the PI on
realistic dynamics and reproduce any control bug at the unit level.

Causality per cycle: apply stimulus -> step plant with the previous setpoints
(dt) -> read measurements -> controller computes -> publish new setpoints.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from common.config_models import (
    EdgeEmsConfigFile,
    validate_asset_config,
    validate_edge_ems_config,
)
from common.data_model import DataModel
from common.points import COMM_FAIL, GOOD, STALE, PointValue
from controller.control_loop import (
    ControlLoop,
    Snapshot,
    battery_limits_from_config,
    derate_limits_from_config,
    params_from_config,
    pcc_params_from_config,
)
from controller.droop import DroopController
from controller.edge_controller import EdgeController
from controller.modes import ModeController

from hil.plant.models import SiteModel

REPO_ROOT = Path(__file__).resolve().parents[1]

# Stimulus: mutate the SiteModel for the elapsed sim-time t (seconds).
Stimulus = Callable[[float, SiteModel], None]


@dataclass
class Faults:
    """CHIL fault-injection switches, toggled by the orchestrator mid-run."""

    stale_inputs: bool = False       # freeze aggregate freshness -> HOLD -> SAFE
    battery_comm_fail: bool = False  # battery aggregate lost -> SAFE
    dropped_classes: set[str] = field(default_factory=set)  # exclude from aggregate


class PlantInTheLoop:
    def __init__(
        self,
        site: SiteModel,
        *,
        asset_config_path: str | Path = REPO_ROOT / "configs/asset_config.example.yaml",
        ems_config_path: str | Path = REPO_ROOT / "configs/edge_ems_config.example.yaml",
        data_model_path: str | Path = REPO_ROOT / "data_model.yaml",
        ems_overrides: dict | None = None,
    ):
        self.site = site
        self.faults = Faults()
        dm = DataModel.load(data_model_path)

        ac_raw = yaml.safe_load(Path(asset_config_path).read_text(encoding="utf-8"))
        self.asset_config = validate_asset_config(ac_raw, dm)
        ec_raw = yaml.safe_load(Path(ems_config_path).read_text(encoding="utf-8"))
        if ems_overrides:
            ec_raw = _deep_merge(ec_raw, ems_overrides)
        self.ems_config: EdgeEmsConfigFile = validate_edge_ems_config(ec_raw, dm)

        self.params = params_from_config(self.ems_config)
        self.battery_limits = battery_limits_from_config(self.asset_config)
        self.derate_limits = derate_limits_from_config(self.asset_config)
        self.pcc_base_kw, self.max_feed_kw = pcc_params_from_config(self.asset_config)
        self.update_period = self.ems_config.controller.update_period
        self.timeout_period = self.ems_config.controller.timeout_period
        self.hold_max_s = self.ems_config.controller.hold_max_s

        self.edge = EdgeController(self.params, self.battery_limits, self.derate_limits)
        self.modes = ModeController(self.hold_max_s)
        droop_cfg = self.ems_config.droop
        self.droop = DroopController(droop_cfg, self.pcc_base_kw) if droop_cfg else None

        self.series: list[dict] = []
        self.cycle_wall_ms: list[float] = []  # full read+compute+publish (loop budget)
        self._last_ts = 0.0

    # -- in-process I/O callbacks for the real ControlLoop -------------------

    def _quality(self) -> str:
        if self.faults.stale_inputs:
            return STALE
        return GOOD

    def _read_snapshot(self, now: float) -> Snapshot:
        # Advance the plant by one period using the setpoints from the last cycle,
        # then sample. dt is real sim-time between reads.
        dt = self.update_period if self._last_ts == 0.0 else (now - self._last_ts)
        self._last_ts = now
        self.site.step(max(0.0, dt))

        q = self._quality()
        agg: dict[str, dict[str, PointValue]] = {}

        if "battery" not in self.faults.dropped_classes:
            bq = COMM_FAIL if self.faults.battery_comm_fail else q
            bp = self.site.points("battery")
            agg["battery"] = {k: PointValue(v, now, bq) for k, v in bp.items()}

        if "pv" not in self.faults.dropped_classes:
            pp = self.site.points("pv")
            agg["pv"] = {k: PointValue(v, now, q) for k, v in pp.items()}

        pcc_pts = self.site.points("pcc")
        pcc = {k: PointValue(v, now, q) for k, v in pcc_pts.items()}
        return Snapshot(aggregates=agg, pcc=pcc)

    def _publish(self, asset_class: str, values: dict[str, float], now: float) -> bool:
        # Controller -> plant converters (what the HIL setpoint registers carry).
        for point, value in values.items():
            self.site.apply_setpoint(asset_class, point, value)
        return True

    def _write_control(self, fields: dict, now: float) -> None:
        self.series.append(dict(fields))

    # -- run -----------------------------------------------------------------

    def build_loop(self, *, pcc_setpoint_kw: float = 0.0) -> ControlLoop:
        return ControlLoop(
            edge=self.edge,
            modes=self.modes,
            droop=self.droop,
            read_snapshot=self._read_snapshot,
            publish=self._publish,
            pcc_base_kw=self.pcc_base_kw,
            max_feed_kw=self.max_feed_kw,
            pcc_setpoint_kw=pcc_setpoint_kw,
            write_control=self._write_control,
        )

    def run(
        self,
        duration_s: float,
        *,
        stimulus: Stimulus | None = None,
        pcc_setpoint_kw: float = 0.0,
        on_cycle: Callable[[int, float, "PlantInTheLoop"], None] | None = None,
    ) -> list[dict]:
        """Run the closed loop for `duration_s` of sim-time at the update period.

        `stimulus(t, site)` mutates the plant each cycle (profile injection).
        `on_cycle(i, t, self)` runs after each cycle (fault toggling, logging).
        Returns the `control` measurement series for the scenarios.py checks.
        """
        loop = self.build_loop(pcc_setpoint_kw=pcc_setpoint_kw)
        self.series.clear()
        self.cycle_wall_ms.clear()
        n = max(1, int(round(duration_s / self.update_period)))
        sim_t = 0.0
        for i in range(n):
            if stimulus is not None:
                stimulus(sim_t, self.site)
            now = (i + 1) * self.update_period  # monotonic loop clock
            t0 = time.perf_counter()
            loop.run_once(now=now)
            self.cycle_wall_ms.append((time.perf_counter() - t0) * 1000.0)
            if on_cycle is not None:
                on_cycle(i, sim_t, self)
            sim_t += self.update_period
        return list(self.series)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
