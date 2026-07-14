"""Droop controller tests (plan task 22).

Interpolation at knots / inside the deadband / past the limits, p.u.->SI
conversion on the PCC base, enable-flag gating, and rejection of non-monotonic
curves.
"""

import pytest
from common.config_models import DroopConfig, PfDroop, QvDroop
from controller.droop import DroopController, interp_clamped


def _pf(enabled=True, dP_f=None, dQ_f=None, f=None):
    return PfDroop(
        enabled=enabled,
        dP_f=dP_f or [-1.0, 0.0, 0.0, 1.0],
        dQ_f=dQ_f or [0.0, 0.0, 0.0, 0.0],
        f=f or [49.0, 49.5, 50.5, 51.0],
    )


def _qv(enabled=True, dP_V=None, dQ_V=None, V=None):
    return QvDroop(
        enabled=enabled,
        dP_V=dP_V or [0.0, 0.0, 0.0, 0.0],
        dQ_V=dQ_V or [-1.0, 0.0, 0.0, 1.0],
        V=V or [210.0, 225.0, 235.0, 250.0],
    )


def _cfg(enabled=True, pf=None, qv=None):
    return DroopConfig(enabled=enabled, p_f_droop=pf or _pf(), q_v_droop=qv or _qv())


# --------------------------------------------------------------- interpolation


def test_interp_at_knots():
    xs, ys = [0.0, 1.0, 2.0, 3.0], [10.0, 20.0, 20.0, 40.0]
    for x, y in zip(xs, ys):
        assert interp_clamped(x, xs, ys) == pytest.approx(y)


def test_interp_midsegment():
    assert interp_clamped(0.5, [0.0, 1.0], [0.0, 10.0]) == pytest.approx(5.0)


def test_interp_flat_extrapolation():
    xs, ys = [10.0, 20.0], [1.0, 2.0]
    assert interp_clamped(5.0, xs, ys) == pytest.approx(1.0)  # below range
    assert interp_clamped(99.0, xs, ys) == pytest.approx(2.0)  # above range


def test_interp_flat_deadband():
    xs, ys = [49.0, 49.5, 50.5, 51.0], [-1.0, 0.0, 0.0, 1.0]
    assert interp_clamped(50.0, xs, ys) == pytest.approx(0.0)  # inside deadband


def test_interp_rejects_mismatched_arrays():
    with pytest.raises(ValueError):
        interp_clamped(1.0, [0.0, 1.0], [0.0])


# --------------------------------------------------------------- pu -> SI


def test_pf_correction_converts_to_si_on_base():
    d = DroopController(_cfg(qv=_qv(enabled=False)), pcc_base_kw=1000.0)
    # f = 51.0 -> dP = 1.0 p.u. -> 1000 kW
    c = d.correction(frequency_hz=51.0, voltage_v=230.0)
    assert c.dp_kw == pytest.approx(1000.0)
    assert c.dq_kvar == pytest.approx(0.0)


def test_pf_correction_midsegment():
    d = DroopController(_cfg(qv=_qv(enabled=False)), pcc_base_kw=500.0)
    # f = 49.25 -> halfway -1.0..0.0 -> -0.5 p.u. -> -250 kW
    c = d.correction(frequency_hz=49.25, voltage_v=230.0)
    assert c.dp_kw == pytest.approx(-250.0)


def test_qv_correction_drives_reactive():
    d = DroopController(_cfg(pf=_pf(enabled=False)), pcc_base_kw=1000.0)
    # V = 250 -> dQ = 1.0 p.u. -> 1000 kVAr
    c = d.correction(frequency_hz=50.0, voltage_v=250.0)
    assert c.dq_kvar == pytest.approx(1000.0)
    assert c.dp_kw == pytest.approx(0.0)


def test_pf_and_qv_sum():
    d = DroopController(_cfg(), pcc_base_kw=1000.0)
    c = d.correction(frequency_hz=51.0, voltage_v=250.0)
    assert c.dp_kw == pytest.approx(1000.0)  # from p-f
    assert c.dq_kvar == pytest.approx(1000.0)  # from q-v


# --------------------------------------------------------------- enable gating


def test_disabled_top_level_returns_zero():
    d = DroopController(_cfg(enabled=False), pcc_base_kw=1000.0)
    c = d.correction(frequency_hz=51.0, voltage_v=250.0)
    assert c.dp_kw == 0.0 and c.dq_kvar == 0.0
    assert not d.enabled


def test_deadband_center_is_zero_when_enabled():
    d = DroopController(_cfg(), pcc_base_kw=1000.0)
    c = d.correction(frequency_hz=50.0, voltage_v=230.0)
    assert c.dp_kw == pytest.approx(0.0)
    assert c.dq_kvar == pytest.approx(0.0)


# --------------------------------------------------------------- validation


def test_rejects_non_monotonic_correction():
    # pydantic blocks non-monotonic dP at construction of the model itself
    with pytest.raises(ValueError):
        _pf(dP_f=[1.0, 0.0, 0.0, -1.0])


def test_rejects_nonpositive_base():
    with pytest.raises(ValueError):
        DroopController(_cfg(), pcc_base_kw=0.0)
