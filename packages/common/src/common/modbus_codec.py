"""Encode/decode between engineering values and Modbus register words.

Shared by the real ModbusTcpAdapter (core) and the device simulator so both
sides of every test speak from the same register map by construction.

Two scaling modes:
  * legacy folded `scale`:           value = raw * scale
  * SunSpec live/fixed scale factor: value = raw * 10^SF * unit_scale, where SF
    is read at runtime from `sf_address` (a sunssf register) or fixed `sf_value`.

Addresses in map files use the conventional 4xxxx numbering (40001 -> offset 0).
"""

from __future__ import annotations

import struct

from common.register_map import RegisterDef

HOLDING_BASE = 40001

_SIGNED = {"int16", "sunssf", "int32"}
_BITS = {"uint16": 16, "int16": 16, "enum16": 16, "sunssf": 16,
         "uint32": 32, "int32": 32, "bitfield32": 32, "uint64": 64}
# The register types that decode to a numeric value. A full walkable SunSpec
# image also carries 'string' (identity: manufacturer, model, serial, version)
# and 'pad' registers, which are structural rather than telemetry -- callers
# selecting points to poll should filter on this set.
NUMERIC_TYPES = frozenset(_BITS) | {"float32"}

_RANGE = {
    "uint16": (0, 0xFFFF), "enum16": (0, 0xFFFF), "sunssf": (-0x8000, 0x7FFF),
    "int16": (-0x8000, 0x7FFF), "uint32": (0, 0xFFFFFFFF), "bitfield32": (0, 0xFFFFFFFF),
    "int32": (-0x80000000, 0x7FFFFFFF), "uint64": (0, (1 << 64) - 1),
}


def holding_offset(address: int) -> int:
    if address < HOLDING_BASE:
        raise ValueError(f"address {address} is not a holding register (4xxxx range)")
    return address - HOLDING_BASE


def _words_to_int(typ: str, words: list[int]) -> int:
    raw = 0
    for w in words:  # big-endian word order
        raw = (raw << 16) | (w & 0xFFFF)
    try:
        bits = _BITS[typ]
    except KeyError:  # e.g. SunSpec 'string'/'pad' -- present in a walkable image
        raise ValueError(
            f"register type '{typ}' is not numeric and cannot be decoded to a value "
            f"(numeric types: {', '.join(sorted(NUMERIC_TYPES))}). SunSpec identity "
            f"strings and padding are part of the walkable image, not telemetry -- "
            f"filter them out before reading."
        ) from None
    if typ in _SIGNED and raw >= (1 << (bits - 1)):
        raw -= 1 << bits
    return raw


def _int_to_words(typ: str, raw: int, width: int) -> list[int]:
    raw &= (1 << (width * 16)) - 1
    return [(raw >> (16 * (width - 1 - i))) & 0xFFFF for i in range(width)]


def _sf_factor(reg: RegisterDef, sf: int | None) -> float:
    exp = sf if sf is not None else (reg.sf_value if reg.sf_value is not None else 0)
    return 10.0 ** exp


def encode(reg: RegisterDef, value: float, sf: int | None = None) -> list[int]:
    """Engineering value -> register words (big-endian word order)."""
    if reg.type == "float32":
        return list(struct.unpack(">HH", struct.pack(">f", float(value) / reg.scale)))
    if reg.sunspec_scaled:
        raw = round(float(value) / reg.unit_scale / _sf_factor(reg, sf))
    else:
        raw = round(float(value) / reg.scale)
    if reg.type in _RANGE:
        lo, hi = _RANGE[reg.type]
        if not lo <= raw <= hi:
            raise ValueError(f"{value} does not fit {reg.type} (raw {raw} outside [{lo},{hi}])")
    return _int_to_words("uint64" if reg.type in ("string", "pad") else reg.type, raw, reg.width)


def encode_raw(reg: RegisterDef, raw: int) -> list[int]:
    """Pack a literal register value (image init: markers, headers, SF, sentinels)."""
    return _int_to_words(reg.type if reg.type != "float32" else "uint32", int(raw), reg.width)


def decode(reg: RegisterDef, words: list[int], sf: int | None = None) -> float:
    """Register words -> engineering value."""
    if len(words) != reg.width:
        raise ValueError(f"expected {reg.width} words for {reg.type}, got {len(words)}")
    if reg.type == "float32":
        return float(struct.unpack(">f", struct.pack(">HH", *words))[0]) * reg.scale
    raw = _words_to_int(reg.type, words)
    if reg.sunspec_scaled:
        return raw * _sf_factor(reg, sf) * reg.unit_scale
    return raw * reg.scale
