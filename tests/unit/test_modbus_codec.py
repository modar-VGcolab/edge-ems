import pytest
from common.modbus_codec import NUMERIC_TYPES, decode, encode, holding_offset
from common.register_map import RegisterDef


def _reg(**kw) -> RegisterDef:
    base = {"address": 40001, "type": "int16", "scale": 0.1}
    base.update(kw)
    return RegisterDef.model_validate(base)


def test_holding_offset():
    assert holding_offset(40001) == 0
    assert holding_offset(40101) == 100
    with pytest.raises(ValueError):
        holding_offset(30001)


@pytest.mark.parametrize(
    ("rtype", "scale", "value"),
    [
        ("int16", 0.1, -312.5),  # battery discharging — the sign convention case
        ("int16", 0.1, 312.5),
        ("int16", 1.0, -1),
        ("uint16", 0.1, 95.0),  # SoC
        ("uint16", 0.001, 0.85),  # derate factor
        ("uint32", 1.0, 70000),
        ("int32", 0.1, -123456.7),
        ("float32", 1.0, -312.5),
    ],
)
def test_roundtrip(rtype, scale, value):
    reg = _reg(type=rtype, scale=scale)
    assert decode(reg, encode(reg, value)) == pytest.approx(value, abs=scale)


def test_negative_int16_word_form():
    reg = _reg(type="int16", scale=0.1)
    words = encode(reg, -312.5)  # raw -3125
    assert words == [0xFFFF & -3125]
    assert words[0] > 0x8000  # stored as two's complement


def test_uint16_rejects_negative():
    reg = _reg(type="uint16", scale=0.1)
    with pytest.raises(ValueError):
        encode(reg, -5.0)


def test_int16_range_check():
    reg = _reg(type="int16", scale=0.1)
    with pytest.raises(ValueError):
        encode(reg, 5000.0)  # raw 50000 > int16


def test_decode_word_count_check():
    reg = _reg(type="int32", scale=1.0)
    with pytest.raises(ValueError):
        decode(reg, [1])


# -- non-numeric register types ----------------------------------------------
#
# A full walkable SunSpec image carries 'string' identity registers and padding.
# They are structural, not telemetry: decoding one used to raise a bare
# KeyError from deep inside _words_to_int, which read as a codec bug rather
# than "you asked for a point that has no numeric value".


def test_string_type_raises_a_legible_error():
    reg = _reg(type="string", size=8, scale=1.0)
    with pytest.raises(ValueError, match="not numeric"):
        decode(reg, [0] * 8)


def test_numeric_types_excludes_string_and_pad():
    assert "string" not in NUMERIC_TYPES
    assert "pad" not in NUMERIC_TYPES


def test_numeric_types_covers_the_decodable_set():
    for typ in ("uint16", "int16", "enum16", "sunssf", "uint32", "int32",
                "bitfield32", "uint64", "float32"):
        assert typ in NUMERIC_TYPES, typ
