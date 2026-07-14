"""SunSpec device discovery -- walk an as-built device and emit a register dump.

This is the tooling that closes the G3 firmware-reconciliation blocker. The maps
in ``maps/*.yaml`` are *generated* from the official SunSpec model definitions
with **chosen** scale factors and **assumed** model placement. Because the HIL
servers and the controller read the same map files, a wrong address/type/scale
is invisible in SIL/CHIL and only breaks on the real inverter. To catch that you
need the device's *own* truth:

  1. walk the live device (or a saved raw register dump),
  2. record what it actually exposes -- model chain, point offsets/types, and the
     **real** ``sunssf`` scale-factor values,
  3. diff that against the generated map (see ``diff_dump_vs_map.py``).

Nothing here invents firmware data. With no device and no saved dump you can only
produce a *self-dump* of our own map image (``dump_from_map``), which is useful
for testing the walker/diff but proves nothing about real hardware (parity by
construction). A real reconciliation needs ``RawDumpReader``/``LiveReader`` fed
from an actual asset.

Dump schema: ``sunspec-device-dump/v1`` (see ``_DUMP_SCHEMA``).
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

_DUMP_SCHEMA = "sunspec-device-dump/v1"

HOLDING_BASE = 40001
SUNS_MARKER = (0x5375, 0x6E53)  # 'SunS'
END_MODEL_ID = 0xFFFF

# String/pad point widths come from the model catalog; everything else from type.
_TYPE_WIDTH = {
    "uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2,
    "enum16": 1, "bitfield32": 2, "uint64": 4, "sunssf": 1,
}
_SIGNED = {"int16", "int32", "sunssf"}
_BITS = {"uint16": 16, "int16": 16, "enum16": 16, "sunssf": 16, "uint32": 32,
         "int32": 32, "bitfield32": 32, "uint64": 64}


def _decode_int(typ: str, words: list[int]) -> int:
    raw = 0
    for w in words:  # big-endian word order
        raw = (raw << 16) | (w & 0xFFFF)
    bits = _BITS.get(typ, 16 * len(words))
    if typ in _SIGNED and raw >= (1 << (bits - 1)):
        raw -= 1 << bits
    return raw


# Model catalog (official SunSpec model point layouts), reused from
# maps/sunspec/generate.py so the walker and the generator share one source of
# model definitions. Device-specific facts (placement, SF, implemented
# points/lengths) come from the *device*, not from here.
def load_model_catalog(repo_root: str | Path) -> dict[int, list[tuple]]:
    """Return ``{model_id: [(name, type, size, sf_ref), ...]}`` from generate.py."""
    gen = Path(repo_root) / "maps" / "sunspec" / "generate.py"
    spec = importlib.util.spec_from_file_location("_sunspec_generate", gen)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load model catalog from {gen}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return dict(mod.MODELS)


def _point_width(typ: str, size: int) -> int:
    if typ in ("string", "pad"):
        return size
    return _TYPE_WIDTH.get(typ, size)


def _catalog_body_len(points: list[tuple]) -> int:
    """Body length (registers after the ID/L header) for a catalog model."""
    return sum(_point_width(t, s) for _, t, s, _ in points[2:])


# Register readers -- the device-facing seam.
class RegisterReader(Protocol):
    def read(self, offset: int, count: int) -> list[int]:
        """Read ``count`` holding-register words at zero-based ``offset``."""
        ...


class RawDumpReader:
    """Reader over a saved raw register dump (offset/word pairs or a base+list).

    Accepts either ``{"base": 40001, "words": {"40001": 21365, ...}}`` (absolute
    4xxxx addresses) or ``{"base": 40001, "words": [w0, w1, ...]}`` (sequential
    from base). This is the artifact a field tech captures from a real asset.
    """

    def __init__(self, words: dict[int, int]):
        self._words = words

    @classmethod
    def from_obj(cls, obj: dict) -> "RawDumpReader":
        base = int(obj.get("base", HOLDING_BASE))
        raw = obj["words"]
        if isinstance(raw, dict):
            words = {int(k): int(v) & 0xFFFF for k, v in raw.items()}
        else:  # sequential list from base
            words = {base + i: int(v) & 0xFFFF for i, v in enumerate(raw)}
        return cls(words)

    @classmethod
    def from_file(cls, path: str | Path) -> "RawDumpReader":
        return cls.from_obj(json.loads(Path(path).read_text(encoding="utf-8")))

    def read(self, offset: int, count: int) -> list[int]:
        out = []
        for i in range(count):
            addr = HOLDING_BASE + offset + i
            if addr not in self._words:
                raise KeyError(f"raw dump has no word at address {addr}")
            out.append(self._words[addr])
        return out


class LiveReader:
    """Synchronous pymodbus reader for a live device. Imported lazily so the
    library (and its tests) don't require pymodbus unless you talk to hardware."""

    def __init__(self, host: str, port: int = 502, unit_id: int = 1, timeout: float = 3.0):
        from pymodbus.client import ModbusTcpClient  # lazy

        self._client = ModbusTcpClient(host, port=port, timeout=timeout)
        self._unit_id = unit_id
        if not self._client.connect():
            raise ConnectionError(f"could not connect to {host}:{port}")

    def read(self, offset: int, count: int) -> list[int]:
        # pymodbus caps a single request at 125 registers; chunk to be safe.
        out: list[int] = []
        i = 0
        while i < count:
            n = min(120, count - i)
            rr = self._client.read_holding_registers(offset + i, count=n, device_id=self._unit_id)
            if rr.isError():
                raise OSError(f"read at offset {offset + i} (+{n}) failed: {rr}")
            out.extend(rr.registers)
            i += n
        return out

    def close(self) -> None:
        self._client.close()


