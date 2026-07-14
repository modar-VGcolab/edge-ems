"""Diff a SunSpec device dump against a generated map.

Consumes a dump from ``sunspec_discovery.walk`` (a real device or a saved raw
dump) and compares it to what ``maps/<file>.yaml`` expects:

  * model presence / order / version (header length),
  * per-point offset & type,
  * **scale-factor values** (the device's real ``sunssf`` vs our chosen SF), and
  * each canonical binding the controller reads/writes (address, type, governing
    SF) -- plus the vendor/non-walkable points that need manual confirmation.

It only *reports*. Reconciliation is a human edit to ``maps/sunspec/generate.py``
(put the device's real SF in ``SF_VALUES``, model real placement/lengths) followed
by a regenerate -- never a hand-edit of the emitted YAML.

The "expected" side is built by self-dumping the map image, so the comparison is
dump-vs-dump and reuses the exact code the controller/simulator/HIL share.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling module

import sunspec_discovery as sd  # noqa: E402


def _index_points(model: dict) -> dict[int, dict]:
    """Map each point's within-model offset -> point entry."""
    return {p["offset"]: p for p in model["points"]}


def diff(device_dump: dict, repo_root: str | Path, map_file: str) -> dict:
    """Compare a device dump to the generated map. Returns a structured report."""
    from common.data_model import DataModel
    from common.register_map import load_register_map

    root = Path(repo_root)
    dm = DataModel.load(root / "data_model.yaml")
    rmap = load_register_map(root / "maps" / map_file, dm)
    canon_names = set(dm.asset_classes[rmap.asset_class].points)

    expected = sd.dump_from_map(root, map_file)
    exp_models = expected["models"]
    dev_models = device_dump.get("models", [])

    report: dict = {
        "map": map_file,
        "device_source": device_dump.get("source"),
        "chain": {"expected": expected["model_chain"], "device": device_dump.get("model_chain")},
        "model_issues": [],
        "scale_factor_issues": [],
        "point_issues": [],
        "canonical": [],
        "discrepancies": [],
    }

    def flag(category: str, msg: str) -> None:
        report[category].append(msg)
        report["discrepancies"].append(msg)

    if not device_dump.get("suns_marker_ok", False):
        report["discrepancies"].append("device has no SunS marker -- not a SunSpec image")
        report["ok"] = False
        return report

    # address-of-expected-point -> (model_id, point), over ALL expected models,
    # so "address not here" reliably means vendor/non-walkable.
    exp_addr_index: dict[int, tuple[int, dict]] = {}
    for m in exp_models:
        for ep in m["points"]:
            exp_addr_index[ep["address"]] = (m["id"], ep)

    n = max(len(exp_models), len(dev_models))
    for i in range(n):
        e = exp_models[i] if i < len(exp_models) else None
        d = dev_models[i] if i < len(dev_models) else None
        if e and not d:
            flag("model_issues", f"position {i}: expected model {e['id']} missing on device")
            continue
        if d and not e:
            flag("model_issues", f"position {i}: device exposes extra model {d['id']} not in map")
            continue
        if e["id"] != d["id"]:
            flag("model_issues",
                 f"position {i}: model order differs -- map has {e['id']}, device has {d['id']}")
            continue
        if e["header_length"] != d["header_length"]:
            flag("model_issues",
                 f"model {e['id']}: length/version differs -- "
                 f"map {e['header_length']}, device {d['header_length']}")

        d_by_off = _index_points(d)
        for ep in e["points"]:
            dp = d_by_off.get(ep["offset"])
            if dp is None:
                flag("point_issues",
                     f"model {e['id']} {ep['name']}@+{ep['offset']}: missing on device")
                continue
            if dp["type"] != ep["type"]:
                flag("point_issues",
                     f"model {e['id']} {ep['name']}@+{ep['offset']}: type differs -- "
                     f"map {ep['type']}, device {dp['type']}")

        for sf_name, exp_val in e["scale_factors"].items():
            dev_val = d["scale_factors"].get(sf_name)
            if dev_val is None:
                flag("scale_factor_issues",
                     f"model {e['id']} {sf_name}: scale factor missing on device")
            elif dev_val != exp_val:
                flag("scale_factor_issues",
                     f"model {e['id']} {sf_name}: SF differs -- "
                     f"map {exp_val:+d}, device {dev_val:+d} "
                     f"(engineering values off by 10^{dev_val - exp_val})")

    # canonical bindings the controller actually uses
    dev_addr_index: dict[int, tuple[dict, dict]] = {}
    for d in dev_models:
        for p in d["points"]:
            dev_addr_index[p["address"]] = (d, p)

    for cname, reg in rmap.points.items():
        if cname not in canon_names:
            continue
        addr = reg.address
        entry: dict = {"point": cname, "address": addr, "map_type": reg.type, "status": "ok"}
        if addr not in exp_addr_index:
            entry["status"] = "manual"
            entry["note"] = "vendor/non-walkable point -- confirm against the device by hand"
            report["canonical"].append(entry)
            continue
        mid, ep = exp_addr_index[addr]
        entry["model"] = mid
        entry["model_point"] = ep["name"]
        dev = dev_addr_index.get(addr)
        if dev is None:
            entry["status"] = "MISMATCH"
            entry["note"] = "device has no point at this address"
            report["discrepancies"].append(f"canonical {cname}@{addr}: device has no point there")
            report["canonical"].append(entry)
            continue
        _dmodel, dpoint = dev
        if dpoint["type"] != reg.type:
            entry["status"] = "MISMATCH"
            entry["note"] = f"type map={reg.type} device={dpoint['type']}"
            report["discrepancies"].append(
                f"canonical {cname}@{addr}: type map={reg.type} device={dpoint['type']}")
        if reg.sf_address is not None:
            exp_model = next(m for m in exp_models if m["id"] == mid)
            sf_ep = next((p for p in exp_model["points"] if p["address"] == reg.sf_address), None)
            if sf_ep is not None:
                sf_name = sf_ep["name"]
                exp_sf = exp_model["scale_factors"].get(sf_name)
                dev_model = next((m for m in dev_models if m["id"] == mid), {})
                dev_sf = dev_model.get("scale_factors", {}).get(sf_name)
                entry["sf_name"] = sf_name
                entry["map_sf"] = exp_sf
                entry["device_sf"] = dev_sf
                if dev_sf is not None and dev_sf != exp_sf:
                    entry["status"] = "MISMATCH"
                    entry["note"] = (entry.get("note", "")
                                     + f" SF {sf_name} map={exp_sf:+d} device={dev_sf:+d}").strip()
        elif reg.sf_value is not None:
            entry["map_sf"] = reg.sf_value
            entry["note"] = (entry.get("note", "") + " fixed sf_value (no live register)").strip()
        report["canonical"].append(entry)

    report["ok"] = not report["discrepancies"]
    report["summary"] = {
        "model_issues": len(report["model_issues"]),
        "scale_factor_issues": len(report["scale_factor_issues"]),
        "point_issues": len(report["point_issues"]),
        "canonical_mismatch": sum(1 for c in report["canonical"] if c["status"] == "MISMATCH"),
        "canonical_manual": sum(1 for c in report["canonical"] if c["status"] == "manual"),
    }
    return report


