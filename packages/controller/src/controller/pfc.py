"""Tariff-driven power-factor control (PFC) at the PCC.

A grid-support layer parallel to droop: instead of reacting to frequency/voltage,
it reacts to *time of day*. A pre-configured tariff schedule (`pfc.windows` in
edge_ems_config) maps each window to a power-factor goal, and this module turns
the active goal into a battery reactive setpoint (kVAr) so the site presents the
wanted PF at the PCC.

Sign conventions (data_model.yaml):
  pcc.reactive_power_kvar   + inductive (lagging) / - capacitive (leading)

So a target PCC reactive is  Q*_pcc = +/- |P_pcc| * tan(acos(pf)) -- positive for
a lagging goal, negative for leading. The battery isn't the only asset contributing
reactive power at the PCC, so the residual it needs to supply is
Q*_battery = Q*_pcc - other_reactive_kvar, where `other_reactive_kvar` is the
measured sum of every non-battery source (PV, flexible load, fixed load/meter
classes) in the same PCC sign convention. Skipping this subtraction sizes the
battery as if it alone must produce the whole target, which badly overshoots
whenever another asset (e.g. a large fixed load) is already contributing.
The battery VarSet that realises Q*_battery at the PCC meter is
q_sign_convention * Q*_battery; `q_sign_convention` (+1/-1) exists because
the battery-VarSet-to-PCC-reactive direction must be confirmed on the rig (the same
class of open sign-flag as PCC_P_SIGN / BESS_P_SIGN). Default +1; flip if the meter
disagrees.

The command is clamped to the battery inverter capability circle
sqrt(S_rated^2 - P^2) when `s_rated_kva` is configured, so reactive never steals
headroom the active-power dispatch needs.

Pure and side-effect-free (like droop.py) so the maths is unit-testable without a
clock or broker; the caller passes `now` (epoch seconds) and the measured powers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from common.config_models import PfcConfig


def _hhmm_to_minutes(s: str) -> int:
    """'HH:MM' -> minutes since local midnight. Format is enforced at config load."""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _in_window(minute_of_day: int, start_min: int, end_min: int) -> bool:
    """Is `minute_of_day` inside [start, end)? A window with start > end wraps past
    midnight (e.g. 20:00->08:00). start == end is treated as the empty window."""
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= minute_of_day < end_min
    return minute_of_day >= start_min or minute_of_day < end_min  # wraps midnight


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


@dataclass(frozen=True)
class _Window:
    start_min: int
    end_min: int
    pf_target: float
    mode: str


class PfcController:
    """Holds the parsed tariff schedule and the PCC/inverter parameters, and maps
    the wall clock to a battery reactive setpoint. First matching window wins."""

    def __init__(self, cfg: PfcConfig):
        self.cfg = cfg
        self._windows = [
            _Window(_hhmm_to_minutes(w.start), _hhmm_to_minutes(w.end), w.pf_target, w.mode)
            for w in cfg.windows
        ]

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def active_target(self, now: float) -> tuple[float, str]:
        """The (pf_target, mode) in force at epoch `now`, by local wall-clock time.
        Falls back to `cfg.default` when no window matches."""
        t = datetime.fromtimestamp(now)
        minute_of_day = t.hour * 60 + t.minute
        for w in self._windows:
            if _in_window(minute_of_day, w.start_min, w.end_min):
                return w.pf_target, w.mode
        return self.cfg.default.pf_target, self.cfg.default.mode

    def reactive_setpoint_kvar(
        self,
        now: float,
        pcc_active_kw: float,
        battery_active_kw: float,
        other_reactive_kvar: float = 0.0,
    ) -> float:
        """Battery reactive setpoint (kVAr) for the tariff goal active at `now`.

        `pcc_active_kw` sizes the reactive so the *PCC* PF hits the target;
        `battery_active_kw` (the active setpoint just computed) sizes the inverter
        capability clamp. `other_reactive_kvar` is the measured reactive power
        every non-battery source at the PCC is already contributing (PV,
        flexible load, fixed load/meter classes), in the PCC's own sign
        convention (+inductive/lagging, -capacitive/leading, same as
        `pcc.reactive_power_kvar`). It's subtracted from the PCC target
        *before* `q_sign_convention` is applied, so the battery is only sized
        for the residual -- without it, the battery would be commanded as if
        it alone had to produce the entire target, double-counting whatever
        the other assets already contribute (e.g. a large fixed load).
        Defaults to 0.0 for callers/tests with nothing else to subtract.
        Returns 0.0 for a unity/absent goal.
        """
        pf, mode = self.active_target(now)
        if mode == "unity" or pf >= 1.0:
            return 0.0
        q_mag = abs(pcc_active_kw) * math.tan(math.acos(_clamp(pf, 1e-6, 1.0)))
        pcc_sign = 1.0 if mode == "lagging" else -1.0  # +inductive / -capacitive
        q_pcc_target = pcc_sign * q_mag  # natural PCC-sign target, before VarSet calibration
        q_battery_needed = q_pcc_target - other_reactive_kvar  # residual, same PCC sign
        q = self.cfg.q_sign_convention * q_battery_needed
        if self.cfg.s_rated_kva is not None:
            headroom_sq = self.cfg.s_rated_kva**2 - battery_active_kw**2
            q_lim = math.sqrt(headroom_sq) if headroom_sq > 0.0 else 0.0
            q = _clamp(q, -q_lim, q_lim)
        return q
