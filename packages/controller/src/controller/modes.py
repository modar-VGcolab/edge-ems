"""Mode ladder RUN -> HOLD -> SAFE (plan task 23).

Deterministic supervisor that sits above the control core and decides whether
this cycle's computed setpoints are used, frozen, or overridden, based on input
freshness and battery availability (system design 5).

  RUN   normal operation; emit the controller's computed setpoints.
  HOLD  aggregate data went stale: keep the last setpoint for up to `hold_max_s`,
        giving a transient outage a chance to clear without disturbing the plant.
  SAFE  battery unavailable, or HOLD outlived `hold_max_s`: ramp the battery to
        0 kW (slew-limited) and release all derates/curtailment; raise an alarm.

Recovery is automatic: once data is fresh and the battery is back, the next
update returns to RUN. Every transition is reported (logged by the caller and
surfaced in /status and the `control` measurement). The decision to auto-recover
from SAFE rather than latch until manual reset is a deliberate choice for an
unattended edge controller -- revisit if site ops want a manual reset instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Mode(str, Enum):
    RUN = "RUN"
    HOLD = "HOLD"
    SAFE = "SAFE"


@dataclass(frozen=True)
class ModeDecision:
    mode: Mode
    changed: bool  # True when this update crossed a mode boundary
    reason: str  # human-readable cause, for logs / alarms


class ModeController:
    """Tracks the current mode and how long HOLD has been active."""

    def __init__(self, hold_max_s: float, mode: Mode = Mode.RUN):
        if hold_max_s < 0:
            raise ValueError("hold_max_s must be >= 0")
        self.hold_max_s = hold_max_s
        self.mode = mode
        self._hold_since: float | None = None

    def update(self, *, data_fresh: bool, battery_ok: bool, now: float) -> ModeDecision:
        """Advance the state machine. `data_fresh` is False when the required
        aggregate inputs are STALE/COMM_FAIL; `battery_ok` is False when the
        battery aggregate cannot be controlled (its loss is unrecoverable here)."""
        prev = self.mode

        if not battery_ok:
            self.mode = Mode.SAFE
            self._hold_since = None
            reason = "battery unavailable -> SAFE"
        elif data_fresh:
            self.mode = Mode.RUN
            self._hold_since = None
            reason = "data fresh -> RUN"
        else:  # data stale, battery present
            if self.mode == Mode.RUN:
                self.mode = Mode.HOLD
                self._hold_since = now
                reason = "stale data -> HOLD"
            elif self.mode == Mode.HOLD:
                held = now - (self._hold_since if self._hold_since is not None else now)
                if held >= self.hold_max_s:
                    self.mode = Mode.SAFE
                    self._hold_since = None
                    reason = f"HOLD exceeded {self.hold_max_s:g}s -> SAFE"
                else:
                    reason = "holding last setpoint"
            else:  # already SAFE and data still stale
                reason = "remaining in SAFE"

        return ModeDecision(mode=self.mode, changed=self.mode != prev, reason=reason)


def safe_battery_setpoint(
    last_setpoint_kw: float, slew_limit_kw_s: float | None, period_s: float
) -> float:
    """SAFE-mode battery target: ramp toward 0 kW, slew-limited if configured.

    With no slew limit the battery is commanded straight to 0. With one, it steps
    toward 0 by at most `slew_limit_kw_s * period_s` per cycle, never overshooting.
    """
    if slew_limit_kw_s is None:
        return 0.0
    step = slew_limit_kw_s * period_s
    if last_setpoint_kw > 0:
        return max(0.0, last_setpoint_kw - step)
    return min(0.0, last_setpoint_kw + step)
