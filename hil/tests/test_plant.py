"""Plant physics: signs and the PCC balance are normative (data_model.yaml)."""
from hil.plant.models import SiteModel


def test_battery_charge_raises_soc_discharge_lowers():
    s = SiteModel()
    s.battery.soc_pct = 50.0
    s.battery.set_setpoint(1000.0)  # +charge
    for _ in range(60):
        s.battery.step(1.0)
    assert s.battery.soc_pct > 50.0
    s.battery.set_setpoint(-1000.0)  # -discharge
    soc0 = s.battery.soc_pct
    for _ in range(60):
        s.battery.step(1.0)
    assert s.battery.soc_pct < soc0


def test_soc_gates_block_charge_at_max_and_discharge_at_min():
    s = SiteModel()
    s.battery.soc_pct = 95.0
    assert s.battery.available_charge_power_kw == 0.0       # full: no charge
    s.battery.soc_pct = 5.0
    assert s.battery.available_discharge_power_kw == 0.0    # empty: no discharge


def test_pv_is_nonpositive_load_nonnegative():
    s = SiteModel()
    s.pv.set_irradiance(1.0)
    s.load.set_base(1.0)
    s.step(1.0)
    assert s.pv.p_kw <= 0.0
    assert s.load.p_kw >= 0.0


def test_pcc_balance_and_signs():
    s = SiteModel()
    s.load.set_base_kw(300.0)         # +import contribution
    s.pv.set_irradiance(0.0)
    s.battery.set_setpoint(-200.0)    # discharge reduces import
    for _ in range(5):                # let the converter lag settle
        s.step(1.0)
    # PCC = load + battery + pv = 300 + (-200) + 0 = 100
    assert abs(s.pcc_active_power_kw - 100.0) < 1.0
    # PV generation pushes toward export (more negative)
    s.pv.peak_kw = 500.0
    s.pv.max_kva = 600.0
    s.pv.set_irradiance(1.0)
    s.step(1.0)
    assert s.pcc_active_power_kw < 100.0
