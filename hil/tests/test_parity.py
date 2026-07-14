"""The register-parity gate must be green (map = simulator = HIL server)."""
from hil import parity_check


def test_parity_holds_for_all_site_maps():
    ok, result = parity_check.run()
    assert ok, result["errors"]
    # every register fingerprinted across all four site maps (incl. the full
    # walkable SunSpec image for the BESS); just assert all maps contributed.
    assert len(result["maps"]) == 5
    assert all(len(d) > 0 for d in result["maps"].values())


def test_asset_config_maps_match_classes():
    from common.data_model import DataModel
    dm = DataModel.load(parity_check.REPO_ROOT / "data_model.yaml")
    assert parity_check.check_asset_config(dm) == []
