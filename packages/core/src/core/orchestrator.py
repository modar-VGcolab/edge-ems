"""Core orchestration cycle (Phase 2/3 wiring of tasks 13-16, 24, 25).

The southbound heartbeat: poll every device, write per-asset and aggregate
records to InfluxDB, dispatch any inbound setpoints to the right devices, and
run the setpoint-silence watchdog. The algorithms (adapters, aggregator,
dispatcher, watchdog, influx writer) are already unit-tested in isolation; this
class only sequences them, so it is exercised end to end in SIL.

Kept free of client construction (paho/influx) and of the asyncio schedule:
those live in core.main. Everything here takes injected collaborators and a
caller-supplied `now`, so the cycle is unit-testable with fakes.
"""

from __future__ import annotations

from common.data_model import DataModel

from core.adapters.base import PointValue, WriteResult
from core.aggregator import aggregate_class
from core.dispatcher import DispatchReport, SetpointDispatcher, UnitLimits
from core.influx_writer import InfluxWriter
from core.watchdog import SetpointWatchdog


class Orchestrator:
    def __init__(
        self,
        dm: DataModel,
        site_id: str,
        adapters: dict[str, object],  # asset_id -> DeviceAdapter
        asset_classes: dict[str, str],  # asset_id -> asset_class
        input_points: dict[str, list[str]],  # asset_id -> input field names to read
        writer: InfluxWriter,
        dispatcher: SetpointDispatcher,
        watchdog: SetpointWatchdog,
        weights: dict[str, float] | None = None,  # asset_id -> capacity (for *_pct avg)
    ):
        self._dm = dm
        self._site_id = site_id
        self._adapters = adapters
        self._classes = asset_classes
        self._input_points = input_points
        self._writer = writer
        self._dispatcher = dispatcher
        self._watchdog = watchdog
        self._weights = weights or {}
        self._aggregated = set(dm.aggregates)  # battery / pv / flexible_load
        self._latest: dict[str, dict[str, PointValue]] = {}

    async def poll_cycle(self, now: float) -> int:
        """Read every device, write per-asset + aggregate records. Returns the
        number of records written."""
        readings: dict[str, dict[str, PointValue]] = {}
        for aid, names in self._input_points.items():
            readings[aid] = await self._adapters[aid].read_points(names)
        self._latest = readings

        points = []
        for aid, vals in readings.items():
            points.append(self._writer.build_asset_point(self._classes[aid], aid, vals))
        for cls in self._aggregated:
            members = {aid: v for aid, v in readings.items() if self._classes[aid] == cls}
            if not members:
                continue
            weights = {aid: self._weights.get(aid, 1.0) for aid in members}
            agg = aggregate_class(cls, self._dm, members, weights=weights)
            points.append(self._writer.build_aggregate_point(cls, agg))
        return self._writer.write_cycle(points)

    async def handle_setpoint(self, payload: dict, now: float) -> DispatchReport:
        """Dispatch one inbound setpoint message; feed the watchdog on accept."""
        report = await self._dispatcher.dispatch(payload, self._battery_unit_limits(), now=now)
        if report.accepted:
            self._watchdog.notify(now)
        return report

    async def safety_cycle(self, now: float) -> bool:
        """Run the silence watchdog. True if it tripped safe-state this call."""
        return await self._watchdog.check(now)

    def _battery_unit_limits(self) -> dict[str, UnitLimits]:
        """Per-unit charge/discharge headroom from the latest BMS readings, used
        by the dispatcher's proportional power sharing."""
        limits: dict[str, UnitLimits] = {}
        for aid, cls in self._classes.items():
            if cls != "battery":
                continue
            vals = self._latest.get(aid, {})
            charge = vals.get("available_charge_power_kw")
            discharge = vals.get("available_discharge_power_kw")
            limits[aid] = UnitLimits(
                charge_kw=float(charge.value) if charge and charge.value is not None else 0.0,
                discharge_kw=float(discharge.value)
                if discharge and discharge.value is not None
                else 0.0,
            )
        return limits


def safe_state_results_ok(results: dict[str, WriteResult]) -> bool:
    """Helper: did every safe-state write succeed?"""
    return all(r.ok for r in results.values())
