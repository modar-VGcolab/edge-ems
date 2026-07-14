"""Watchdog state machine + gateway runtime (the heart of S1).

The `Watchdog` is pure and time-injected so it unit-tests without clocks, MQTT,
or HTTP:

    FOLLOWING_EXTERNAL: effective P* = last external P*
       --(no external msg for >= watchdog_timeout)--> SELF_CONSUMPTION  [TAKEOVER]
    SELF_CONSUMPTION:    effective P* = self_consumption_kw (0)
       --(a fresh external msg arrives)-->            FOLLOWING_EXTERNAL [RELEASE]

Startup with no external message yet -> SELF_CONSUMPTION (safe default).
Freshness boundary: an external message is fresh while its age is strictly less
than the timeout; at age == timeout it is stale and the gateway takes over.

The `Gateway` wires the watchdog to side effects (forward effective setpoint,
write InfluxDB, log transitions) via injected callables, so it is also testable
with fakes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger("ext_ems.gateway")


class State(str, Enum):
    FOLLOWING_EXTERNAL = "FOLLOWING_EXTERNAL"
    SELF_CONSUMPTION = "SELF_CONSUMPTION"


@dataclass(frozen=True)
class TickResult:
    state: State
    effective_setpoint_kw: float
    active_source: int  # 1 = external, 0 = self-consumption
    changed: bool
    transition: str | None  # "TAKEOVER" | "RELEASE" | None
    last_external_age_s: float | None


class Watchdog:
    """Freshness clock + FOLLOWING <-> SELF_CONSUMPTION transitions."""

    def __init__(self, timeout_s: float, self_consumption_kw: float = 0.0):
        self.timeout_s = float(timeout_s)
        self.self_consumption_kw = float(self_consumption_kw)
        self._last_external_kw: float | None = None
        self._last_external_ts: float | None = None
        self._state = State.SELF_CONSUMPTION  # safe default before any message
        self.takeover_count = 0

    @property
    def state(self) -> State:
        return self._state

    @property
    def last_external_kw(self) -> float | None:
        return self._last_external_kw

    def on_external(self, pcc_setpoint_kw: float, now: float) -> None:
        """Record a fresh external setpoint (does not transition; tick does)."""
        self._last_external_kw = float(pcc_setpoint_kw)
        self._last_external_ts = float(now)

    def last_external_age_s(self, now: float) -> float | None:
        if self._last_external_ts is None:
            return None
        return now - self._last_external_ts

    def _fresh(self, now: float) -> bool:
        age = self.last_external_age_s(now)
        return age is not None and age < self.timeout_s

    def effective_setpoint_kw(self, now: float) -> float:
        if self._fresh(now) and self._last_external_kw is not None:
            return self._last_external_kw
        return self.self_consumption_kw

    def tick(self, now: float) -> TickResult:
        """Re-evaluate freshness, applying any FOLLOWING<->SELF transition."""
        prev = self._state
        self._state = State.FOLLOWING_EXTERNAL if self._fresh(now) else State.SELF_CONSUMPTION
        changed = self._state != prev
        transition: str | None = None
        if changed:
            if self._state == State.SELF_CONSUMPTION:
                self.takeover_count += 1
                transition = "TAKEOVER"
            else:
                transition = "RELEASE"
        effective = self.effective_setpoint_kw(now)
        return TickResult(
            state=self._state,
            effective_setpoint_kw=effective,
            active_source=1 if self._state == State.FOLLOWING_EXTERNAL else 0,
            changed=changed,
            transition=transition,
            last_external_age_s=self.last_external_age_s(now),
        )


class Gateway:
    """Drives the watchdog and pushes effects out.

    `forward(setpoint_kw)` applies the effective PCC target to the controller.
    `write_state(fields)` persists telemetry (InfluxDB). Both are injected so the
    runtime can be tested with fakes. The forwarder is called whenever the
    effective setpoint changes (and on every transition), so following a moving
    external setpoint propagates while takeover/release happen exactly once.
    """

    def __init__(
        self,
        watchdog: Watchdog,
        forward: Callable[[float], bool],
        write_state: Callable[[dict], None] | None = None,
    ):
        self._wd = watchdog
        self._forward = forward
        self._write_state = write_state
        self._last_forwarded: float | None = None

    @property
    def watchdog(self) -> Watchdog:
        return self._wd

    def handle_external(self, pcc_setpoint_kw: float, now: float) -> None:
        self._wd.on_external(pcc_setpoint_kw, now)

    def tick(self, now: float) -> TickResult:
        res = self._wd.tick(now)
        if res.transition == "TAKEOVER":
            log.warning(
                "TAKEOVER: external EMS silent for %.1fs (>= %.1fs) -> SELF_CONSUMPTION, "
                "PCC target -> %.1f kW",
                res.last_external_age_s or -1.0,
                self._wd.timeout_s,
                res.effective_setpoint_kw,
            )
        elif res.transition == "RELEASE":
            log.warning(
                "RELEASE: fresh external setpoint -> FOLLOWING_EXTERNAL, PCC target -> %.1f kW",
                res.effective_setpoint_kw,
            )

        if self._last_forwarded is None or res.effective_setpoint_kw != self._last_forwarded:
            if self._forward(res.effective_setpoint_kw):
                self._last_forwarded = res.effective_setpoint_kw

        if self._write_state is not None:
            self._write_state(
                {
                    "active_source": res.active_source,
                    "pcc_setpoint_kw": res.effective_setpoint_kw,
                    "last_external_age_s": (
                        res.last_external_age_s if res.last_external_age_s is not None else -1.0
                    ),
                }
            )
        return res

    def status(self, now: float) -> dict:
        return {
            "state": self._wd.state.value,
            "active_source": (
                "external" if self._wd.state == State.FOLLOWING_EXTERNAL else "self_consumption"
            ),
            "last_external_age_s": self._wd.last_external_age_s(now),
            "watchdog_timeout_s": self._wd.timeout_s,
            "last_pcc_setpoint_kw": self._wd.effective_setpoint_kw(now),
            "takeover_count": self._wd.takeover_count,
        }
