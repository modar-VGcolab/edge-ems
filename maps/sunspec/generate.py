"""Generate SunSpec-700 device images (walkable model chains) and canonical maps.

Layouts are transcribed from the official SunSpec model definitions
(sunspec/models: model_1, _701, _704, _713). A model block = ID + L header,
then its points in declared order (704's four PF sub-groups append after the
points). Scale-factor (`sunssf`) registers carry a fixed SF so the canonical
engineering value = raw * 10^SF * unit_scale (unit_scale only does W->kW etc.).

This emits, per asset:
  - <asset>.image.yaml      full walkable chain: SunS marker + model headers +
                            every register at its offset + 0xFFFF end model
  - <asset>.canonical.yaml  data_model point -> {address, type, sf_address,
                            unit_scale, rw}  (the binding the controller uses)

Run: python maps/sunspec/generate.py
"""
from __future__ import annotations

from pathlib import Path

# (name, type, size, sf_ref)  -- order IS the register order within the model.
# 'sunssf' points are scale factors; ID/L are the header.
MODELS = {
    1: [
        ("ID", "uint16", 1, None), ("L", "uint16", 1, None),
        ("Mn", "string", 16, None), ("Md", "string", 16, None),
        ("Opt", "string", 8, None), ("Vr", "string", 8, None),
        ("SN", "string", 16, None), ("DA", "uint16", 1, None),
        ("Pad", "pad", 1, None),
    ],
    701: [
        ("ID", "uint16", 1, None), ("L", "uint16", 1, None),
        ("ACType", "enum16", 1, None), ("St", "enum16", 1, None),
        ("InvSt", "enum16", 1, None), ("ConnSt", "enum16", 1, None),
        ("Alrm", "bitfield32", 2, None), ("DERMode", "bitfield32", 2, None),
        ("W", "int16", 1, "W_SF"), ("VA", "int16", 1, "VA_SF"),
        ("Var", "int16", 1, "Var_SF"), ("PF", "int16", 1, "PF_SF"),
        ("A", "int16", 1, "A_SF"), ("LLV", "uint16", 1, "V_SF"),
        ("LNV", "uint16", 1, "V_SF"), ("Hz", "uint32", 2, "Hz_SF"),
        ("TotWhInj", "uint64", 4, "TotWh_SF"), ("TotWhAbs", "uint64", 4, "TotWh_SF"),
        ("TotVarhInj", "uint64", 4, "TotVarh_SF"), ("TotVarhAbs", "uint64", 4, "TotVarh_SF"),
        ("TmpAmb", "int16", 1, "Tmp_SF"), ("TmpCab", "int16", 1, "Tmp_SF"),
        ("TmpSnk", "int16", 1, "Tmp_SF"), ("TmpTrns", "int16", 1, "Tmp_SF"),
        ("TmpSw", "int16", 1, "Tmp_SF"), ("TmpOt", "int16", 1, "Tmp_SF"),
        ("WL1", "int16", 1, "W_SF"), ("VAL1", "int16", 1, "VA_SF"),
        ("VarL1", "int16", 1, "Var_SF"), ("PFL1", "int16", 1, "PF_SF"),
        ("AL1", "int16", 1, "A_SF"), ("VL1L2", "uint16", 1, "V_SF"),
        ("VL1", "uint16", 1, "V_SF"), ("TotWhInjL1", "uint64", 4, "TotWh_SF"),
        ("TotWhAbsL1", "uint64", 4, "TotWh_SF"), ("TotVarhInjL1", "uint64", 4, "TotVarh_SF"),
        ("TotVarhAbsL1", "uint64", 4, "TotVarh_SF"),
        ("WL2", "int16", 1, "W_SF"), ("VAL2", "int16", 1, "VA_SF"),
        ("VarL2", "int16", 1, "Var_SF"), ("PFL2", "int16", 1, "PF_SF"),
        ("AL2", "int16", 1, "A_SF"), ("VL2L3", "uint16", 1, "V_SF"),
        ("VL2", "uint16", 1, "V_SF"), ("TotWhInjL2", "uint64", 4, "TotWh_SF"),
        ("TotWhAbsL2", "uint64", 4, "TotWh_SF"), ("TotVarhInjL2", "uint64", 4, "TotVarh_SF"),
        ("TotVarhAbsL2", "uint64", 4, "TotVarh_SF"),
        ("WL3", "int16", 1, "W_SF"), ("VAL3", "int16", 1, "VA_SF"),
        ("VarL3", "int16", 1, "Var_SF"), ("PFL3", "int16", 1, "PF_SF"),
        ("AL3", "int16", 1, "A_SF"), ("VL3L1", "uint16", 1, "V_SF"),
        ("VL3", "uint16", 1, "V_SF"), ("TotWhInjL3", "uint64", 4, "TotWh_SF"),
        ("TotWhAbsL3", "uint64", 4, "TotWh_SF"), ("TotVarhInjL3", "uint64", 4, "TotVarh_SF"),
        ("TotVarhAbsL3", "uint64", 4, "TotVarh_SF"),
        ("ThrotPct", "uint16", 1, None), ("ThrotSrc", "bitfield32", 2, None),
        ("A_SF", "sunssf", 1, None), ("V_SF", "sunssf", 1, None),
        ("Hz_SF", "sunssf", 1, None), ("W_SF", "sunssf", 1, None),
        ("PF_SF", "sunssf", 1, None), ("VA_SF", "sunssf", 1, None),
        ("Var_SF", "sunssf", 1, None), ("TotWh_SF", "sunssf", 1, None),
        ("TotVarh_SF", "sunssf", 1, None), ("Tmp_SF", "sunssf", 1, None),
        ("MnAlrmInfo", "string", 32, None),
    ],
    704: [
        ("ID", "uint16", 1, None), ("L", "uint16", 1, None),
        ("PFWInjEna", "enum16", 1, None), ("PFWInjEnaRvrt", "enum16", 1, None),
        ("PFWInjRvrtTms", "uint32", 2, None), ("PFWInjRvrtRem", "uint32", 2, None),
        ("PFWAbsEna", "enum16", 1, None), ("PFWAbsEnaRvrt", "enum16", 1, None),
        ("PFWAbsRvrtTms", "uint32", 2, None), ("PFWAbsRvrtRem", "uint32", 2, None),
        ("WMaxLimPctEna", "enum16", 1, None), ("WMaxLimPct", "uint16", 1, "WMaxLimPct_SF"),
        ("WMaxLimPctRvrt", "uint16", 1, "WMaxLimPct_SF"), ("WMaxLimPctEnaRvrt", "enum16", 1, None),
        ("WMaxLimPctRvrtTms", "uint32", 2, None), ("WMaxLimPctRvrtRem", "uint32", 2, None),
        ("WSetEna", "enum16", 1, None), ("WSetMod", "enum16", 1, None),
        ("WSet", "int32", 2, "WSet_SF"), ("WSetRvrt", "int32", 2, "WSet_SF"),
        ("WSetPct", "int16", 1, "WSetPct_SF"), ("WSetPctRvrt", "int16", 1, "WSetPct_SF"),
        ("WSetEnaRvrt", "enum16", 1, None), ("WSetRvrtTms", "uint32", 2, None),
        ("WSetRvrtRem", "uint32", 2, None), ("VarSetEna", "enum16", 1, None),
        ("VarSetMod", "enum16", 1, None), ("VarSetPri", "enum16", 1, None),
        ("VarSet", "int32", 2, "VarSet_SF"), ("VarSetRvrt", "int32", 2, "VarSet_SF"),
        ("VarSetPct", "int16", 1, "VarSetPct_SF"), ("VarSetPctRvrt", "int16", 1, "VarSetPct_SF"),
        ("VarSetEnaRvrt", "enum16", 1, None), ("VarSetRvrtTms", "uint32", 2, None),
        ("VarSetRvrtRem", "uint32", 2, None), ("WRmp", "uint16", 1, None),
        ("WRmpRef", "enum16", 1, None), ("VarRmp", "uint16", 1, None),
        ("AntiIslEna", "enum16", 1, None),
        ("PF_SF", "sunssf", 1, None), ("WMaxLimPct_SF", "sunssf", 1, None),
        ("WSet_SF", "sunssf", 1, None), ("WSetPct_SF", "sunssf", 1, None),
        ("VarSet_SF", "sunssf", 1, None), ("VarSetPct_SF", "sunssf", 1, None),
        # 4 PF sub-groups (each PF uint16 + Ext enum16) append after the points:
        ("PFWInj.PF", "uint16", 1, "PF_SF"), ("PFWInj.Ext", "enum16", 1, None),
        ("PFWInjRvrt.PF", "uint16", 1, "PF_SF"), ("PFWInjRvrt.Ext", "enum16", 1, None),
        ("PFWAbs.PF", "uint16", 1, "PF_SF"), ("PFWAbs.Ext", "enum16", 1, None),
        ("PFWAbsRvrt.PF", "uint16", 1, "PF_SF"), ("PFWAbsRvrt.Ext", "enum16", 1, None),
    ],
    713: [
        ("ID", "uint16", 1, None), ("L", "uint16", 1, None),
        ("WHRtg", "uint16", 1, "WH_SF"), ("WHAvail", "uint16", 1, "WH_SF"),
        ("SoC", "uint16", 1, "Pct_SF"), ("SoH", "uint16", 1, "Pct_SF"),
        ("Sta", "enum16", 1, None),
        ("WH_SF", "sunssf", 1, None), ("Pct_SF", "sunssf", 1, None),
    ],
}

