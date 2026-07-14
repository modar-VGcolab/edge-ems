"""Characterization tests — ADR-0001 Phase 0.

These freeze the configuration *contract* that today lives in the Controller
(`controller.config_manager.ConfigManager` + `controller.http_api`). The Core
config-authority refactor (ADR-0001) must keep every behavior below true when
the same surface is served by Core. They intentionally assert behavior already
relied on elsewhere, but pin it explicitly as the migration contract.

If one of these breaks during the refactor, the migration changed observable
behavior — stop and reconcile against ADR-0001, do not "fix" the test.
"""

import shutil

import pytest
import yaml
from controller.config_manager import ConfigManager
from controller.http_api import create_app
from fastapi.testclient import TestClient


@pytest.fixture()
def client(repo_root, dm, tmp_path):
    asset_path = tmp_path / "asset_config.yaml"
    ems_path = tmp_path / "edge_ems_config.yaml"
    shutil.copy(repo_root / "configs" / "asset_config.example.yaml", asset_path)
    shutil.copy(repo_root / "configs" / "edge_ems_config.example.yaml", ems_path)
    cm = ConfigManager(dm, asset_path, ems_path)
    return TestClient(create_app(cm, dm)), cm, asset_path, ems_path


# -- secret containment: the API must never expose a resolved ${VAR} ----------

def test_get_ems_via_api_keeps_secret_placeholder(client, monkeypatch):
    """GET /config/ems returns the editable document with ${VAR} intact — the
    resolved secret must never cross the HTTP boundary."""
    tc, *_ = client
    body = tc.get("/config/ems").json()
    assert body["influxdb"]["token"] == "${INFLUX_TOKEN}"


def test_put_roundtrip_via_api_never_persists_secret(client, monkeypatch, tmp_path):
    """A GET->PUT round-trip through the API must leave the placeholder on disk;
    the secret is resolved only for the live (in-memory) config."""
    monkeypatch.setenv("INFLUX_TOKEN", "real-secret-123")
    tc, cm, _, ems_path = client
    raw = tc.get("/config/ems").json()
    assert tc.put("/config/ems", json=raw).status_code == 200
    on_disk = ems_path.read_text(encoding="utf-8")
    assert "${INFLUX_TOKEN}" in on_disk
    assert "real-secret-123" not in on_disk


# -- structural classification: reclassifying an asset is structural ----------

def test_reclassifying_asset_is_structural_change(client):
    """Changing an existing asset's class (not just its limits) is a structural
    change and must be refused (409) while the loop runs."""
    tc, *_ = client
    assert tc.post("/loop/start").status_code == 200
    raw = tc.get("/config/assets").json()
    # flip the class of an existing asset id -> structural per (id, class) set
    raw["assets"][1]["class"] = "pv"
    assert tc.put("/config/assets", json=raw).status_code == 409


def test_adding_asset_is_structural_change(client):
    """Adding a new asset id is structural and refused while running."""
    tc, *_ = client
    assert tc.post("/loop/start").status_code == 200
    raw = tc.get("/config/assets").json()
    new_asset = dict(raw["assets"][1])
    new_asset["id"] = "bess-99"
    raw["assets"].append(new_asset)
    assert tc.put("/config/assets", json=raw).status_code == 409


# -- atomicity: a rejected write leaves both disk and live config intact -------

def test_invalid_put_leaves_disk_and_live_untouched(client):
    tc, cm, asset_path, _ = client
    before_disk = asset_path.read_text(encoding="utf-8")
    before_live = yaml.safe_dump(
        {a.id: a.asset_class for a in cm.asset_config.assets}, sort_keys=True
    )
    raw = tc.get("/config/assets").json()
    raw["assets"][0]["limits"]["max_nonsense"] = 1  # unknown key -> 422
    assert tc.put("/config/assets", json=raw).status_code == 422
    assert asset_path.read_text(encoding="utf-8") == before_disk
    after_live = yaml.safe_dump(
        {a.id: a.asset_class for a in cm.asset_config.assets}, sort_keys=True
    )
    assert after_live == before_live