def reader_from_map(repo_root: str | Path, map_file: str):
    """A reader backed by our own generated map image (a *self-dump*).

    Builds a SimulatedDevice from the map and reads its datastore. Useful for
    testing the walker/diff and for demonstrating the round-trip end-to-end, but
    it carries our chosen SF by construction -- NOT a substitute for a real dump.
    """
    from common.data_model import DataModel
    from common.register_map import load_register_map
    from simulator.device import SimulatedDevice

    root = Path(repo_root)
    dm = DataModel.load(root / "data_model.yaml")
    rmap = load_register_map(root / "maps" / map_file, dm)
    dev = SimulatedDevice(rmap)
    store = dev.context[0]

    class _SimReader:
        def read(self, offset: int, count: int) -> list[int]:
            return list(store.getValues(3, offset, count))

    return _SimReader()


class SunSError(ValueError):
    """Raised when the device does not present a SunSpec image at the base."""


def walk(
    reader: RegisterReader,
    catalog: dict[int, list[tuple]],
    base_address: int = HOLDING_BASE,
    max_models: int = 64,
    include_values: bool = False,
    source: dict | None = None,
) -> dict:
    """Walk a SunSpec model chain and return a device-dump dict.

    Verifies the ``SunS`` identifier at ``base_address``, then walks
    ``(model-id, length)`` headers to the ``0xFFFF`` end model. For each model it
    records base address, id, header length (= version-implied length), the
    catalog body length, every point's offset/type, and the **actual** value of
    each ``sunssf`` register.
    """
    base_off = base_address - HOLDING_BASE
    marker = tuple(reader.read(base_off, 2))
    suns_ok = marker == SUNS_MARKER
    if not suns_ok:
        raise SunSError(
            f"no SunS marker at {base_address}: got {marker!r}, expected {SUNS_MARKER!r} "
            f"(not a SunSpec image -- e.g. a legacy/vendor map)"
        )

    models: list[dict] = []
    notes: list[str] = []
    off = base_off + 2  # first model header sits right after the marker
    end_address = None
    for _ in range(max_models):
        mid, length = reader.read(off, 2)
        model_base_addr = HOLDING_BASE + off
        if mid == END_MODEL_ID:
            end_address = model_base_addr
            break
        points = catalog.get(mid)
        known = points is not None
        model: dict = {
            "index": len(models),
            "id": mid,
            "base_address": model_base_addr,
            "header_length": length,
            "known": known,
            "catalog_length": None,
            "length_matches_catalog": None,
            "points": [],
            "scale_factors": {},
        }
        if known:
            cat_len = _catalog_body_len(points)
            model["catalog_length"] = cat_len
            model["length_matches_catalog"] = cat_len == length
            if cat_len != length:
                notes.append(
                    f"model {mid} @ {model_base_addr}: device length {length} != "
                    f"catalog length {cat_len} (different model version / optional points)"
                )
            # Lay out points from the catalog, reading only as far as the device
            # says the model extends (header_length + 2 for the ID/L header).
            point_off = off
            model_end = off + 2 + length
            for name, typ, size, sf_ref in points:
                width = _point_width(typ, size)
                if point_off + width > model_end:
                    notes.append(
                        f"model {mid}: catalog point '{name}' lies beyond the device's "
                        f"declared length -- device omits it"
                    )
                    break
                addr = HOLDING_BASE + point_off
                entry = {
                    "name": name,
                    "offset": point_off - off,  # offset within the model
                    "address": addr,
                    "type": typ,
                }
                if typ in ("string", "pad"):
                    entry["size"] = size
                if typ == "sunssf":
                    val = _decode_int("sunssf", reader.read(point_off, 1))
                    entry["value"] = val
                    model["scale_factors"][name] = val
                elif name in ("ID", "L") or include_values:
                    rtyp = typ if typ not in ("string", "pad") else "uint16"
                    entry["value"] = _decode_int(rtyp, reader.read(point_off, width))
                model["points"].append(entry)
                point_off += width
        else:
            notes.append(
                f"model {mid} @ {model_base_addr}: not in catalog -- recorded header only "
                f"(length {length}); confirm against the SunSpec model definition"
            )
        models.append(model)
        off += 2 + length
    else:
        notes.append(f"walk stopped after {max_models} models without reaching the end model")

    return {
        "schema": _DUMP_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source or {"type": "unknown"},
        "base_address": base_address,
        "suns_marker_ok": suns_ok,
        "register_span": (end_address - base_address + 2) if end_address else None,
        "end_model_address": end_address,
        "model_chain": [(m["id"], m["header_length"]) for m in models],
        "models": models,
        "notes": notes,
    }