# Fixed scale-factor values we publish (chosen so canonical resolution matches
# the prior maps). Engineering(SI) value = raw * 10^SF.
SF_VALUES = {
    "W_SF": 2, "Var_SF": 2, "VA_SF": 2, "A_SF": -1, "V_SF": -1, "Hz_SF": -2,
    "PF_SF": -3, "TotWh_SF": 0, "TotVarh_SF": 0, "Tmp_SF": -1,
    "WMaxLimPct_SF": 0, "WSet_SF": 0, "WSetPct_SF": 0, "VarSet_SF": 0,
    "VarSetPct_SF": 0, "WH_SF": 0, "Pct_SF": -1,
}

# SunSpec "not implemented" sentinels by type (for points we don't drive).
SENTINEL = {
    "uint16": 0xFFFF, "int16": -0x8000, "uint32": 0xFFFFFFFF, "int32": -0x80000000,
    "uint64": 0, "enum16": 0xFFFF, "bitfield32": 0, "string": 0, "pad": 0,
}
WIDTH = {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "uint64": 4,
         "enum16": 1, "bitfield32": 2, "sunssf": 1}

# ----- per-asset chain + canonical binding (data_model name -> model.point) ---
# unit_scale converts the model's SI unit to the canonical unit (W->kW = 0.001).
ASSETS = {
    "bess-01": {
        "map": "custom_bess_v1.yaml",
        "chain": [1, 701, 704, 713],
        "bind": {
            "soc_pct":                      ("713.SoC",  1.0, "r"),
            "active_power_kw":              ("701.W",    0.001, "r"),
            "reactive_power_kvar":          ("701.Var",  0.001, "r"),
            "active_power_setpoint_kw":     ("704.WSet", 0.001, "rw"),
            "reactive_power_setpoint_kvar": ("704.VarSet", 0.001, "rw"),
            "active_power_setpoint_enable":   ("704.WSetEna",   1.0, "rw"),
            "reactive_power_setpoint_enable": ("704.VarSetEna", 1.0, "rw"),
        },
        # vendor extension (no standard SunSpec point) appended after the chain:
        "vendor": {
            "available_charge_power_kw":    ("int16", -1, 1.0, "r"),
            "available_discharge_power_kw": ("int16", -1, 1.0, "r"),
        },
    },
    "pv-01": {
        "map": "custom_pv_inverter_v1.yaml",
        "chain": [1, 701, 704],
        "bind": {
            "active_power_kw":        ("701.W",   0.001, "r"),
            "reactive_power_kvar":    ("701.Var", 0.001, "r"),
            "derate_factor_setpoint": ("704.WMaxLimPct", 0.01, "rw"),
        },
    },
    "pcc-01": {
        "map": "grid_meter_v1.yaml",
        "chain": [701],
        "bind": {
            "voltage_v":           ("701.LNV", 1.0, "r"),
            "frequency_hz":        ("701.Hz",  1.0, "r"),
            "current_a":           ("701.A",   1.0, "r"),
            "active_power_kw":     ("701.W",   0.001, "r"),
            "reactive_power_kvar": ("701.Var", 0.001, "r"),
        },
    },
    # controllable load modelled like the PV inverter (1 + 701 + 704): the
    # derate_factor_setpoint binds to 704.WMaxLimPct, same as pv-01.
    "fload-01": {
        "map": "flexible_load_v1.yaml",
        "chain": [1, 701, 704],
        "bind": {
            "active_power_kw":        ("701.W",   0.001, "r"),
            "reactive_power_kvar":    ("701.Var", 0.001, "r"),
            "derate_factor_setpoint": ("704.WMaxLimPct", 0.01, "rw"),
        },
    },
    # fixed (non-controllable) load behind a passive meter: measurement only,
    # model 701 like the PCC grid meter -- no controls, no setpoints.
    "meter-01": {
        "map": "meter_v1.yaml",
        "chain": [701],
        "bind": {
            "voltage_v":           ("701.LNV", 1.0, "r"),
            "frequency_hz":        ("701.Hz",  1.0, "r"),
            "current_a":           ("701.A",   1.0, "r"),
            "active_power_kw":     ("701.W",   0.001, "r"),
            "reactive_power_kvar": ("701.Var", 0.001, "r"),
        },
    },
}

