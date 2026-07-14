"""The fixed-load scenario hook drives SiteModel.fixed_load and the PCC balance."""
from hil.plant.models import SiteModel
from hil.profiles import tracking, with_fixed_load


def test_with_fixed_load_drives_plant_and_pcc():
    stim = with_fixed_load(tracking(load_kw=0.0), 150.0)
    site = SiteModel()
    stim.apply(0.0, site)
    site.step(1.0)
    # the fixed load consumes its base demand...
    assert abs(site.fixed_load.p_kw - 150.0) < 1e-9
    # ...and shows up at the PCC (flex load + pv + battery are all 0 here).
    assert abs(site.pcc_active_power_kw - 150.0) < 1e-6
    # and the channel is carried for the Typhoon SCADA timeline.
    assert (0.0, "fixed_load_kw", 150.0) in stim.timeline


def test_unwrapped_scenarios_have_no_fixed_load():
    site = SiteModel()
    tracking(load_kw=250.0).apply(0.0, site)
    site.step(1.0)
    assert site.fixed_load.p_kw == 0.0  # inert unless explicitly wrapped
