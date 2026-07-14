"""Verify the signal_bridge bindings against a running Typhoon model.

Off-rig it still does something useful: it collects every name the bridge will
read or write and prints them grouped, so you can eyeball the map.

On the rig (Typhoon API present + model loaded) it additionally queries the
model's signal list and flags any binding that does NOT resolve -- i.e. every
`# VERIFY` name you must fix before the first closed-loop run.

    python -m hil.schematic.verify_signals            # collect + (if on rig) check
    python -m hil.schematic.verify_signals --list     # just print the bindings

Exit code is non-zero if any binding is unresolved (rig only), so it can gate a
bring-up script.

NOTE: Typhoon 2026.x exposes the HIL API as MODULE-LEVEL functions
(`import typhoon.api.hil as hil_api`), not as an importable `hil` object.
Reads are checked against `available_analog_signals()`; writable setpoints
against `get_scada_inputs()` / `available_sources()`. A model must be LOADED
(hil_api.load_model(...)) for those lists to be populated.
"""

from __future__ import annotations

import argparse

from hil.schematic import signal_bridge as sb

try:
    import typhoon.api.hil as hil_api  # type: ignore
    _HAVE_TYPHOON = True
    _IMPORT_ERR = ""
except Exception as e:  # noqa: BLE001
    hil_api = None  # type: ignore
    _HAVE_TYPHOON = False
    _IMPORT_ERR = repr(e)

# Keys in SIGNALS that are probe OUTPUTS (read from the model).
_READ_KEYS = {
    "voltage_v", "frequency_hz", "current_a",
    "active_power_kw", "reactive_power_kvar", "soc_pct",
}


def collect_bindings() -> tuple[dict[str, str], dict[str, str]]:
    """Return (reads, writes): label -> model signal name, from the bridge maps."""
    reads: dict[str, str] = {}
    writes: dict[str, str] = {}
    for asset, pts in sb.SIGNALS.items():
        for key, name in pts.items():
            if not name or name.startswith("("):
                continue  # computed headroom, not a model signal
            label = f"{asset}.{key}"
            if key in _READ_KEYS:
                reads[label] = name
            else:
                writes[label] = name
    for channel, name in sb.CHANNEL_SIGNAL.items():
        if name:
            writes[f"channel.{channel}"] = name
    return reads, writes


def _flatten_names(obj) -> set[str]:
    out: set[str] = set()
    if obj is None:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            out |= _flatten_names(v)
    elif isinstance(obj, (list, tuple, set)):
        for x in obj:
            out |= _flatten_names(x)
    else:
        out.add(str(obj))
    return out


def _collect(getter_name: str) -> set[str]:
    fn = getattr(hil_api, getter_name, None)
    if not callable(fn):
        return set()
    try:
        return _flatten_names(fn())
    except Exception:  # noqa: BLE001
        return set()


# Modern (non-deprecated) getters. available_analog_signals()/available_sources()
# are deprecated in 2026.x and can return a bool instead of a name list.
_READ_GETTERS = ("get_analog_signals", "get_scada_outputs", "get_streaming_analog_signals")
_WRITE_GETTERS = ("get_scada_inputs",)


def _readable_names() -> set[str]:
    names: set[str] = set()
    for g in _READ_GETTERS:
        names |= _collect(g)
    return names


def _writable_names() -> set[str]:
    names: set[str] = set()
    for g in _WRITE_GETTERS:
        names |= _collect(g)
    return names


def _print_group(title: str, mapping: dict[str, str],
                 available: set[str] | None) -> int:
    print(f"\n[{title}]  ({len(mapping)})")
    missing = 0
    for label, name in mapping.items():
        if available is None:
            mark = "  "
        elif name in available:
            mark = "OK"
        else:
            mark = "!!"
            missing += 1
        print(f"  {mark} {label:<34} <-> {name}")
    return missing


def main() -> int:
    ap = argparse.ArgumentParser(description="verify signal_bridge bindings")
    ap.add_argument("--list", action="store_true",
                    help="only print the bindings; never query the rig")
    ap.add_argument("--dump", action="store_true",
                    help="print the model's full readable/writable signal lists and exit")
    args = ap.parse_args()

    if args.dump:
        if not _HAVE_TYPHOON:
            print(f"Typhoon API not importable: {_IMPORT_ERR}")
            return 2
        rd = sorted(_readable_names())
        wr = sorted(_writable_names())
        print(f"=== READABLE signals ({len(rd)}) ===")
        for n in rd:
            print("  " + n)
        print(f"\n=== WRITABLE SCADA inputs ({len(wr)}) ===")
        for n in wr:
            print("  " + n)
        return 0

    reads, writes = collect_bindings()

    readable = writable = None
    if not args.list:
        if not _HAVE_TYPHOON:
            print(f"Typhoon API not importable: {_IMPORT_ERR}\n"
                  "Printing bindings only -- cannot confirm names.")
        else:
            readable = _readable_names()
            writable = _writable_names()
            total = len(readable) + len(writable)
            if total == 0:
                print("Typhoon imported, but the model exposes 0 signals -- is a "
                      "model LOADED? Run hil_api.load_model(...) first.\n"
                      "Printing bindings only.")
                readable = writable = None
            else:
                print(f"Model exposes {len(readable)} readable + {len(writable)} "
                      "writable signal names; checking bindings.")

    missing = 0
    missing += _print_group("READ  (probe outputs)", reads, readable)
    missing += _print_group("WRITE (SCADA inputs)", writes, writable)

    if readable is not None or writable is not None:
        print(f"\n{missing} unresolved binding(s)."
              + ("" if missing else "  All bindings resolve."))
        if missing:
            print("Fix the '!!' names in hil/schematic/signal_bridge.py "
                  "(the # VERIFY items) to match the model's signal list.")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