BASE = 40001  # SunSpec holding-register base ('SunS' marker at 40001-40002)


def model_length(mid):
    body = sum(w for _, t, w, _ in MODELS[mid][2:])
    return body


def build(asset):
    spec = ASSETS[asset]
    regs = []  # (addr, fqname, type, size, rw, role, value|None)
    sf_addr = {}   # (mid, sf_name) -> addr
    pt_addr = {}   # "mid.point" -> (addr, type, sf_name)
    addr = BASE
    # SunS identifier marker (2 regs = 0x53756e53)
    regs.append((addr, "SunS", "uint16", 1, "r", "marker", 0x5375))
    regs.append((addr + 1, "SunS", "uint16", 1, "r", "marker", 0x6e53))
    addr += 2
    for mid in spec["chain"]:
        pts = MODELS[mid]
        L = model_length(mid)
        for i, (name, typ, size, sfref) in enumerate(pts):
            fq = f"{mid}.{name}"
            if name == "ID":
                val = mid
            elif name == "L":
                val = L
            elif typ == "sunssf":
                val = SF_VALUES[name]
                sf_addr[(mid, name)] = addr
            else:
                val = SENTINEL.get(typ, 0)
            regs.append((addr, fq, typ, size, "r", "header" if name in ("ID", "L")
                         else ("sf" if typ == "sunssf" else "data"), val))
            pt_addr[fq] = (addr, typ, sfref, mid)
            addr += size
    # end model marker (0xFFFF id + 0 len) -- terminates the SunSpec chain
    regs.append((addr, "END", "uint16", 1, "r", "end", 0xFFFF))
    regs.append((addr + 1, "END", "uint16", 1, "r", "end", 0))
    addr += 2
    # vendor extension block AFTER the end model: read by absolute address; a
    # generic SunSpec walker stops at END and never sees these (stays walkable).
    for vname, (typ, sfv, uscale, rw) in spec.get("vendor", {}).items():
        regs.append((addr, f"vendor.{vname}", typ, WIDTH[typ], rw, "vendor", SENTINEL.get(typ, 0)))
        pt_addr[f"vendor.{vname}"] = (addr, typ, None, "vendor")
        addr += WIDTH[typ]

    # canonical bindings
    canon = {}
    for cname, (target, uscale, rw) in spec["bind"].items():
        a, typ, sfref, mid = pt_addr[target]
        entry = {"address": a, "type": typ, "rw": rw, "unit_scale": uscale}
        if sfref:
            entry["sf_address"] = sf_addr[(mid, sfref)]
        canon[cname] = entry
    for vname, (typ, sfv, uscale, rw) in spec.get("vendor", {}).items():
        a, t, _, _ = pt_addr[f"vendor.{vname}"]
        canon[vname] = {"address": a, "type": typ, "rw": rw, "unit_scale": uscale,
                        "sf_value": sfv}
    return regs, canon, addr - BASE