def render(report: dict) -> str:
    lines = [f"SunSpec reconciliation diff -- {report['map']}"]
    src = report.get("device_source") or {}
    warn = f"  ! {src['warning']}" if src.get("warning") else ""
    lines.append(f"  device source: {src.get('type', 'unknown')}{warn}")
    lines.append(f"  chain  map:    {report['chain']['expected']}")
    lines.append(f"  chain  device: {report['chain']['device']}")
    lines.append(f"  result: {'MATCH' if report.get('ok') else 'DISCREPANCIES'}")
    s = report.get("summary", {})
    if s:
        lines.append(f"  ({s['model_issues']} model, {s['scale_factor_issues']} scale-factor, "
                     f"{s['point_issues']} point issues; "
                     f"{s['canonical_mismatch']} canonical mismatch, "
                     f"{s['canonical_manual']} need manual confirmation)")
    if report["discrepancies"]:
        lines.append("\n  discrepancies:")
        lines += [f"    - {d}" for d in report["discrepancies"]]
    lines.append("\n  canonical bindings:")
    marks = {"ok": "[ ok ]", "manual": "[ ?? ]", "MISMATCH": "[FAIL]"}
    for c in report["canonical"]:
        sf = ""
        if "map_sf" in c:
            sf = f"  SF {c.get('sf_name', 'fixed')} map={c['map_sf']}"
            if c.get("device_sf") is not None:
                sf += f" device={c['device_sf']}"
        note = f"  [{c['note']}]" if c.get("note") else ""
        lines.append(f"    {marks[c['status']]} {c['point']:32s} "
                     f"@{c['address']} {c['map_type']:8s}{sf}{note}")
    return "\n".join(lines)


def _load_device_dump(args, repo_root: Path) -> dict:
    catalog = sd.load_model_catalog(repo_root)
    if args.from_dump:
        reader = sd.RawDumpReader.from_file(args.from_dump)
        source = {"type": "raw-dump", "path": str(args.from_dump)}
    elif args.host:
        reader = sd.LiveReader(args.host, port=args.port, unit_id=args.unit_id)
        source = {"type": "live", "host": args.host, "port": args.port, "unit_id": args.unit_id}
    elif args.self_dump:
        return sd.dump_from_map(repo_root, args.map)
    else:
        raise SystemExit("provide one of --from-dump / --host / --self-dump")
    return sd.walk(reader, catalog, base_address=args.base, source=source)


def main() -> None:
    p = argparse.ArgumentParser(description="diff a SunSpec device dump against a generated map")
    p.add_argument("--map", required=True, help="map filename under maps/")
    p.add_argument("--from-dump", help="saved raw register dump (JSON)")
    p.add_argument("--host", help="live device host")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--unit-id", type=int, default=1)
    p.add_argument("--base", type=int, default=sd.HOLDING_BASE)
    p.add_argument("--self-dump", action="store_true",
                   help="diff the map against its own image (demo; always matches)")
    p.add_argument("--json", help="also write the structured report to this path")
    p.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    args = p.parse_args()

    root = Path(args.repo_root)
    device_dump = _load_device_dump(args, root)
    report = diff(device_dump, root, args.map)
    print(render(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    sys.exit(0 if report.get("ok") else 1)


if __name__ == "__main__":
    main()
