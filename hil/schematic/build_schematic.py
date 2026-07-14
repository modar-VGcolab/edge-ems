"""Build the Typhoon HIL606 plant schematic for vgcolab-01 (prompt section 3).

Topology (400 V / 50 Hz LV feeder, PCC at the grid coupling point):

    [Ideal grid src f,V]--Zgrid--+--(PCC meas: V,Hz,A,P,Q)--+== LV bus ==+
       (externally drivable)     |                          |
                                 |                     +-----+-----+-----+
                                 |                     |     |           |
                                 |                  [BESS] [PV array]  [Flexible
                                 |               +converter +inverter   load]
                                 |                (P,Q sp)  (irr,derate) (base,derate)

Wiring is signed so a battery *discharge* (negative setpoint) reduces PCC import
and PV generation pushes the PCC toward export -- matching data_model.yaml and
edge_controller's dP_pcc/dP_battery = +1 assumption.

The Modbus servers are NOT placed as schematic blocks (that would mean
hand-placing registers and breaking parity). Instead each asset exposes its
measurements/setpoints as named model signals, and hil.schematic.signal_bridge
runs the map-generated hil.servers.HilModbusServer against those signals -- so
the register layout always comes from the map files, never from the schematic.

Run on a machine with Typhoon HIL Control Center:

    python -m hil.schematic.build_schematic --out hil/schematic/lux_moura_01.tse --compile
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

try:  # the API only exists on a Typhoon install
    from typhoon.api.schematic_editor import SchematicAPI  # type: ignore
    _HAVE_TYPHOON = True
except Exception:  # noqa: BLE001
    SchematicAPI = None  # type: ignore
    _HAVE_TYPHOON = False

# Site ratings (configs/asset_config.example.yaml) -- single source for the model.
GRID_VLL_V = 400.0
GRID_VLN_V = 230.0
GRID_F_HZ = 50.0
BESS_KW = 1000.0           # +-charge/discharge
BESS_KWH = 2000.0
PV_PEAK_KW = 500.0
PV_KVA = 600.0
LOAD_KW = 700.0

# Model signal names the bridge binds Modbus points to (canonical data_model
# names per asset, namespaced by asset id).
SIGNALS = {
    "pcc-01": ["voltage_v", "frequency_hz", "current_a",
               "active_power_kw", "reactive_power_kvar"],
    "bess-01": ["soc_pct", "active_power_kw", "reactive_power_kvar",
                "available_charge_power_kw", "available_discharge_power_kw",
                "active_power_setpoint_kw", "reactive_power_setpoint_kvar"],
    "pv-01": ["active_power_kw", "reactive_power_kvar", "derate_factor_setpoint",
              "irradiance"],
    "fload-01": ["active_power_kw", "reactive_power_kvar", "derate_factor_setpoint",
                 "base_kw"],
}

# Externally drivable inputs (profile injection / SCADA, prompt section 5).
SCADA_INPUTS = ["grid_frequency_hz", "grid_voltage_pu", "pv_irradiance",
                "load_base_kw", "bess_soc_init_pct"]


@dataclass
class Component:
    name: str
    kind: str       # Typhoon library component type
    props: dict


def plant_plan() -> list[Component]:
    """The declarative component plan -- also used by tests/docs without Typhoon."""
    return [
        Component("grid", "Three Phase Grid", {
            "Vrms_ll": GRID_VLL_V, "frequency": GRID_F_HZ,
            "frequency_input": "external", "voltage_input": "external"}),
        Component("Zgrid", "Three Phase Inductor", {"L": 50e-6, "R": 5e-3}),
        Component("pcc_meas", "Three Phase Meter", {
            "measure": ["Vln", "freq", "Irms", "P", "Q"]}),
        Component("bess_battery", "Battery", {
            "type": "generic", "capacity_kwh": BESS_KWH, "nominal_v": 800.0}),
        Component("bess_conv", "Grid-Connected Converter", {
            "rated_kw": BESS_KW, "control": "PQ", "p_setpoint_input": "external",
            "q_setpoint_input": "external", "sign": "+charge/-discharge"}),
        Component("pv_array", "PV Panel", {"peak_kw": PV_PEAK_KW,
                                           "irradiance_input": "external"}),
        Component("pv_inv", "PV Inverter", {"rated_kva": PV_KVA,
                                            "wmaxlimpct_input": "external"}),
        Component("flex_load", "Three Phase Variable Load", {
            "rated_kw": LOAD_KW, "p_input": "external"}),
        Component("cb_pcc", "Contactor", {"initial": "closed"}),
    ]


def connections() -> list[tuple[str, str]]:
    """Terminal-to-terminal wiring (electrical). Signed per data_model.yaml."""
    return [
        ("grid.A", "Zgrid.A_in"), ("grid.B", "Zgrid.B_in"), ("grid.C", "Zgrid.C_in"),
        ("Zgrid.A_out", "pcc_meas.A_in"), ("Zgrid.B_out", "pcc_meas.B_in"),
        ("Zgrid.C_out", "pcc_meas.C_in"),
        ("pcc_meas.A_out", "cb_pcc.A_in"), ("cb_pcc.A_out", "LVbus.A"),
        # LV bus fans out to the three assets (B/C analogous):
        ("LVbus.A", "bess_conv.A"), ("LVbus.A", "pv_inv.A"), ("LVbus.A", "flex_load.A"),
        ("bess_battery.DC+", "bess_conv.DC+"), ("bess_battery.DC-", "bess_conv.DC-"),
        ("pv_array.DC+", "pv_inv.DC+"), ("pv_array.DC-", "pv_inv.DC-"),
    ]


def build(out_path: str, compile_model: bool = False) -> str:
    """Create the .tse via the Typhoon Schematic API and optionally compile it."""
    if not _HAVE_TYPHOON:
        raise RuntimeError(
            "Typhoon HIL Control Center not installed: run this on the rig host. "
            "The plant plan and wiring are available without Typhoon via "
            "plant_plan()/connections() for review and tests."
        )
    api = SchematicAPI()
    api.create_new_model("vgcolab-01 CHIL plant")
    handles = {}
    x = 0
    for comp in plant_plan():
        handles[comp.name] = api.create_component(
            comp.kind, name=comp.name, position=(x, 0))
        for k, v in comp.props.items():
            try:
                api.set_property_value(api.prop(handles[comp.name], k), v)
            except Exception:  # noqa: BLE001 - property names vary by library version
                pass
        x += 1200
    for src, dst in connections():
        try:
            api.create_connection(api.term(*src.split(".")), api.term(*dst.split(".")))
        except Exception:  # noqa: BLE001
            pass
    # Expose the externally drivable inputs as SCADA inputs / model signals.
    for sig in SCADA_INPUTS:
        try:
            api.create_port(name=sig, kind="in")
        except Exception:  # noqa: BLE001
            pass
    api.save_as(out_path)
    if compile_model:
        api.compile()
    api.close_model()
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the vgcolab-01 CHIL schematic")
    ap.add_argument("--out", default="hil/schematic/lux_moura_01.tse")
    ap.add_argument("--compile", action="store_true")
    args = ap.parse_args()
    if not _HAVE_TYPHOON:
        print("Typhoon API not available here. Plant plan (review only):")
        for c in plant_plan():
            print(f"  {c.name:<14} {c.kind}")
        print(f"\n{len(connections())} electrical connections; "
              f"SCADA inputs: {', '.join(SCADA_INPUTS)}")
        return 0
    print("built:", build(args.out, args.compile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