def emit(asset):
    """Emit ONE loadable SunSpec map: full walkable image where canonical points
    carry sf_address/unit_scale and every other register is a roled raw entry."""
    regs, canon, total = build(asset)
    spec = ASSETS[asset]
    addr2canon = {e["address"]: (cn, e) for cn, e in canon.items()}
    lines = [
        f"# SunSpec-700 register map for {asset} (GENERATED by maps/sunspec/generate.py).",
        f"# Full walkable image: SunS marker + models {spec['chain']}"
        + (" + vendor" if spec.get("vendor") else "")
        + " + 0xFFFF end model.",
        "# Canonical points carry live scale: value = raw * 10^(SF@sf_address) * unit_scale.",
        "# Raw image registers (role: marker/header/sf/data/vendor/end) are served verbatim.",
        'map_version: "3.0-sunspec"',
        f"asset_class: {SPEC_CLASS[asset]}",
        f"device: {asset}-sunspec",
        f"base_address: {BASE}",
        "points:",
    ]
    seen = set()
    for a, fq, typ, size, rw, role, val in regs:
        if a in addr2canon:
            cn, e = addr2canon[a]
            if cn in seen:
                continue
            seen.add(cn)
            extra = "".join(
                f", {k}: {v}" for k, v in e.items() if k not in ("address", "type", "rw"))
            lines.append(
                f"  {cn}: {{address: {e['address']}, type: {e['type']}, rw: {e['rw']}{extra}}}")
        else:
            name = fq
            if fq in ("SunS", "END"):
                name = f"_{fq}_{a}"
            sz = f", size: {size}" if (typ in ("string", "pad") or size != 1) else ""
            lines.append(
                f"  {name}: {{address: {a}, type: {typ}{sz}, rw: r, role: {role}, value: {val}}}")
    out = Path(__file__).parent.parent / spec["map"]   # maps/<map file>
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return total, len(regs)


