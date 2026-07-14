"""P-f / Q-V droop (plan task 22).

Optional grid-support layer that nudges the PCC setpoint as a function of
measured frequency and voltage, before the PI sees the error (system design 4,
step t3: "adjusted PCC setpoint = configured + interp(f, V)").

Curves are the 4-knot piecewise-linear arrays from edge_ems_config
(`droop.p_f_droop`, `droop.q_v_droop`). Each correction array is in p.u. on the
PCC base (data_model.yaml: per-unit base = PCC max power); this module converts
to SI on the way out so everything crossing back into the loop is kW / kVAr.

Monotonicity is validated by the pydantic config models at load; we re-check
here so a hand-built DroopController can never interpolate a non-monotonic curve.
"""

from __future__ import annotations

from dataclasses import dataclass

from common.config_models import DroopConfig


def interp_clamped(x: float, xs: list[float], ys: list[float]) -> float:
    """Piecewise-linear interpolation with flat extrapolation past the ends.

    `xs` must be strictly increasing and the same length as `ys`. Outside
    [xs[0], xs[-1]] the nearest endpoint value is returned (no extrapolation).
    """
    if len(xs) != len(ys) or len(xs) < 2:
        raise ValueError("droop curve needs matching x/y arrays of length >= 2")
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            x0, x1 = xs[i - 1], xs[i]
            y0, y1 = ys[i - 1], ys[i]
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return ys[-1]  # unreachable given the x >= xs[-1] guard


def _require_monotonic(name: str, xs: list[float], ys: list[float]) -> None:
    if any(b <= a for a, b in zip(xs, xs[1:])):
        raise ValueError(f"{name}: breakpoints must be strictly increasing")
    if any(b < a for a, b in zip(ys, ys[1:])):
        raise ValueError(f"{name}: corrections must be monotonically non-decreasing")


@dataclass(frozen=True)
class DroopCorrection:
    """Setpoint corrections to add to the configured PCC setpoint, in SI."""

    dp_kw: float
    dq_kvar: float


class DroopController:
    """Holds the validated curves and the PCC base for p.u.->SI conversion."""

    def __init__(self, cfg: DroopConfig, pcc_base_kw: float):
        if pcc_base_kw <= 0:
            raise ValueError("pcc_base_kw must be positive for p.u. conversion")
        self.cfg = cfg
        self.base = pcc_base_kw
        pf, qv = cfg.p_f_droop, cfg.q_v_droop
        _require_monotonic("p_f_droop.dP_f", pf.f, pf.dP_f)
        _require_monotonic("p_f_droop.dQ_f", pf.f, pf.dQ_f)
        _require_monotonic("q_v_droop.dP_V", qv.V, qv.dP_V)
        _require_monotonic("q_v_droop.dQ_V", qv.V, qv.dQ_V)

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled and (self.cfg.p_f_droop.enabled or self.cfg.q_v_droop.enabled)

    def correction(self, frequency_hz: float, voltage_v: float) -> DroopCorrection:
        """Combined P and Q corrections (SI). Returns zero when droop is disabled."""
        dp_pu = dq_pu = 0.0
        if self.cfg.enabled and self.cfg.p_f_droop.enabled:
            pf = self.cfg.p_f_droop
            dp_pu += interp_clamped(frequency_hz, pf.f, pf.dP_f)
            dq_pu += interp_clamped(frequency_hz, pf.f, pf.dQ_f)
        if self.cfg.enabled and self.cfg.q_v_droop.enabled:
            qv = self.cfg.q_v_droop
            dp_pu += interp_clamped(voltage_v, qv.V, qv.dP_V)
            dq_pu += interp_clamped(voltage_v, qv.V, qv.dQ_V)
        return DroopCorrection(dp_kw=dp_pu * self.base, dq_kvar=dq_pu * self.base)
