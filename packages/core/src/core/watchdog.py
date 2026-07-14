"""Setpoint-silence watchdog (plan task 25).

core.py's backstop against a dead or partitioned controller. The dispatcher
calls `notify()` on every accepted setpoint message; if no setpoint arrives for
longer than `max_silence_cycles` loop periods, the watchdog drives every
controllable device to its configured safe-state (battery -> 0 kW). The inverter
firmware's own last-setpoint timeout is the final backstop beneath this one
(system design 5).

Safe-state is commanded once per silence episode (not re-sent every cycle); a
fresh setpoint clears the trip and re-arms the watchdog. Kept free of timers and
I/O wiring so it is fully unit-testable: time is passed in, adapters are the
injected DeviceAdapter instances.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from core.adapters.base import WriteResult

# Default safe-state setpoints per controllable asset class (data_model output
# points). Battery to zero power; deratable classes to full output is unsafe for
# loads, so flexible_load is shed and pv is left producing (curtailment is a
# control nicety, not a safety action). Override via `safe_setpoints`.
DEFAULT_SAFE_SETPOINTS: dict[str, dict[str, float]] = {
    "battery": {"active_power_setpoint_kw": 0.0, "reactive_power_setpoint_kvar": 0.0},
    "flexible_load": {"derate_factor_setpoint": 0.0},
}


class SetpointWatchdog:
    def __init__(
        self,
        adapters: dict[str, object],  # asset_id -> DeviceAdapter
        asset_classes: dict[str, str],  # asset_id -> asset_class
        period_s: float,
        max_silence_cycles: int = 3,
        safe_setpoints: dict[str, dict[str, float]] | None = None,
    ):
        if period_s <= 0:
            raise ValueError("period_s must be positive")
        if max_silence_cycles < 1:
            raise ValueError("max_silence_cycles must be >= 1")
        self._adapters = adapters
        self._classes = asset_classes
        self._timeout = period_s * max_silence_cycles
        self._safe = safe_setpoints or DEFAULT_SAFE_SETPOINTS
        self._last_setpoint_ts: float | None = None
        self._tripped = False

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def timeout_s(self) -> float:
        return self._timeout

    def notify(self, now: float) -> None:
        """Record an accepted setpoint; clears any active trip and re-arms."""
        self._last_setpoint_ts = now
        self._tripped = False

    def is_silent(self, now: float) -> bool:
        """True once nothing has been heard for longer than the timeout. Before
        the first setpoint the watchdog is armed from `now` via `start()`; if it
        was never started or notified, it stays quiet (no spurious trip at boot)."""
        if self._last_setpoint_ts is None:
            return False
        return (now - self._last_setpoint_ts) > self._timeout

    def start(self, now: float) -> None:
        """Arm the silence timer at startup so a controller that never speaks is
        still caught after the timeout."""
        self._last_setpoint_ts = now
        self._tripped = False

    async def check(self, now: float) -> bool:
        """Run once per core cycle. Returns True if this call tripped safe-state."""
        if self._tripped or not self.is_silent(now):
            return False
        await self.trip()
        self._tripped = True
        return True

    async def trip(self) -> dict[str, WriteResult]:
        """Command safe-state to every controllable device. Best-effort: a failed
        write is recorded but does not stop the others (each is independent)."""
        results: dict[str, WriteResult] = {}
        for asset_id, asset_class in self._classes.items():
            setpoints = self._safe.get(asset_class)
            if not setpoints:
                continue
            adapter = self._adapters.get(asset_id)
            if adapter is None:
                continue
            try:
                results[asset_id] = await adapter.write_points(dict(setpoints))
            except Exception as exc:  # noqa: BLE001 - one bad device must not block safe-state
                results[asset_id] = WriteResult(ok=False, errors={"exception": str(exc)})
        return results


def make_safe_state_callback(
    watchdog: SetpointWatchdog,
) -> Callable[[float], Awaitable[bool]]:
    """Convenience: a per-cycle callable for the core loop to await."""

    async def _cb(now: float) -> bool:
        return await watchdog.check(now)

    return _cb