def emit_split(asset):
    """Emit the two-file SunSpec representation (review/discovery artifacts):
      maps/sunspec/<asset>.image.yaml      full walkable register image
      maps/sunspec/<asset>.canonical.yaml  data_model point -> register binding
    Addresses come from build(), so these always match the combined map."""
    regs, canon, _total = build(asset)
    spec = ASSETS[asset]
    outdir = Path(__file__).parent
    img = [
        f"# Walkable SunSpec-700 image for {asset} (generated by generate.py).",
        f"# {len(regs)} registers, base {BASE}. Chain: {spec['chain']}"
        + (" + vendor" if spec.get("vendor") else "") + " + end.",
        f"device: {asset}",
        f"base_address: {BASE}",
        "registers:",
    ]
    for a, fq, typ, size, _rw, role, val in regs:
        img.append(f"  - {{addr: {a}, point: {fq}, type: {typ}, size: {size}, "
                   f"rw: r, role: {role}, value: {val}}}")
    (outdir / f"{asset}.image.yaml").write_text("\n".join(img) + "\n", encoding="utf-8")

    can = [
        f"# Canonical binding for {asset}: data_model point -> SunSpec register.",
        "# value = raw * 10^(SF@sf_address) * unit_scale  (live scale factor).",
        f"device: {asset}",
        "points:",
    ]
    for cn, e in canon.items():
        parts = [f"address: {e['address']}", f"type: {e['type']}",
                 f"rw: {e['rw']}", f"unit_scale: {e['unit_scale']}"]
        if "sf_address" in e:
            parts.append(f"sf_address: {e['sf_address']}")
        if "sf_value" in e:
            parts.append(f"sf_value: {e['sf_value']}")
        can.append(f"  {cn}: {{{', '.join(parts)}}}")
    (outdir / f"{asset}.canonical.yaml").write_text("\n".join(can) + "\n", encoding="utf-8")
    return len(regs)


SPEC_CLASS = {"bess-01": "battery", "pv-01": "pv", "pcc-01": "pcc", "fload-01": "flexible_load",
              "meter-01": "meter"}

if __name__ == "__main__":
    for mid in (1, 701, 704, 713):
        print(f"model {mid}: L={model_length(mid)} body -> {model_length(mid)+2} regs")
    for asset in ASSETS:
        total, n = emit(asset)
        ns = emit_split(asset)
        print(f"{asset}: {n} registers, span {total} -> maps/{ASSETS[asset]['map']} "
              f"(+ maps/sunspec/{asset}.image.yaml & {asset}.canonical.yaml, {ns} regs)")
