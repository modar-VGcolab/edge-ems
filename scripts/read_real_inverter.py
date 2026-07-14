"""Bench tool for real-hardware bring-up. Two modes:

  discover  Walk a live device (or a saved raw register dump) as a generic
            SunSpec client: verify the SunS marker, walk the model chain to the
            0xFFFF end model, and record every model's placement, point
            offsets/types, and the **actual** sunssf scale-factor values. Emits a
            machine-readable dump (JSON/YAML) and can diff it against a map. This
            is what closes the G3 firmware-reconciliation blocker -- see
            docs/prompts/sunspec-firmware-reconciliation.md.

  read      Read every input point through a given map + the EMS adapter and flag
            implausible values (the original bring-up smoke check).

Run from the repo root, on a machine that can reach the inverter:

    # dump a real device and diff it against the candidate map
    python scripts/read_real_inverter.py discover --host 192.168.1.22 \
        --map custom_bess_v1.yaml --out dumps/bess-01.dump.json

    # or work from a saved raw register dump (no device needed)
    python scripts/read_real_inverter.py discover --from-dump dumps/bess-01.raw.json \
        --map custom_bess_v1.yaml

    # original read-through-map smoke check
    python scripts/read_real_inverter.py read --host 192.168.1.22 --map maps/custom_bess_v1.yaml

If values look wrong (scale off by 10, sign flipped, wrong register), the fix is
the *generator input* (maps/sunspec/generate.py SF_VALUES / placement), then
regenerate -- never the controller and never a hand-edit of the emitted YAML.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

PLAUSIBLE = {
    "%": (0.0, 100.0),
    "Hz": (45.0, 65.0),
    "V": (180.0, 280.0),
}


def cmd_discover(args: argparse.Namespace) -> int:
    import sunspec_discovery as sd

    catalog = sd.load_model_catalog(REPO_ROOT)
    if args.from_dump:
        reader = sd.RawDumpReader.from_file(args.from_dump)
        source = {"type": "raw-dump", "path": str(args.from_dump)}
    elif args.host:
        reader = sd.LiveReader(args.host, port=args.port, unit_id=args.unit_id)
        source = {"type": "live", "host": args.host, "port": args.port, "unit_id": args.unit_id}
    else:
        print("discover: provide --host or --from-dump", file=sys.stderr)
        return 2

    try:
        dump = sd.walk(reader, catalog, base_address=args.base,
                       include_values=args.values, source=source)
    except sd.SunSError as exc:
        print(f"NOT A SUNSPEC DEVICE: {exc}", file=sys.stderr)
        return 2
    finally:
        if hasattr(reader, "close"):
            reader.close()

    print(f"walked {len(dump['models'])} models: {dump['model_chain']}")
    for m in dump["models"]:
        sf = "  ".join(f"{k}={v:+d}" for k, v in m["scale_factors"].items())
        print(f"  model {m['id']:>4} @ {m['base_address']}  L={m['header_length']}"
              + (f"   SF: {sf}" if sf else ""))
    for note in dump["notes"]:
        print(f"  note: {note}")

    if args.out:
        out = sd.write_dump(dump, args.out)
        print(f"dump written -> {out}")

    rc = 0
    if args.map:
        import diff_dump_vs_map as ddm

        report = ddm.diff(dump, REPO_ROOT, args.map)
        print("\n" + ddm.render(report))
        if args.diff_json:
            import json

            Path(args.diff_json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        rc = 0 if report.get("ok") else 1
    return rc


async def _run_read(args: argparse.Namespace) -> int:
    from common.data_model import DataModel
    from common.register_map import load_register_map
    from core.adapters.modbus_tcp import ModbusTcpAdapter

    dm = DataModel.load(args.data_model)
    rmap = load_register_map(args.map, dm)
    cls_ = dm.asset_classes[rmap.asset_class]
    input_names = [
        n for n in rmap.points if n in cls_.points and cls_.points[n].direction == "input"
    ]

    adapter = ModbusTcpAdapter(args.host, rmap, port=args.port, unit_id=args.unit_id)
    warnings = 0
    for cycle in range(args.loop):
        values = await adapter.read_points(input_names)
        print(f"--- cycle {cycle + 1}/{args.loop} ({args.host}:{args.port}) ---")
        for name in input_names:
            pv = values[name]
            unit = cls_.points[name].unit
            if pv.quality != "GOOD":
                print(f"  {name:32s}  <{pv.quality}>")
                warnings += 1
                continue
            flag = ""
            bounds = PLAUSIBLE.get(unit)
            if bounds and not bounds[0] <= pv.value <= bounds[1]:
                flag = "  <-- implausible, check scale/register"
                warnings += 1
            print(f"  {name:32s} {pv.value:12.3f} {unit}{flag}")
        if cycle + 1 < args.loop:
            await asyncio.sleep(args.period)
    await adapter.disconnect()
    print(f"done: {warnings} warning(s). health={adapter.health()}")
    return 1 if warnings else 0


def cmd_read(args: argparse.Namespace) -> int:
    return asyncio.run(_run_read(args))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="read/discover a real device through its register map")
    sub = p.add_subparsers(dest="mode")

    d = sub.add_parser("discover", help="SunSpec model discovery -> machine-readable dump")
    d.add_argument("--host", help="live device host")
    d.add_argument("--from-dump", help="saved raw register dump (JSON: {base, words})")
    d.add_argument("--port", type=int, default=502)
    d.add_argument("--unit-id", type=int, default=1)
    d.add_argument("--base", type=int, default=40001)
    d.add_argument("--map",
                   help="map filename under maps/ to diff against (e.g. custom_bess_v1.yaml)")
    d.add_argument("--out", help="write the device dump here (.json/.yaml)")
    d.add_argument("--diff-json", help="write the diff report JSON here")
    d.add_argument("--values", action="store_true", help="also record every point's raw value")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("read", help="read input points through a map and flag implausible values")
    r.add_argument("--host", required=True)
    r.add_argument("--port", type=int, default=502)
    r.add_argument("--unit-id", type=int, default=1)
    r.add_argument("--map", required=True)
    r.add_argument("--data-model", default=str(REPO_ROOT / "data_model.yaml"))
    r.add_argument("--loop", type=int, default=1, help="number of read cycles")
    r.add_argument("--period", type=float, default=1.0, help="seconds between cycles")
    r.set_defaults(func=cmd_read)

    args = p.parse_args(argv)
    if args.mode is None:
        p.print_help()
        sys.exit(2)
    return args


def main() -> None:
    args = parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
