"""The seven SIL validation scenarios (plan task 26 / Gate G2).

Each scenario is a named spec with: the simulator/controller setup hints the live
runner uses to create the condition, and a pure `check` over the `control`
measurement series it produced. Keeping `check` pure (a list of cycle dicts ->
asserts) means the assertions are unit-testable without any infrastructure
(see test_sil_assertions.py); the live runner in test_sil.py only has to fetch
the series and call `check`.

A control-series row mirrors the `control` measurement fields:
    {pcc_error_kw, pi_output_kw, derate_factor, curtail_factor,
     loop_duration_ms, mode}
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field

# Loop timing budget (system design 4): 250 ms of the 1 s period.
LOOP_BUDGET_MS = 250.0


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    duration_s: int
    setup: dict  # hints for the live runner (profile, droop flag, asset count...)
    check: Callable[[list[dict]], None]
    tags: tuple[str, ...] = field(default_factory=tuple)


def _modes(series: list[dict]) -> list[str]:
    return [row["mode"] for row in series]


def _settled(series: list[dict], tail: int = 10) -> list[dict]:
    return series[-tail:] if len(series) >= tail else series


def _require(condition: bool, msg: str) -> None:
    if not condition:
        raise AssertionError(msg)


# 1 ------------------------------------------------------------------ tracking
def _check_tracking(series: list[dict]) -> None:
    _require(series, "no control samples captured")
    tail = _settled(series)
    worst = max(abs(r["pcc_error_kw"]) for r in tail)
    _require(worst < 5.0, f"PCC error did not settle: worst |error|={worst:.2f} kW")
    _require(all(r["mode"] == "RUN" for r in tail), "controller left RUN while tracking")


# 2 ------------------------------------------------------- saturation -> derate
def _check_saturation_derate(series: list[dict]) -> None:
    _require(series, "no control samples captured")
    _require(
        any(r["derate_factor"] < 1.0 - 1e-9 for r in series),
        "flexible load was never derated under battery saturation",
    )
    _require(
        all(r["derate_factor"] >= 0.0 for r in series),
        "derate factor went negative",
    )


# 3 ------------------------------------------------------------- PV curtailment
def _check_curtailment(series: list[dict]) -> None:
    _require(series, "no control samples captured")
    _require(
        any(r["curtail_factor"] < 1.0 - 1e-9 for r in series),
        "PV was never curtailed despite export over the feed limit",
    )
    _require(all(0.0 <= r["curtail_factor"] <= 1.0 for r in series), "curtail factor out of [0,1]")


# 4 -------------------------------------------------------------------- droop
def _check_droop(series: list[dict]) -> None:
    _require(series, "no control samples captured")
    # With droop active and a frequency/voltage excursion injected, the PI output
    # must actually move (a flat response means droop did nothing).
    outputs = [r["pi_output_kw"] for r in series]
    _require(statistics.pstdev(outputs) > 1.0, "PI output flat: droop produced no response")
    _require(any(r["mode"] == "RUN" for r in series), "never ran while testing droop")


# 5 ----------------------------------------------------------------- stale data
def _check_stale_data(series: list[dict]) -> None:
    modes = _modes(series)
    _require("HOLD" in modes, "did not enter HOLD on stale data")
    _require("SAFE" in modes, "did not escalate to SAFE after hold_max_s")
    _require(modes.index("HOLD") < modes.index("SAFE"), "SAFE must follow HOLD, not precede it")


# 6 --------------------------------------------------------------- config reload
def _check_config_reload(series: list[dict]) -> None:
    _require(series, "no control samples captured")
    _require(
        all(r["mode"] == "RUN" for r in series),
        "loop left RUN across a config reload (reload must be seamless)",
    )
    _require(
        all(r["loop_duration_ms"] < LOOP_BUDGET_MS for r in series),
        "loop overran its budget during reload",
    )


# 7 ---------------------------------------------------------------- 50-asset scale
def _check_scale(series: list[dict]) -> None:
    _require(series, "no control samples captured")
    worst = max(r["loop_duration_ms"] for r in series)
    _require(
        worst < LOOP_BUDGET_MS, f"loop exceeded {LOOP_BUDGET_MS:.0f} ms at scale: {worst:.1f} ms"
    )
    p95 = sorted(r["loop_duration_ms"] for r in series)[max(0, int(len(series) * 0.95) - 1)]
    _require(p95 < LOOP_BUDGET_MS, f"p95 loop duration over budget: {p95:.1f} ms")


SCENARIOS: list[Scenario] = [
    Scenario(
        "tracking",
        "PCC self-consumption tracking settles to zero error",
        30,
        # Grid PCC ramps to ~0 so the tracked error settles (no plant coupling in SIL).
        {"sim_profiles": {"sim-grid": "track_settle.csv"}},
        _check_tracking,
        ("core",),
    ),
    Scenario(
        "saturation_derate",
        "Low-SoC battery saturation derates flexible load",
        40,
        {"profile": "profiles/drain_battery.csv"},
        _check_saturation_derate,
        ("ladder",),
    ),
    Scenario(
        "curtailment",
        "PV export over feed limit with full battery is curtailed",
        40,
        # Grid exports past the 999 kVA feed limit while the battery sits above its
        # high-SoC warning band, so the controller must curtail PV (edge_controller t7).
        {"sim_profiles": {"sim-grid": "export_over_feed.csv", "sim-bess": "high_soc.csv"}},
        _check_curtailment,
        ("ladder",),
    ),
    Scenario(
        "droop",
        "P-f/Q-V droop responds to frequency/voltage excursions",
        30,
        {"sim_profiles": {"sim-grid": "freq_excursion.csv"}, "droop": True},
        _check_droop,
        ("droop",),
    ),
    Scenario(
        "stale_data",
        "Stale aggregate drives RUN -> HOLD -> SAFE",
        20,
        {"fault": "influx_stall"},
        _check_stale_data,
        ("modes",),
    ),
    Scenario(
        "config_reload",
        "Hot config reload is seamless (stays RUN)",
        25,
        # Reload the SIL site config (sim-* hosts); the example config points at
        # real device IPs that are unreachable inside the compose network.
        {"reload": "configs/asset_config.sil.yaml"},
        _check_config_reload,
        ("config",),
    ),
    Scenario(
        "scale_50",
        "50 simulated assets keep the loop within budget",
        30,
        {"asset_count": 50},
        _check_scale,
        ("perf",),
    ),
]


def by_name(name: str) -> Scenario:
    for s in SCENARIOS:
        if s.name == name:
            return s
    raise KeyError(name)
