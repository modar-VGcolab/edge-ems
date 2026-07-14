"""Plant device models + site power balance for the CHIL rig.

Signs are NORMATIVE from data_model.yaml (positive = consumed by the asset) and
are asserted, not reinterpreted:

    pcc.active_power_kw        +import  / -export
    battery.active_power_kw    +charge  / -discharge   (so is its setpoint)
    pv.active_power_kw         <= 0 (generation)
    flexible_load.active_power_kw  >= 0 (consumption)
    derate_factor_setpoint     in [0, 1], 1 = no derate / no curtailment

Site power balance at the coupling point (Kirchhoff at the LV bus):

    P_pcc = P_load + P_battery + P_pv

so a battery *discharge* (P_battery < 0) reduces PCC import, and PV generation
(P_pv < 0) pushes the PCC toward export -- exactly the wiring the controller
assumes (edge_controller: dP_pcc/dP_battery = +1).

These classes are deliberately framework-free. The Typhoon schematic
(hil.schematic) realises the identical equations with a battery+converter, a
PV array+inverter, a controllable load and an ideal grid behind an impedance;
keeping the maths here means the rig and the software plant-in-the-loop agree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ------------------------------------------------------------------ BESS -----


@dataclass
class BatteryModel:
    """Battery + bidirectional converter.

    Accepts an active/reactive power setpoint (data_model: +charge/-discharge),
    follows it within the dynamic BMS headroom and the SoC hard gates, and
    integrates SoC from the *actual* delivered power over time. Headroom shrinks
    to zero at the SoC gates so the converter physically pins at the limit -- the
    condition the saturation->derate scenario relies on.
    """

    max_charge_kw: float = 1000.0
    max_discharge_kw: float = 1000.0
    capacity_kwh: float = 2000.0
    min_soc_pct: float = 5.0
    max_soc_pct: float = 95.0
    soc_pct: float = 50.0
    # Converter first-order tracking lag (s); 0 = instantaneous. A small lag
    # makes PI tuning on the rig realistic rather than algebraic.
    tau_s: float = 0.2

    p_kw: float = 0.0  # actual delivered active power (+charge/-discharge)
    q_kvar: float = 0.0
    _p_setpoint: float = 0.0
    _q_setpoint: float = 0.0

    def set_setpoint(self, p_kw: float, q_kvar: float = 0.0) -> None:
        self._p_setpoint = float(p_kw)
        self._q_setpoint = float(q_kvar)

    def _power_window(self) -> tuple[float, float]:
        """Signed [lower, upper] setpoint window: BMS headroom AND SoC gates."""
        upper = self.available_charge_power_kw      # charge is positive
        lower = -self.available_discharge_power_kw   # discharge is negative
        if lower > upper:
            lower = upper = 0.0
        return lower, upper

    def step(self, dt_s: float) -> None:
        lower, upper = self._power_window()
        target = min(max(self._p_setpoint, lower), upper)
        if self.tau_s > 0.0:
            alpha = 1.0 - math.exp(-dt_s / self.tau_s)
            self.p_kw += alpha * (target - self.p_kw)
        else:
            self.p_kw = target
        # Re-clamp after the lag so we never violate the window, then integrate.
        self.p_kw = min(max(self.p_kw, lower), upper)
        self.q_kvar = self._q_setpoint
        d_soc = (self.p_kw * dt_s / 3600.0) / self.capacity_kwh * 100.0
        self.soc_pct = min(100.0, max(0.0, self.soc_pct + d_soc))

    @property
    def available_charge_power_kw(self) -> float:
        return self.max_charge_kw if self.soc_pct < self.max_soc_pct else 0.0

    @property
    def available_discharge_power_kw(self) -> float:
        return self.max_discharge_kw if self.soc_pct > self.min_soc_pct else 0.0


# -------------------------------------------------------------------- PV -----


@dataclass
class PVModel:
    """PV array + inverter. Output follows irradiance and the curtailment
    (derate) command from the pv Modbus server. active_power_kw <= 0 always."""

    peak_kw: float = 500.0
    max_kva: float = 600.0
    min_derate: float = 0.0
    irradiance: float = 0.0          # 0..1+ fraction of STC peak
    curtail_factor: float = 1.0      # derate_factor_setpoint; 1 = no curtailment
    pf: float = 1.0                  # inverter power factor for Q

    p_kw: float = 0.0
    q_kvar: float = 0.0

    def set_curtail(self, derate_factor: float) -> None:
        self.curtail_factor = min(1.0, max(self.min_derate, float(derate_factor)))

    def set_irradiance(self, irradiance: float) -> None:
        self.irradiance = max(0.0, float(irradiance))

    def step(self, dt_s: float) -> None:
        gen = self.peak_kw * self.irradiance * self.curtail_factor
        gen = min(gen, self.max_kva)            # apparent-power inverter clamp
        self.p_kw = -gen                        # generation is negative
        if self.pf < 1.0 and gen > 0.0:
            self.q_kvar = -gen * math.tan(math.acos(self.pf))
        else:
            self.q_kvar = 0.0


# --------------------------------------------------------- flexible load -----


@dataclass
class FlexibleLoadModel:
    """Controllable load (EV charger / heat pump...). Follows a base demand
    profile scaled by the derate command. active_power_kw >= 0 always."""

    max_kw: float = 700.0
    min_derate: float = 0.2
    base_fraction: float = 0.0       # 0..1 of max_kw requested before derate
    derate_factor: float = 1.0       # 1 = no derate
    pf: float = 1.0

    p_kw: float = 0.0
    q_kvar: float = 0.0

    def set_derate(self, derate_factor: float) -> None:
        self.derate_factor = min(1.0, max(self.min_derate, float(derate_factor)))

    def set_base(self, fraction: float) -> None:
        self.base_fraction = max(0.0, float(fraction))

    def set_base_kw(self, kw: float) -> None:
        self.base_fraction = max(0.0, float(kw) / self.max_kw)

    def step(self, dt_s: float) -> None:
        demand = self.max_kw * min(1.0, self.base_fraction) * self.derate_factor
        self.p_kw = max(0.0, demand)
        if self.pf < 1.0 and self.p_kw > 0.0:
            self.q_kvar = self.p_kw * math.tan(math.acos(self.pf))
        else:
            self.q_kvar = 0.0


# ----------------------------------------------------------- fixed load -----


@dataclass
class FixedLoadModel:
    """Non-controllable load behind a passive meter: consumes a fixed base demand
    (no derate, no setpoint). Like any consumer it adds to the site PCC balance.
    active_power_kw >= 0 always; default 0 kW so it is inert until a profile sets
    a base demand (keeping existing scenarios unchanged)."""

    base_kw: float = 0.0
    pf: float = 1.0

    p_kw: float = 0.0
    q_kvar: float = 0.0

    def set_base_kw(self, kw: float) -> None:
        self.base_kw = max(0.0, float(kw))

    def step(self, dt_s: float) -> None:
        self.p_kw = max(0.0, self.base_kw)
        if self.pf < 1.0 and self.p_kw > 0.0:
            self.q_kvar = self.p_kw * math.tan(math.acos(self.pf))
        else:
            self.q_kvar = 0.0


# ------------------------------------------------------------------ grid -----


@dataclass
class GridModel:
    """Ideal grid source behind a small impedance. Frequency and voltage are
    externally drivable (profile injection) for the droop/excursion scenarios.
    The grid is a slack bus: it supplies the net site power, so it does not
    compute P itself -- the SiteModel forms the balance."""

    nominal_voltage_v: float = 230.0   # phase-neutral (data_model pcc.voltage_v)
    nominal_frequency_hz: float = 50.0
    line_voltage_v: float = 400.0      # LV feeder line-line, for current calc
    frequency_hz: float = 50.0
    voltage_v: float = 230.0

    def set_frequency(self, hz: float) -> None:
        self.frequency_hz = float(hz)

    def set_voltage(self, v: float) -> None:
        self.voltage_v = float(v)

    def set_voltage_pu(self, pu: float) -> None:
        self.voltage_v = float(pu) * self.nominal_voltage_v


# ------------------------------------------------------------------ site -----


@dataclass(frozen=True)
class PccMeasurement:
    """What the grid-meter Modbus server publishes each tick (canonical names)."""

    voltage_v: float
    frequency_hz: float
    current_a: float
    active_power_kw: float   # +import / -export
    reactive_power_kvar: float


@dataclass
class SiteModel:
    """One site: grid/PCC + one battery + one PV + one flexible load on an LV bus.

    `step` advances every device, then forms the PCC balance. Measurements are
    exposed under canonical data_model names for the Modbus servers to publish;
    setpoints flow in via the device `set_*` methods (driven by the servers'
    writable registers)."""

    grid: GridModel = field(default_factory=GridModel)
    battery: BatteryModel = field(default_factory=BatteryModel)
    pv: PVModel = field(default_factory=PVModel)
    load: FlexibleLoadModel = field(default_factory=FlexibleLoadModel)
    fixed_load: FixedLoadModel = field(default_factory=FixedLoadModel)

    def step(self, dt_s: float) -> None:
        self.battery.step(dt_s)
        self.pv.step(dt_s)
        self.load.step(dt_s)
        self.fixed_load.step(dt_s)

    # -- PCC balance ---------------------------------------------------------

    @property
    def pcc_active_power_kw(self) -> float:
        # +import / -export = load (>=0) + battery (+charge/-discharge) + pv (<=0)
        return self.load.p_kw + self.fixed_load.p_kw + self.battery.p_kw + self.pv.p_kw

    @property
    def pcc_reactive_power_kvar(self) -> float:
        return self.load.q_kvar + self.fixed_load.q_kvar + self.battery.q_kvar + self.pv.q_kvar

    @property
    def pcc_current_a(self) -> float:
        v_ll = self.grid.line_voltage_v
        if v_ll <= 0.0:
            return 0.0
        s_kva = math.hypot(self.pcc_active_power_kw, self.pcc_reactive_power_kvar)
        return abs(s_kva) * 1000.0 / (math.sqrt(3.0) * v_ll)

    def pcc_measurement(self) -> PccMeasurement:
        return PccMeasurement(
            voltage_v=self.grid.voltage_v,
            frequency_hz=self.grid.frequency_hz,
            current_a=self.pcc_current_a,
            active_power_kw=self.pcc_active_power_kw,
            reactive_power_kvar=self.pcc_reactive_power_kvar,
        )

    # -- canonical point views for the Modbus servers ------------------------

    def points(self, asset_class: str) -> dict[str, float]:
        """Current input-point values for an asset class, by canonical name."""
        if asset_class == "pcc":
            m = self.pcc_measurement()
            return {
                "voltage_v": m.voltage_v,
                "frequency_hz": m.frequency_hz,
                "current_a": m.current_a,
                "active_power_kw": m.active_power_kw,
                "reactive_power_kvar": m.reactive_power_kvar,
            }
        if asset_class == "battery":
            return {
                "soc_pct": self.battery.soc_pct,
                "active_power_kw": self.battery.p_kw,
                "reactive_power_kvar": self.battery.q_kvar,
                "available_charge_power_kw": self.battery.available_charge_power_kw,
                "available_discharge_power_kw": self.battery.available_discharge_power_kw,
            }
        if asset_class == "pv":
            return {
                "active_power_kw": self.pv.p_kw,
                "reactive_power_kvar": self.pv.q_kvar,
            }
        if asset_class == "flexible_load":
            return {
                "active_power_kw": self.load.p_kw,
                "reactive_power_kvar": self.load.q_kvar,
            }
        if asset_class == "meter":
            # passive meter on the fixed load: V/Hz from the bus, P/Q + derived I.
            v_ll = self.grid.line_voltage_v
            s_kva = math.hypot(self.fixed_load.p_kw, self.fixed_load.q_kvar)
            current_a = abs(s_kva) * 1000.0 / (math.sqrt(3.0) * v_ll) if v_ll > 0.0 else 0.0
            return {
                "voltage_v": self.grid.voltage_v,
                "frequency_hz": self.grid.frequency_hz,
                "current_a": current_a,
                "active_power_kw": self.fixed_load.p_kw,
                "reactive_power_kvar": self.fixed_load.q_kvar,
            }
        raise KeyError(f"unknown asset class '{asset_class}'")

    def apply_setpoint(self, asset_class: str, point: str, value: float) -> None:
        """Apply a writable setpoint coming from a Modbus server's register."""
        if asset_class == "battery":
            if point == "active_power_setpoint_kw":
                self.battery.set_setpoint(value, self.battery._q_setpoint)
            elif point == "reactive_power_setpoint_kvar":
                self.battery.set_setpoint(self.battery._p_setpoint, value)
        elif asset_class == "pv":
            if point == "derate_factor_setpoint":
                self.pv.set_curtail(value)
        elif asset_class == "flexible_load":
            if point == "derate_factor_setpoint":
                self.load.set_derate(value)