def dump_from_map(repo_root: str | Path, map_file: str, include_values: bool = False) -> dict:
    """Self-dump: walk our own generated map image. (Carries our chosen SF.)"""
    catalog = load_model_catalog(repo_root)
    reader = reader_from_map(repo_root, map_file)
    return walk(reader, catalog, include_values=include_values,
                source={"type": "map-image", "map": map_file,
                        "warning": "self-dump of our own map -- not real firmware"})


def raw_dump_from_map(repo_root: str | Path, map_file: str) -> dict:
    """Produce a *raw register* dump ({base, words}) from our own map image.

    This is the shape a field tech captures from a real asset (a flat read of the
    holding registers). Round-tripping it through ``RawDumpReader`` exercises the
    exact ingestion path a real saved dump takes. Being our own image, it carries
    our chosen SF -- fine for tests, not a real reconciliation.
    """
    from common.data_model import DataModel
    from common.register_map import load_register_map
    from simulator.device import SimulatedDevice

    root = Path(repo_root)
    dm = DataModel.load(root / "data_model.yaml")
    rmap = load_register_map(root / "maps" / map_file, dm)
    dev = SimulatedDevice(rmap)
    store = dev.context[0]
    span = max((r.address - HOLDING_BASE) + r.width for r in rmap.points.values())
    words = {HOLDING_BASE + off: int(store.getValues(3, off, 1)[0]) for off in range(span + 2)}
    return {"base": HOLDING_BASE, "words": words}


def write_dump(dump: dict, path: str | Path) -> Path:
    """Write a dump as JSON (.json) or YAML (.yaml/.yml)."""
    p = Path(path)
    if p.suffix in (".yaml", ".yml"):
        import yaml

        p.write_text(yaml.safe_dump(dump, sort_keys=False), encoding="utf-8")
    else:
        p.write_text(json.dumps(dump, indent=2), encoding="utf-8")
    return p
