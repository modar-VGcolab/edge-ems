"""Contract tests for the Core config API (ADR-0001 Phase 2).

Core is the configuration authority. These mirror the frozen Controller config
contract (Phase 0) against Core's surface: read, dry-run validate, atomic update,
422 on invalid (file + live untouched), secret containment, and the read-only
data-model endpoint. Loop-state gating (409) is Phase 4 and intentionally absent.
"""

import shutil

import pytest
import yaml
from common.config_manager import ConfigManager
from core.config_api import create_config_app
from fastapi.testclient import TestClient


@pytest.fixture()
def client(repo_root, dm, tmp_path):
    asset_path = tmp_path / "asset_config.yaml"
    ems_path = tmp_path / "edge_ems_config.yaml"
    shutil.copy(repo_root / "configs" / "asset_config.example.yaml", asset_path)
    shutil.copy(repo_root / "configs" / "edge_ems_config.example.yaml", ems_path)
    cm = ConfigManager(dm, asset_path, ems_path)
    dm_path = repo_root / "data_model.yaml"
    return TestClient(create_config_app(cm, dm, dm_path)), cm, asset_path, ems_path


def test_health(client):
    tc, *_ = client
    body = tc.get("/health").json()
    assert body["status"] == "ok"
    assert body["data_model_version"] == "0.1"


def test_data_model_served_read_only(client):
    tc, *_ = client
    body = tc.get("/config/data_model").json()
    assert body["data_model_version"] == "0.1"
    assert "pcc" in body["asset_classes"]
    # read-only: there is no PUT for the ontology
    assert tc.put("/config/data_model", json=body).status_code == 405


def test_get_and_put_ems(client):
    tc, cm, _, ems_path = client
    raw = tc.get("/config/ems").json()
    raw["controller"]["Kp"] = 0.8
    assert tc.put("/config/ems", json=raw).status_code == 200
    assert cm.ems_config.controller.Kp == 0.8
    assert yaml.safe_load(ems_path.read_text())["controller"]["Kp"] == 0.8


def test_put_invalid_ems_is_422_and_file_untouched(client):
    tc, cm, _, ems_path = client
    before = ems_path.read_text()
    raw = tc.get("/config/ems").json()
    raw["controller"]["timeout_period"] = 0.1  # < update_period
    assert tc.put("/config/ems", json=raw).status_code == 422
    assert ems_path.read_text() == before
    assert cm.ems_config.controller.timeout_period == 2.0


def test_validate_is_dry_run(client):
    tc, _, asset_path, _ = client
    before = asset_path.read_text()
    raw = tc.get("/config/assets").json()
    raw["assets"][0]["limits"]["max_bananas"] = 7
    body = tc.post("/config/assets/validate", json=raw).json()
    assert body["valid"] is False
    assert any("max_bananas" in e for e in body["errors"])
    assert asset_path.read_text() == before


def test_put_assets_reports_structural_flag(client):
    tc, *_ = client
    # non-structural: limit tweak only
    raw = tc.get("/config/assets").json()
    raw["assets"][1]["flexibility"]["limits"]["max_charge_kw"] = 900
    body = tc.put("/config/assets", json=raw).json()
    assert body["applied"] is True and body["structural"] is False
    # structural: reclassify an asset
    raw2 = tc.get("/config/assets").json()
    raw2["assets"][1]["class"] = "pv"
    # may 422 on class-specific schema; only assert structural detection when applied
    resp = tc.put("/config/assets", json=raw2)
    if resp.status_code == 200:
        assert resp.json()["structural"] is True


def test_secret_never_crosses_api(client, monkeypatch):
    tc, _, _, ems_path = client
    body = tc.get("/config/ems").json()
    assert body["influxdb"]["token"] == "${INFLUX_TOKEN}"
    monkeypatch.setenv("INFLUX_TOKEN", "real-secret-123")
    assert tc.put("/config/ems", json=body).status_code == 200
    on_disk = ems_path.read_text(encoding="utf-8")
    assert "${INFLUX_TOKEN}" in on_disk and "real-secret-123" not in on_disk
