"""Bind Typhoon model signals to the map-generated Modbus servers (parity glue).

This bridge is the only thing between the Typhoon plant (analog model signals)
and the controller (Modbus registers). It is deliberately dumb except for the
ONE thing it must get right: the sign/scale conversion between the Typhoon DER
blocks and data_model.yaml.

  Convention clash (this is the whole reason the bridge is not a plain copy):
  the Typhoon DER blocks (Battery ESS, PV Plant) use GENERATOR convention --
  positive power = injected into the grid. data_model.yaml uses LOAD convention
  -- positive = consumed by the asset. So, matched to BP09_ext_v1.tse:

    battery  : block Pmeas/Pref positive = discharge(inject); data_model
               +charge. -> NEGATE both measurement and setpoint.
    pv       : block Pmeas positive = generation; data_model pv <= 0.
               -> NEGATE measurement. Curtailment is a [0,1] magnitude limit
               (no sign), written to the PV Pcurtailment input in pu.
    pcc      : Three-phase "Power Meter" wired Grid -> bus, so positive =
               grid->bus = +import. data_model pcc +import. -> +1
               (verify the Power Meter polarity on the rig; flip PCC_P_SIGN if not).
    load     : fload positive = consumption. data_model load >= 0. -> +1.

  Scale: DER *_kW / *_kVAr probes are already in kW/kVAr. The generic block power
  references (Pref/Qref/Pcurtailment/Pref_bal) are in PER-UNIT, so setpoints are
  divided by the asset's nominal kW. The Three-phase "Power Meter" POWER_P/POWER_Q
  probes are in W/VAr -> multiplied by 1e-3 (set PCC_POWER_SCALE=1.0 if kW).

  available_charge/discharge_power_kw are NOT block outputs; they are computed
  here from SoC vs the configured limits (the BMS headroom the controller reads).

Signal-name mapping (adapted to BP09_ext_v1.tse, 2026-06-24)
------------------------------------------------------------
The model exposes signals through TWO different access patterns, confirmed from
the .tse/.cus and the Typhoon block references:

  * Generic DER blocks expose signals via their "-UI" companion subsystem:
        Battery ESS (Generic)    -> "bess-01-UI.<signal>"
        PV Power Plant (Generic) -> "pv-01-UI.<signal>"
        Variable Load (Generic)  -> "Load-01-UI.<signal>"   (note capital L)
        Grid                     -> "Grid UI1.<signal>"
  * The flexible load is a Variable Load (Generic) "load-01" driven via its
    "load-01-UI" companion ("load-01-UI.<signal>"); the controller asset is
    now "load-01" too. (load-02, a passive RL load, is plant detail, not driven.)
  * The PCC meter is a nested "Power Meter" (Three-phase Meter) subsystem;
    its probe path is set by PCC_METER below.

Items marked  # VERIFY  could not be confirmed off-rig (exact SCADA path, sign,
or convention). Confirm each against the running model -- e.g. dump the signal
list with the HIL API (hil.get_sources() / available SCADA inputs) -- before the
first closed-loop run. Everything else is structurally fixed.

Register layout still comes only from the map files (hil.servers), so parity
holds by construction. Guarded: imports without Typhoon; raises if started
without it (use hil.chil_runner.PlantInTheLoop for off-rig validation).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import math
import time
from pathlib import Path

import yaml

try:
    # Typhoon 2026.x exposes the HIL API as MODULE-LEVEL functions; there is no
    # importable `hil` object. Use the module itself (hil_api.read_analog_signal,
    # hil_api.set_scada_input_value, ...).
    import typhoon.api.hil as hil_api  # type: ignore
    _HAVE_TYPHOON = True
    _TYPHOON_IMPORT_ERR = ""
except Exception as e:  # noqa: BLE001
    hil_api = None  # type: ignore
    _HAVE_TYPHOON = False
    _TYPHOON_IMPORT_ERR = repr(e)

from common.config_models import validate_asset_config
from common.data_model import DataModel
from common.register_map import load_register_map

from hil import profiles
from hil.plant.models import SiteModel
from hil.servers import HilModbusServer

_REPO = Path(__file__).resolve().parents[2]

# PCC meter probe prefix. In BP09_ext_v1.tse the meter is nested:
#   Root > "Power Meter" (subsystem) > pcc_01_meter > "Power Meter" (block) > probes
# Typhoon addresses probe signals by their namespaced path. "Power Meter.<probe>"
# is the most likely handle; VERIFY the exact path with the model's signal list
# and adjust this one constant if the namespace differs.
PCC_METER = "Power Meter.pcc_01_meter"  # confirmed via signal dump 2026-06-24

# Default rig asset_config: defines, per asset, the Modbus host:port that core
# connects to. The bridge binds its servers to those same ports so the loop
# closes without touching core. (SIL used one container per asset on :502; on a
# single rig host we use distinct ports on localhost -- see this file.)
DEFAULT_ASSET_CONFIG = "configs/asset_config.rig.yaml"

# asset_id -> register map. Informational now: server host/port/map come from the
# asset_config passed to TyphoonSignalBridge (kept in sync with core by construction).
ASSET_MAPS = {
    "pcc-01": "maps/grid_meter_v1.yaml",
    "bess-01": "maps/custom_bess_v1.yaml",
    "pv-01": "maps/custom_pv_inverter_v1.yaml",
    "load-01": "maps/flexible_load_v1.yaml",
    # meter-01 (fixed-load telemetry) is intentionally OMITTED: the controller's
    # control law never reads the `meter` class (data_model: control=none, not in
    # aggregates), and the loads are metered by their own UI/"loads Meter". Leaving it out avoids
    # serving a register set nothing consumes. If you DO want fixed-load telemetry,
    # re-add the line below and the matching SIGNALS["meter-01"] block, bound to the
    # Variable Load (Generic) UI outputs ("Load-01-UI.*").
    # "meter-01": "maps/meter_v1.yaml",
}

# Canonical point -> Typhoon model signal name in BP09_ext_v1.tse.
#   read = probe outputs; write = SCADA inputs.
SIGNALS = {
    "pcc-01": {
        "voltage_v": f"{PCC_METER}.VAn_RMS",        # phase-neutral RMS [V]
        "frequency_hz": f"{PCC_METER}.Freq",         # PLL frequency [Hz]
        "current_a": f"{PCC_METER}.I_RMS",           # RMS current [A]
        "active_power_kw": f"{PCC_METER}.POWER_P",   # [W] -> kW
        "reactive_power_kvar": f"{PCC_METER}.POWER_Q",  # [VAr] -> kVAr
    },
    # Battery ESS (Generic): signals on the "bess-01-UI" companion.
    "bess-01": {
        "soc_pct": "bess-01-UI.SOC",                          # output #24 (confirmed)
        "active_power_kw": "bess-01-UI.Pmeas_kW",
        "reactive_power_kvar": "bess-01-UI.Qmeas_kVAr",
        "available_charge_power_kw": "(computed: max_charge if SoC<max_soc else 0)",
        "available_discharge_power_kw": "(computed: max_discharge if SoC>min_soc else 0)",
        "active_power_setpoint_kw": "bess-01-UI.Pref",        # input #3, SCADA in, pu of Pnom
        "reactive_power_setpoint_kvar": "bess-01-UI.Qref",    # input #4, SCADA in, pu
        "_enable": "bess-01-UI.Enable",                       # input #0
        "_converter_mode": "bess-01-UI.Converter mode",       # input #1 (0=grid following) (confirmed)
        "_initial_soc": "bess-01-UI.Initial SOC",             # external input (confirmed)
        "_max_soc": "bess-01-UI.Max SOC",                     # input #10
        "_min_soc": "bess-01-UI.Min SOC",                     # input #11
    },
    # PV Power Plant (Generic): signals on the "pv-01-UI" companion.
    "pv-01": {
        "active_power_kw": "pv-01-UI.Pmeas_kW",
        "reactive_power_kvar": "pv-01-UI.Qmeas_kVAr",
        "derate_factor_setpoint": "pv-01-UI.Pcurtailment",    # input #2, pu power limit  # VERIFY convention
        "_irradiance": "pv-01-UI.Irradiance",                 # external input [W/m^2]
        "_enable": "pv-01-UI.Enable",                         # input #0
    },
    # Flexible load: Variable Load (Generic) "load-01" via its "load-01-UI"
    # companion (BP09_ext_v1 updated 2026-06-24; was a Three-phase Variable Load
    # named fload-01). DER-style PQ load with a single Pref (pu of Snom). The
    # controller asset id stays "load-01"; only the Typhoon signal names change.
    "load-01": {
        "active_power_kw": "load-01-UI.Pmeas_kW",            # confirmed via dump 2026-06-24
        "reactive_power_kvar": "load-01-UI.Qmeas_kVAr",
        "derate_factor_setpoint": "load-01-UI.Pref",         # active-power consumption ref, pu of Snom
        "_enable": "load-01-UI.Enable",
        # NOTE: Variable Load (Generic) has NO "Balance enable" (single Pref).
    },
    # Optional fixed-load telemetry (disabled; see ASSET_MAPS note). To enable,
    # uncomment the meter-01 line above and bind P/Q to the Variable Load (Generic)
    # UI outputs. V/F/I are not block outputs of that load; reuse the PCC meter
    # (same LV bus) or drop them from maps/meter_v1.yaml.
    # "meter-01": {
    #     "voltage_v": f"{PCC_METER}.VAn_RMS",
    #     "frequency_hz": f"{PCC_METER}.Freq",
    #     "current_a": f"{PCC_METER}.I_RMS",            # VERIFY: PCC current != load current
    #     "active_power_kw": "Load-01-UI.Pmeas_kW",     # VERIFY name
    #     "reactive_power_kvar": "Load-01-UI.Qmeas_kVAr",  # VERIFY name
    # },
}

# SCADA input per profile-injection channel (grid excursions, irradiance, ...).
CHANNEL_SIGNAL = {
    "frequency_hz": "Grid UI1.Grid_freq_cmd",    # pu
    "voltage_pu": "Grid UI1.Grid_Vrms_cmd",       # pu
    "irradiance": "pv-01-UI.Irradiance",          # W/m^2 (0..1000)
    "load_kw": None,                              # handled via _load_base_fraction
    "fixed_load_kw": None,                        # fixed load is passive; not driven
    "soc_init": "bess-01-UI.Initial SOC",         # %  # VERIFY
}

# Optional INTERACTIVE load-demand slider. Add a SCADA input with this exact name
# to the panel (0..500 kW, bind a slider to it) to drive the controllable load
# live in closed loop. The bridge reads it each tick and uses it as the load base
# demand (kW -> pu), then writes Pref = base * controller-derate. If the input is
# absent (off-rig, or before you add it), the bridge falls back to the load_kw
# profile-injection path, so existing scenarios are unaffected.
LOAD_DEMAND_SCADA = "load-01-UI.load_demand_probe"  # the PROBE (readable), not the SC input
# A SCADA input is write-only via this HIL API; the slider feeds it, a Probe taps
# it into a readable analog signal, and the bridge reads the Probe.
# Confirm the exact name with: python -m hil.schematic.verify_signals --dump | findstr load_demand

# --- signs / scales tied to BP09_ext_v1.tse (see module docstring) ----------
PCC_P_SIGN = +1.0     # import positive; flip to -1 if Power Meter polarity is reversed  # VERIFY
PCC_Q_SIGN = +1.0     # VERIFY
PCC_POWER_SCALE = 1e-3  # Power Meter POWER_P/Q in W/VAr -> kW/kVAr; set 1.0 if already kW  # VERIFY
BESS_P_SIGN = -1.0    # block generator-convention -> data_model +charge  # VERIFY on rig
PV_P_SIGN = -1.0      # block generation positive -> data_model pv <= 0  # VERIFY on rig

# Battery converter operating mode written at preconditioning. Pref (the active
# power setpoint) is only honored in grid-following (0) or droop (1) mode; the
# controller dispatches power, so force grid-following.
BESS_CONVERTER_MODE_GRID_FOLLOWING = 0


def _pv_curtailment_from_derate(derate: float) -> float:
    """Map the controller derate (1 = full power, 0 = fully curtailed) to the PV
    block's Pcurtailment input (pu).

    # VERIFY on rig: the Generic PV "Pcurtailment" input "limits the maximum
    # output power [pu]" (range >= 0, default 0). Two possible conventions:
    #   (a) Pcurtailment is the power CEILING in pu  -> write `derate` directly.
    #   (b) Pcurtailment is the AMOUNT curtailed     -> write `1 - derate`.
    # Confirm which one your block uses (set PV curtailment to e.g. 0.5 and watch
    # Pmeas) and keep only the correct return line.
    """
    d = max(0.0, min(1.0, derate))
    return d                  # interpretation (a): pu power ceiling
    # return 1.0 - d          # interpretation (b): pu amount curtailed


class TyphoonSignalBridge:
    """Copies model signals <-> Modbus registers each tick, with the schematic's
    sign/scale conventions baked in. Backed by the rig when Typhoon is present."""

    def __init__(
        self,
        asset_config_path: str | Path = DEFAULT_ASSET_CONFIG,
        *,
        host: str = "0.0.0.0",
        require_typhoon: bool = True,
    ):
        if require_typhoon and not _HAVE_TYPHOON:
            raise RuntimeError(
                "Typhoon HIL API not available: run on the rig. For off-rig "
                "validation use hil.chil_runner.PlantInTheLoop."
            )
        dm = DataModel.load(_REPO / "data_model.yaml")
        acp = Path(asset_config_path)
        if not acp.is_absolute():
            acp = _REPO / acp
        ac = validate_asset_config(yaml.safe_load(acp.read_text(encoding="utf-8")), dm)

        # pu bases + battery limits from asset_config (no hardcoding/drift).
        self.pu_base_kw: dict[str, float] = {}
        for a in ac.assets:
            if a.id == "bess-01":
                self.pu_base_kw["bess-01"] = a.flexibility.limits["max_charge_kw"]
                self.bess_max_charge = a.flexibility.limits["max_charge_kw"]
                self.bess_max_discharge = a.flexibility.limits["max_discharge_kw"]
                self.bess_min_soc = a.flexibility.limits["min_soc_pct"]
                self.bess_max_soc = a.flexibility.limits["max_soc_pct"]
            elif a.id == "pv-01":
                self.pu_base_kw["pv-01"] = a.nominal["max_active_power_kw"]
            elif a.id == "load-01":
                self.pu_base_kw["load-01"] = a.nominal["max_active_power_kw"]

        # One Modbus server per bridged asset, bound to the SAME host:port core
        # connects to (from this asset_config). Serving the registers over TCP is
        # what lets core.py read measurements / write setpoints -> closed loop.
        self._dummy = SiteModel()  # registers are driven by tick(), not the model
        self.servers: dict[str, HilModbusServer] = {}
        self.ports: dict[str, int] = {}
        self._serve_tasks: dict[str, object] = {}
        self.active: dict[str, bool] = {}
        for a in ac.assets:
            if a.id not in SIGNALS or a.comm is None or a.state != "active":
                continue
            rmap = load_register_map(_REPO / a.comm.register_map, dm)
            self.servers[a.id] = HilModbusServer(
                rmap, self._dummy, host=host, port=a.comm.port, unit_id=a.comm.unit_id
            )
            self.ports[a.id] = a.comm.port
            self.active[a.id] = True

        self._load_base_fraction = 0.0  # set by profile injection (load_kw channel)

    # -- low-level rig I/O ----------------------------------------------------

    def _read(self, name: str) -> float:
        return float(hil_api.read_analog_signal(name=name))

    def _read_meas(self, name: str, *, floor: float | None = None) -> float:
        """Read a plant analog signal, sanitised so an out-of-range transient
        cannot crash the Modbus encode and take down the plant interface.

        On a grid loss the PCC meter collapses to ~0 V / 0 Hz, and the model can
        emit a brief NEGATIVE-frequency transient; encoding a negative into the
        unsigned SunSpec frequency register (701.Hz, uint32) raised and killed the
        bridge (the E3 bridge-robustness finding). Here: NaN/inf -> 0.0, and
        `floor` clamps signals destined for UNSIGNED registers (V, Hz, I, SoC) to
        >= 0. The clamp only removes the crash; it never masks a real collapse --
        0 V / 0 Hz still passes straight through, so the controller's plausibility
        gate trips and the loop fails safe. Signed quantities (active/reactive
        power) pass with NaN/inf guarded but their sign preserved (floor=None)."""
        v = float(self._read(name))
        if math.isnan(v) or math.isinf(v):
            return 0.0
        if floor is not None and v < floor:
            return floor
        return v

    def _write(self, name: str, value: float) -> None:
        hil_api.set_scada_input_value(name, float(value))

    def _read_scada_input(self, name: str) -> float | None:
        """Read back the operator's load-demand slider value.

        This HIL API has no get_scada_input_value, and a SCADA *input* is not an
        analog signal (read_analog_signal can't see it). So we try, in order: a
        namespace variable (get_ns_var, qualified then bare name), then an
        analog-signal read (works only if the input is routed through a Probe).
        The first method that resolves is cached so we don't retry failing calls
        every tick (which spams Typhoon "unable to find" warnings). Returns None
        if none resolve -> caller keeps the profile-injection base demand."""
        reader = getattr(self, "_load_slider_reader", "unset")
        if reader == "unset":
            reader = self._resolve_slider_reader(name)
            self._load_slider_reader = reader
        if reader is None:
            return None
        try:
            v = reader()
            return None if v is None else float(v)
        except Exception:  # noqa: BLE001
            return None

    def _resolve_slider_reader(self, name: str):
        """Probe the available HIL API once to find a callable that returns the
        slider value, or None. Logs the outcome so the path is visible."""
        bare = name.split(".")[-1]
        candidates = []
        if hasattr(hil_api, "get_ns_var"):
            candidates.append(("get_ns_var", name, lambda: hil_api.get_ns_var(name)))
            candidates.append(("get_ns_var", bare, lambda: hil_api.get_ns_var(bare)))
        candidates.append(
            ("read_analog_signal", name, lambda: hil_api.read_analog_signal(name=name))
        )
        errs = []
        for label, arg, fn in candidates:
            try:
                if fn() is not None:
                    print(f"bridge: load slider read via {label}({arg!r})", flush=True)
                    return fn
                errs.append(f"{label}({arg!r}): None")
            except Exception as e:  # noqa: BLE001
                errs.append(f"{label}({arg!r}): {e!r}")
        print(f"bridge: load slider {name!r} unreadable; using profile base demand. "
              f"Tried -> {'; '.join(errs)}", flush=True)
        return None

    # -- one tick: model <-> registers (signs/scales applied) ----------------

    def tick(self) -> None:
        self._push_pcc()
        if self.active["bess-01"]:
            self._push_pull_bess()
        if self.active["pv-01"]:
            self._push_pull_pv()
        if self.active["load-01"]:
            self._push_pull_load()
        if self.active.get("meter-01"):
            self._push_meter()

    def _push_pcc(self) -> None:
        s, sig = self.servers["pcc-01"], SIGNALS["pcc-01"]
        # V/Hz/I map to unsigned registers -> floor at 0 (a grid-loss 0/neg
        # transient must not crash the encode; 0 V / 0 Hz still flags the island).
        s.set_point("voltage_v", self._read_meas(sig["voltage_v"], floor=0.0))
        s.set_point("frequency_hz", self._read_meas(sig["frequency_hz"], floor=0.0))
        s.set_point("current_a", self._read_meas(sig["current_a"], floor=0.0))
        s.set_point("active_power_kw",
                    PCC_P_SIGN * PCC_POWER_SCALE * self._read_meas(sig["active_power_kw"]))
        s.set_point("reactive_power_kvar",
                    PCC_Q_SIGN * PCC_POWER_SCALE * self._read_meas(sig["reactive_power_kvar"]))

    def _push_meter(self) -> None:
        # passive fixed-load meter: measurement only, no setpoints to pull back.
        # (Disabled by default -- see ASSET_MAPS / SIGNALS notes.)
        s, sig = self.servers["meter-01"], SIGNALS["meter-01"]
        s.set_point("voltage_v", self._read_meas(sig["voltage_v"], floor=0.0))
        s.set_point("frequency_hz", self._read_meas(sig["frequency_hz"], floor=0.0))
        s.set_point("current_a", self._read_meas(sig["current_a"], floor=0.0))
        s.set_point("active_power_kw", PCC_POWER_SCALE * self._read_meas(sig["active_power_kw"]))
        s.set_point("reactive_power_kvar", PCC_POWER_SCALE * self._read_meas(sig["reactive_power_kvar"]))

    def _push_pull_bess(self) -> None:
        s, sig = self.servers["bess-01"], SIGNALS["bess-01"]
        soc = self._read_meas(sig["soc_pct"], floor=0.0)  # SoC unsigned register
        s.set_point("soc_pct", soc)
        # generator-convention -> +charge/-discharge
        s.set_point("active_power_kw", BESS_P_SIGN * self._read_meas(sig["active_power_kw"]))
        s.set_point("reactive_power_kvar", BESS_P_SIGN * self._read_meas(sig["reactive_power_kvar"]))
        # BMS headroom (not a block output): SoC-gated, like hil.plant.BatteryModel
        s.set_point("available_charge_power_kw",
                    self.bess_max_charge if soc < self.bess_max_soc else 0.0)
        s.set_point("available_discharge_power_kw",
                    self.bess_max_discharge if soc > self.bess_min_soc else 0.0)
        # controller setpoint (+charge, kW) -> block Pref (pu, generator convention)
        base = self.pu_base_kw["bess-01"]
        p_sp = s.get_point("active_power_setpoint_kw")
        q_sp = s.get_point("reactive_power_setpoint_kvar")
        self._write(sig["active_power_setpoint_kw"], BESS_P_SIGN * p_sp / base)
        self._write(sig["reactive_power_setpoint_kvar"], BESS_P_SIGN * q_sp / base)

    def _push_pull_pv(self) -> None:
        s, sig = self.servers["pv-01"], SIGNALS["pv-01"]
        s.set_point("active_power_kw", PV_P_SIGN * self._read_meas(sig["active_power_kw"]))
        s.set_point("reactive_power_kvar", PV_P_SIGN * self._read_meas(sig["reactive_power_kvar"]))
        # curtailment: derate in [0,1] (1 = no curtailment) -> PV Pcurtailment (pu)
        derate = s.get_point("derate_factor_setpoint")
        self._write(sig["derate_factor_setpoint"], _pv_curtailment_from_derate(derate))

    def _push_pull_load(self) -> None:
        s, sig = self.servers["load-01"], SIGNALS["load-01"]
        s.set_point("active_power_kw", self._read_meas(sig["active_power_kw"]))
        s.set_point("reactive_power_kvar", self._read_meas(sig["reactive_power_kvar"]))
        # Interactive base demand: a SCADA slider (kW) takes precedence when present,
        # so you can step the load live and watch the BESS react. Otherwise the base
        # demand stays whatever profile injection set (load_kw channel).
        slider_kw = self._read_scada_input(LOAD_DEMAND_SCADA)
        if slider_kw is not None:
            self._load_base_fraction = slider_kw / self.pu_base_kw["load-01"]
        # load command = base demand * controller derate, in pu of Snom
        derate = s.get_point("derate_factor_setpoint")
        self._write(sig["derate_factor_setpoint"],
                    max(0.0, min(1.0, self._load_base_fraction * derate)))

    # -- profile injection (SCADA inputs) ------------------------------------

    def apply_profile_point(self, channel: str, value: float) -> None:
        if channel == "load_kw":
            self._load_base_fraction = value / self.pu_base_kw["load-01"]  # -> pu
            return
        sig = CHANNEL_SIGNAL.get(channel)
        if sig is not None:
            self._write(sig, value)

    def precondition(self, stim=None) -> None:
        # Battery: force grid-following so Pref is honored, then seed SoC limits.
        self._write(SIGNALS["bess-01"]["_converter_mode"],
                    BESS_CONVERTER_MODE_GRID_FOLLOWING)
        self._write(SIGNALS["bess-01"]["_max_soc"], self.bess_max_soc)
        self._write(SIGNALS["bess-01"]["_min_soc"], self.bess_min_soc)
        if stim is not None:
            self._write(SIGNALS["bess-01"]["_initial_soc"], stim.soc_init_pct)

    # -- fault injection: asset dropout / PCC switch (prompt section 7) -------

    def drop_asset(self, asset_id: str) -> None:
        """Asset dropout: disable the converter (Enable=0) AND stop serving its
        Modbus registers so core's adapter reports COMM_FAIL and the aggregate
        excludes it. Battery dropout drives the controller to SAFE."""
        self.active[asset_id] = False
        enable_sig = SIGNALS[asset_id].get("_enable")
        if enable_sig is not None:
            self._write(enable_sig, 0)
        task = self._serve_tasks.pop(asset_id, None)
        if task is not None:
            task.cancel()  # close the TCP listener -> COMM_FAIL at the controller

    def restore_asset(self, asset_id: str) -> None:
        """Recover a dropped asset: re-enable and resume serving."""
        import asyncio
        self.active[asset_id] = True
        enable_sig = SIGNALS[asset_id].get("_enable")
        if enable_sig is not None:
            self._write(enable_sig, 1)
        srv = self.servers[asset_id]
        self._serve_tasks[asset_id] = asyncio.get_event_loop().create_task(srv.serve())

    def open_pcc(self) -> None:
        """Open the PCC contactor (grid-loss / islanding test). NOT used for
        asset dropout -- this disconnects the whole site."""
        hil_api.set_contactor("PCC-S1", swControl=True, swState=False)

    def close_pcc(self) -> None:
        hil_api.set_contactor("PCC-S1", swControl=True, swState=True)

    # -- serve + run on the rig ----------------------------------------------

    async def serve_forever(
        self,
        *,
        scenario: str | None = None,
        tick_period_s: float = 0.5,
        duration_s: float | None = None,
    ) -> None:
        """Serve every Modbus server and run the signal<->register tick loop until
        cancelled (or duration_s elapses). SCADA-safe: if a simulation is already
        running (HIL SCADA owns it) we do NOT call start/stop."""
        sim_running = bool(hil_api.is_simulation_running())
        started_by_us = False
        if not sim_running:
            try:
                hil_api.start_simulation()
                started_by_us = True
                print("bridge: started simulation via API (SCADA not running).")
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(
                    "No simulation is running and the API cannot start one "
                    "(HIL SCADA likely owns the model). Start the simulation in "
                    "HIL SCADA, then re-run the bridge."
                ) from e
        else:
            print("bridge: simulation already running (SCADA-driven); leaving "
                  "start/stop to SCADA.")

        stim = profiles.by_name(scenario) if scenario else None
        self.precondition(stim)

        for aid, srv in self.servers.items():
            self._serve_tasks[aid] = asyncio.create_task(srv.serve())
        print("bridge: serving Modbus -> "
              + ", ".join(f"{aid}@{self.ports[aid]}" for aid in self.servers))

        t0 = time.time()
        pending = sorted(stim.timeline) if stim else []
        try:
            while True:
                t = time.time() - t0
                while pending and pending[0][0] <= t:
                    _, channel, value = pending.pop(0)
                    self.apply_profile_point(channel, value)
                self.tick()
                if duration_s is not None and t >= duration_s:
                    break
                await asyncio.sleep(tick_period_s)
        finally:
            for task in self._serve_tasks.values():
                task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*self._serve_tasks.values(), return_exceptions=True)
            if started_by_us:
                with contextlib.suppress(Exception):
                    hil_api.stop_simulation()

    def run_scenario(self, name: str, tick_period_s: float = 0.5) -> None:
        """Backwards-compatible: run one scenario for its duration, then stop."""
        stim = profiles.by_name(name)
        asyncio.run(self.serve_forever(
            scenario=name, tick_period_s=tick_period_s, duration_s=stim.duration_s
        ))


def canonical_signal_map() -> dict[str, dict[str, str]]:
    """The point->signal binding, for review/wiring (no Typhoon needed)."""
    return {a: {k: v for k, v in pts.items() if not k.startswith("_")}
            for a, pts in SIGNALS.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description="Typhoon model<->Modbus bridge")
    ap.add_argument("--scenario", default=None, choices=list(profiles.BUILDERS),
                    help="optional profile to inject (grid/irradiance); default none")
    ap.add_argument("--asset-config", default=DEFAULT_ASSET_CONFIG,
                    help="asset_config whose comm host:port the servers bind to")
    ap.add_argument("--tick-period", type=float, default=0.5)
    ap.add_argument("--duration", type=float, default=None,
                    help="seconds to run; default: serve until Ctrl-C")
    ap.add_argument("--load-base", type=float, default=0.0,
                    help="flexible-load base demand, fraction [0..1] of Snom, BEFORE "
                         "the controller derate (0 = load idle unless --scenario "
                         "drives load_kw)")
    ap.add_argument("--print-signals", action="store_true",
                    help="print the canonical point -> Typhoon signal map and exit")
    args = ap.parse_args()
    if args.print_signals or not _HAVE_TYPHOON:
        if not _HAVE_TYPHOON and not args.print_signals:
            print("Typhoon API not available here; this runs on the rig host.\n")
        for asset, pts in canonical_signal_map().items():
            print(f"[{asset}]")
            for point, sig in pts.items():
                print(f"  {point:<28} <-> {sig}")
        return 0
    bridge = TyphoonSignalBridge(asset_config_path=args.asset_config)
    bridge._load_base_fraction = args.load_base
    try:
        asyncio.run(bridge.serve_forever(
            scenario=args.scenario,
            tick_period_s=args.tick_period,
            duration_s=args.duration,
        ))
    except KeyboardInterrupt:
        print("\nbridge: stopped by user.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
