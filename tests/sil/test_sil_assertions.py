"""Unit tests for the SIL scenario assertions (run everywhere, no infra needed).

Feeds each scenario.check a synthetic control series that should pass and one
that should fail, so the SIL oracle itself is trustworthy before it gates G2.
"""

import pytest
from scenarios import SCENARIOS, by_name


def _row(mode="RUN", err=0.0, pi=0.0, derate=1.0, curtail=1.0, dur=50.0):
    return {
        "pcc_error_kw": err,
        "pi_output_kw": pi,
        "derate_factor": derate,
        "curtail_factor": curtail,
        "loop_duration_ms": dur,
        "mode": mode,
    }


def test_all_scenarios_have_unique_names():
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names)) == 7


def test_tracking_pass_and_fail():
    by_name("tracking").check([_row(err=0.1) for _ in range(20)])
    with pytest.raises(AssertionError):
        by_name("tracking").check([_row(err=50.0) for _ in range(20)])
    with pytest.raises(AssertionError):
        by_name("tracking").check([_row(mode="HOLD", err=0.1) for _ in range(20)])


def test_saturation_derate_pass_and_fail():
    series = [_row() for _ in range(10)] + [_row(derate=0.5) for _ in range(5)]
    by_name("saturation_derate").check(series)
    with pytest.raises(AssertionError):
        by_name("saturation_derate").check([_row() for _ in range(15)])


def test_curtailment_pass_and_fail():
    by_name("curtailment").check([_row(curtail=0.7) for _ in range(5)])
    with pytest.raises(AssertionError):
        by_name("curtailment").check([_row() for _ in range(5)])


def test_droop_pass_and_fail():
    moving = [_row(pi=v) for v in (-50, -10, 0, 20, 60, -30)]
    by_name("droop").check(moving)
    with pytest.raises(AssertionError):
        by_name("droop").check([_row(pi=0.0) for _ in range(6)])


def test_stale_data_pass_and_fail():
    series = [_row() for _ in range(3)] + [_row(mode="HOLD")] * 5 + [_row(mode="SAFE")] * 3
    by_name("stale_data").check(series)
    with pytest.raises(AssertionError):  # never escalates
        by_name("stale_data").check([_row(mode="HOLD")] * 8)


def test_config_reload_pass_and_fail():
    by_name("config_reload").check([_row(dur=120.0) for _ in range(20)])
    with pytest.raises(AssertionError):  # left RUN
        by_name("config_reload").check([_row(mode="HOLD")] * 5)
    with pytest.raises(AssertionError):  # overran budget
        by_name("config_reload").check([_row(dur=400.0) for _ in range(5)])


def test_scale_pass_and_fail():
    by_name("scale_50").check([_row(dur=180.0) for _ in range(100)])
    with pytest.raises(AssertionError):
        by_name("scale_50").check([_row(dur=300.0) for _ in range(100)])


def test_empty_series_fails_loudly():
    for s in SCENARIOS:
        if s.name in ("stale_data",):  # mode-list based: handled separately
            continue
        with pytest.raises(AssertionError):
            s.check([])
