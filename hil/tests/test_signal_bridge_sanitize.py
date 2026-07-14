"""Signal-bridge measurement sanitiser (E3 bridge-robustness fix).

`_read_meas` must stop an out-of-range plant transient from crashing the Modbus
encode (the islanding finding: a negative-frequency transient hit the unsigned
SunSpec 701.Hz register and took down the bridge), WITHOUT masking a genuine
grid collapse (0 V / 0 Hz must survive so the controller's plausibility gate
trips). These call the method on a stub so no Typhoon/rig is needed.
"""

from hil.schematic.signal_bridge import TyphoonSignalBridge


class _Stub:
    """Minimal stand-in exposing just the `_read` that `_read_meas` calls."""

    def __init__(self, value: float):
        self._value = value

    def _read(self, name: str) -> float:
        return self._value


def _meas(value, **kw):
    return TyphoonSignalBridge._read_meas(_Stub(value), "sig", **kw)


def test_negative_floored_to_zero():
    # the grid-loss negative-frequency transient must never reach an unsigned encode
    assert _meas(-3.2, floor=0.0) == 0.0


def test_nan_and_inf_become_zero():
    assert _meas(float("nan"), floor=0.0) == 0.0
    assert _meas(float("inf")) == 0.0
    assert _meas(float("-inf"), floor=0.0) == 0.0


def test_zero_passes_through_for_island_detection():
    # a real collapse (0 V / 0 Hz) must survive so the controller gate can trip
    assert _meas(0.0, floor=0.0) == 0.0


def test_signed_power_preserved_without_floor():
    assert _meas(-150.0) == -150.0
    assert _meas(150.0) == 150.0


def test_plausible_values_unchanged():
    assert _meas(50.0, floor=0.0) == 50.0
    assert _meas(230.0, floor=0.0) == 230.0
