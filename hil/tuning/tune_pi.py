"""Tune Kp/Ki against the HIL plant dynamics, and check anti-windup recovery.

The default gains (Kp=0.5, Ki=0.1) were chosen for SIL where the "plant" is an
ideal register source; against the real plant dynamics (converter lag + setpoint
slew limit + one-cycle measurement delay) they settle too slowly and the
tracking scenario's <5 kW settle band is missed. This harness sweeps a gain grid
through the plant-in-the-loop, scores each on the tracking scenario (settle
error, settling time, overshoot, tail oscillation), and recommends a stable gain
set that meets the band with margin.

It also runs a *deep-saturation anti-windup* probe: drive a load far beyond the
battery's discharge limit so the PI pins and the integrator is at risk of
winding up, then release the load and measure how many cycles the battery takes
to return to its steady command. The controller uses conditional-integration
anti-windup (it freezes the integrator only when saturated AND the error pushes
further into the limit); the probe confirms recovery is prompt. If a chosen gain
set recovered slowly, that is the signal to revisit back-calculation -- reported,
not silently worked around.

No controller code is changed here: tuning is config (Kp/Ki). Run:

    python -m hil.tuning.tune_pi              # sweep + recommend + probe
    python -m hil.tuning.tune_pi --write      # also write tuned defaults to configs
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from hil.chil_runner import PlantInTheLoop
from hil.plant.models import SiteModel

_REPO = Path(__file__).resolve().parents[2]
SETTLE_BAND_KW = 5.0


@dataclass
class GainScore:
    kp: float
    ki: float
    worst_tail_kw: float      # worst |error| over the last 10 cycles
    settling_time_s: float    # first time after which |error| stays < band
    overshoot_kw: float       # worst battery command magnitude beyond steady
    tail_pstdev_kw: float     # tail oscillation
    settles: bool

    @property
    def cost(self) -> float:
        # Prefer settling, then speed, then low overshoot/oscillation.
        base = 0.0 if self.settles else 1e6
        return base + self.settling_time_s + 0.05 * self.overshoot_kw + self.tail_pstdev_kw


def _run_tracking(kp: float, ki: float, load_kw: float = 250.0, duration_s: int = 30):
    site = SiteModel()
    site.battery.soc_pct = 50.0
    pil = PlantInTheLoop(
        site, ems_overrides={"controller": {"Kp": kp, "Ki": ki}}
    )
    series = pil.run(
        duration_s,
        stimulus=lambda t, s: (s.load.set_base_kw(load_kw), s.pv.set_irradiance(0.0)),
    )
    return series, pil


def score_gains(kp: float, ki: float) -> GainScore:
    series, _ = _run_tracking(kp, ki)
    errs = [abs(r["pcc_error_kw"]) for r in series]
    dt = 1.0
    tail = errs[-10:]
    worst_tail = max(tail)
    # settling time: last index where error exceeds band, +1 cycle.
    settle_idx = 0
    for i, e in enumerate(errs):
        if e >= SETTLE_BAND_KW:
            settle_idx = i + 1
    settling_time = settle_idx * dt
    steady = -250.0
    pis = [r["pi_output_kw"] for r in series]
    overshoot = max(0.0, max(abs(p) for p in pis) - abs(steady))
    tail_std = (sum((e - sum(tail) / len(tail)) ** 2 for e in tail) / len(tail)) ** 0.5
    return GainScore(kp, ki, worst_tail, settling_time, overshoot, tail_std,
                     settles=worst_tail < SETTLE_BAND_KW)


def sweep(kps=None, kis=None) -> list[GainScore]:
    kps = kps or [0.5, 0.8, 1.0, 1.2, 1.5]
    kis = kis or [0.1, 0.2, 0.3, 0.5, 0.8, 1.0]
    scores = [score_gains(kp, ki) for kp in kps for ki in kis]
    return sorted(scores, key=lambda s: s.cost)


@dataclass
class WindupProbe:
    kp: float
    ki: float
    recovery_cycles: int       # cycles to return within band after release
    peak_integral_term_kw: float
    recovered: bool


def antiwindup_probe(kp: float, ki: float) -> WindupProbe:
    """Saturate hard (2000 kW import vs 1000 kW discharge limit), then release to
    a small load and measure recovery."""
    site = SiteModel()
    site.battery.soc_pct = 60.0
    pil = PlantInTheLoop(site, ems_overrides={"controller": {"Kp": kp, "Ki": ki}})

    def stim(t, s: SiteModel):
        s.pv.set_irradiance(0.0)
        s.load.set_base_kw(2000.0 if t < 15 else 100.0)  # deep sat, then release

    series = pil.run(45, stimulus=stim)
    # integral term contribution = pi_output - Kp*error
    peak_int = max(abs(r["pi_output_kw"] - kp * r["pcc_error_kw"]) for r in series)
    release_idx = 15
    recovery = 0
    for i in range(release_idx, len(series)):
        if abs(series[i]["pcc_error_kw"]) < SETTLE_BAND_KW:
            recovery = i - release_idx
            break
    else:
        recovery = len(series) - release_idx
    recovered = abs(series[-1]["pcc_error_kw"]) < SETTLE_BAND_KW
    return WindupProbe(kp, ki, recovery, peak_int, recovered)


def recommend() -> dict:
    scores = sweep()
    best = scores[0]
    probe = antiwindup_probe(best.kp, best.ki)
    baseline = score_gains(0.5, 0.1)
    return {"baseline": baseline, "best": best, "probe": probe, "ranked": scores}


def _fmt_score(s: GainScore) -> str:
    return (f"Kp={s.kp:<4} Ki={s.ki:<4} settles={str(s.settles):<5} "
            f"worst_tail={s.worst_tail_kw:6.2f} kW  settle={s.settling_time_s:4.0f}s  "
            f"overshoot={s.overshoot_kw:6.1f} kW  tail_std={s.tail_pstdev_kw:5.2f}")


def _write_config(kp: float, ki: float) -> Path:
    """Update the controller Kp/Ki defaults in configs/edge_ems_config.example.yaml."""
    import re
    path = _REPO / "configs" / "edge_ems_config.example.yaml"
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(\n\s*Kp:\s*)[-\d.]+", rf"\g<1>{kp}", text, count=1)
    text = re.sub(r"(\n\s*Ki:\s*)[-\d.]+", rf"\g<1>{ki}", text, count=1)
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="HIL PI tuning")
    ap.add_argument("--write", action="store_true",
                    help="write the recommended Kp/Ki to configs/edge_ems_config.example.yaml")
    args = ap.parse_args()

    r = recommend()
    print("baseline:  " + _fmt_score(r["baseline"]))
    print("recommend: " + _fmt_score(r["best"]))
    p = r["probe"]
    print(f"anti-windup probe @ recommended: recovery={p.recovery_cycles} cycles, "
          f"peak |integral term|={p.peak_integral_term_kw:.1f} kW, recovered={p.recovered}")
    print("\ntop 5 gain sets:")
    for s in r["ranked"][:5]:
        print("  " + _fmt_score(s))

    if args.write:
        path = _write_config(r["best"].kp, r["best"].ki)
        print(f"\nwrote tuned defaults Kp={r['best'].kp}, Ki={r['best'].ki} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
