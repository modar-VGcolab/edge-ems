"""Setpoint dispatcher (plan task 24): MQTT payload -> per-device writes.

Guards: conformity validation, site match, monotonic seq, max message age
(system design §3.2). Power sharing v1 (system design §6): battery active power
split proportional to each unit's available headroom in the commanded direction
— if the command exceeds total capacity every unit saturates and the remainder
is reported as leftover. Reactive power splits equally; derate factors are
broadcast unchanged to every unit of the class. The policy functions are pure
and replaceable (SoC-balancing etc. can swap in without touching dispatch).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from common.conformity import validate_setpoint_payload
from common.data_model import DataModel

from core.adapters.base import WriteResult


@dataclass(frozen=True)
class UnitLimits:
    charge_kw: float
    discharge_kw: float


def share_battery_power(
    total_kw: float, limits: dict[str, UnitLimits]
) -> tuple[dict[str, float], float]:
    """Split a class-level battery setpoint across units.

    Sign convention: positive = charge, negative = discharge (data model).
    Returns ({unit_id: setpoint_kw}, leftover_kw). leftover is 0 unless the
    fleet saturates.
    """
    if not limits:
        return {}, total_kw
    charging = total_kw >= 0
    caps = {
        uid: max(0.0, lim.charge_kw if charging else lim.discharge_kw)
        for uid, lim in limits.items()
    }
    cap_total = sum(caps.values())
    magnitude = abs(total_kw)
    sign = 1.0 if charging else -1.0
    if cap_total <= 0.0:
        return dict.fromkeys(limits, 0.0), total_kw
    if magnitude >= cap_total:  # fleet saturated: everyone at their limit
        return {uid: sign * cap for uid, cap in caps.items()}, sign * (magnitude - cap_total)
    return {uid: sign * magnitude * cap / cap_total for uid, cap in caps.items()}, 0.0


@dataclass
class DispatchReport:
    accepted: bool
    reason: str | None = None
    written: dict[str, WriteResult] = field(default_factory=dict)
    leftover_kw: float = 0.0


class SetpointDispatcher:
    def __init__(
        self,
        dm: DataModel,
        site_id: str,
        adapters: dict[str, object],  # asset_id -> DeviceAdapter
        asset_classes: dict[str, str],  # asset_id -> asset_class
        max_age_s: float = 2.0,
    ):
        self._dm = dm
        self._site_id = site_id
        self._adapters = adapters
        self._classes = asset_classes
        self._max_age = max_age_s
        self._last_seq: dict[str, int] = {}

    def _guard(self, payload: dict, now: float) -> str | None:
        errors = validate_setpoint_payload(payload, self._dm)
        if errors:
            return f"non-conformant payload: {errors}"
        if payload["site_id"] != self._site_id:
            return f"wrong site '{payload['site_id']}'"
        asset_class = payload["asset_class"]
        if payload["seq"] <= self._last_seq.get(asset_class, -1):
            return f"stale seq {payload['seq']}"
        try:
            ts = datetime.fromisoformat(str(payload["ts"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return f"unparseable ts '{payload['ts']}'"
        if now - ts > self._max_age:
            return f"message too old ({now - ts:.1f}s)"
        return None

    async def dispatch(
        self,
        payload: dict,
        unit_limits: dict[str, UnitLimits] | None = None,
        now: float | None = None,
    ) -> DispatchReport:
        now = time.time() if now is None else now
        reason = self._guard(payload, now)
        if reason is not None:
            return DispatchReport(accepted=False, reason=reason)
        asset_class = payload["asset_class"]
        self._last_seq[asset_class] = payload["seq"]
        unit_ids = [aid for aid, cls in self._classes.items() if cls == asset_class]
        if not unit_ids:
            return DispatchReport(accepted=False, reason=f"no units of class '{asset_class}'")

        setpoints = dict(payload["setpoints"])
        report = DispatchReport(accepted=True)

        per_unit: dict[str, dict[str, float]] = {uid: {} for uid in unit_ids}
        if asset_class == "battery" and "active_power_setpoint_kw" in setpoints:
            total = setpoints.pop("active_power_setpoint_kw")
            shares, leftover = share_battery_power(
                total, {uid: (unit_limits or {}).get(uid, UnitLimits(0.0, 0.0)) for uid in unit_ids}
            )
            report.leftover_kw = leftover
            for uid, kw in shares.items():
                per_unit[uid]["active_power_setpoint_kw"] = kw
        if "reactive_power_setpoint_kvar" in setpoints:
            kvar_each = setpoints.pop("reactive_power_setpoint_kvar") / len(unit_ids)
            for uid in unit_ids:
                per_unit[uid]["reactive_power_setpoint_kvar"] = kvar_each
        for name, value in setpoints.items():  # remaining (derate factors etc.): broadcast
            for uid in unit_ids:
                per_unit[uid][name] = value

        for uid in unit_ids:
            if per_unit[uid]:
                report.written[uid] = await self._adapters[uid].write_points(per_unit[uid])
        return report
