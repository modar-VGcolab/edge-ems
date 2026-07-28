"""Contract tests for the controller HTTP API (system design §3.1)."""

import copy
import shutil

import pytest
import yaml
from controller.config_manager import ConfigManager
from controller.http_api import create_app
from fastapi.testclient import TestClient


@pytest.fixture()
def client(repo_root, dm, tmp_path):
    # work on throwaway copies — PUTs must not touch the repo's example configs
    asset_path = tmp_path / "asset_config.yaml"
    ems_path = tmp_path / "edge_ems_config.yaml"
    shutil.copy(repo_root / "configs" / "asset_config.example.yaml", asset_path)
    shutil.copy(repo_root / "configs" / "edge_ems_config.example.yaml", ems_path)
    cm = ConfigManager(dm, asset_path, ems_path)
    app = create_app(cm, dm)
    return TestClient(app), cm, asset_path, ems_path


def test_health(client):
    tc, *_ = client
    body = tc.get("/health").json()
    assert body["status"] == "ok"
    assert body["loop_state"] == "stopped"
    assert body["data_model_version"] == "0.1"


def test_status_lists_assets(client):
    tc, *_ = client
    body = tc.get("/status").json()
    assert body["site_id"] == "vgcolab-01"
    assert body["assets"]["bess-01"]["class"] == "battery"


def test_get_and_put_ems_config(client):
    tc, cm, _, ems_path = client
    raw = tc.get("/config/ems").json()
    raw["controller"]["Kp"] = 0.8
    assert tc.put("/config/ems", json=raw).status_code == 200
    assert cm.ems_config.controller.Kp == 0.8
    on_disk = yaml.safe_load(ems_path.read_text())
    assert on_disk["controller"]["Kp"] == 0.8


def test_put_ems_applies_to_a_running_loop(repo_root, dm, tmp_path):
    # KNOWN_ISSUES #2: a PUT must reach a live loop that supports apply_config,
    # not just update the stored ConfigManager. The default LoopHandle stub
    # (used by every other test in this file) has no apply_config -- those
    # tests implicitly cover the "no live loop attached" no-op path.
    class SpyLoop:
        def __init__(self):
            self.applied_with = None
            self.state = "running"

        def apply_config(self, cm):
            self.applied_with = cm

    asset_path = tmp_path / "asset_config.yaml"
    ems_path = tmp_path / "edge_ems_config.yaml"
    shutil.copy(repo_root / "configs" / "asset_config.example.yaml", asset_path)
    shutil.copy(repo_root / "configs" / "edge_ems_config.example.yaml", ems_path)
    cm = ConfigManager(dm, asset_path, ems_path)
    spy = SpyLoop()
    tc = TestClient(create_app(cm, dm, loop=spy))

    raw = tc.get("/config/ems").json()
    raw["controller"]["Kp"] = 0.8
    assert tc.put("/config/ems", json=raw).status_code == 200
    assert spy.applied_with is cm
    assert spy.applied_with.ems_config.controller.Kp == 0.8


def test_put_invalid_ems_is_422_and_file_untouched(client):
    tc, cm, _, ems_path = client
    before = ems_path.read_text()
    raw = tc.get("/config/ems").json()
    raw["controller"]["timeout_period"] = 0.1  # < update_period
    resp = tc.put("/config/ems", json=raw)
    assert resp.status_code == 422
    assert "timeout_period" in str(resp.json())
    assert ems_path.read_text() == before
    assert cm.ems_config.controller.timeout_period == 2.0


def test_validate_endpoint_is_dry_run(client):
    tc, _, asset_path, _ = client
    before = asset_path.read_text()
    raw = tc.get("/config/assets").json()
    raw["assets"][0]["limits"]["max_bananas"] = 7
    body = tc.post("/config/assets/validate", json=raw).json()
    assert body["valid"] is False
    assert any("max_bananas" in e for e in body["errors"])
    assert asset_path.read_text() == before


def test_put_assets_applies_when_loop_stopped(client):
    tc, cm, *_ = client
    raw = tc.get("/config/assets").json()
    raw["assets"][1]["flexibility"]["limits"]["max_charge_kw"] = 900
    assert tc.put("/config/assets", json=raw).status_code == 200
    bess = next(a for a in cm.asset_config.assets if a.id == "bess-01")
    assert bess.flexibility.limits["max_charge_kw"] == 900


def test_structural_change_blocked_while_running(client):
    tc, *_ = client
    assert tc.post("/loop/start").status_code == 200
    raw = tc.get("/config/assets").json()
    raw["assets"] = [a for a in raw["assets"] if a["id"] != "meter-01"]  # remove an asset
    assert tc.put("/config/assets", json=raw).status_code == 409
    # non-structural edit still allowed while running
    raw2 = tc.get("/config/assets").json()
    raw2["assets"][1]["flexibility"]["limits"]["max_charge_kw"] = 950
    assert tc.put("/config/assets", json=raw2).status_code == 200


def test_loop_start_stop_semantics(client):
    tc, *_ = client
    assert tc.post("/loop/start").status_code == 200
    assert tc.post("/loop/start").status_code == 409
    assert tc.post("/loop/stop").status_code == 200
    assert tc.post("/loop/stop").status_code == 409


def test_version_mismatch_rejected_via_api(client):
    tc, *_ = client
    raw = copy.deepcopy(tc.get("/config/ems").json())
    raw["data_model_version"] = "9.9"
    resp = tc.put("/config/ems", json=raw)
    assert resp.status_code == 422
    assert "mismatch" in str(resp.json())


# -- live PCC setpoint (external-EMS takeover, S1) -----------------------------


class _FakeLoop:
    """Stands in for LoopRunner: tracks state plus a live PCC setpoint and the
    /status last_cycle view, so /setpoint can be exercised end to end."""

    def __init__(self):
        self.state = "stopped"
        self._pcc = 0.0

    def start(self):
        self.state = "running"
        return True

    def stop(self):
        self.state = "stopped"
        return True

    def set_pcc_setpoint_kw(self, value):
        self._pcc = float(value)
        return self._pcc

    def status(self):
        return {"loop_state": self.state, "pcc_setpoint_kw": self._pcc}


@pytest.fixture()
def live_client(repo_root, dm, tmp_path):
    asset_path = tmp_path / "asset_config.yaml"
    ems_path = tmp_path / "edge_ems_config.yaml"
    shutil.copy(repo_root / "configs" / "asset_config.example.yaml", asset_path)
    shutil.copy(repo_root / "configs" / "edge_ems_config.example.yaml", ems_path)
    cm = ConfigManager(dm, asset_path, ems_path)
    app = create_app(cm, dm, loop=_FakeLoop())
    return TestClient(app)


def test_setpoint_applies_live_and_shows_in_status(live_client):
    resp = live_client.post("/setpoint", json={"pcc_setpoint_kw": -123.0})
    assert resp.status_code == 200
    assert resp.json()["pcc_setpoint_kw"] == -123.0
    # readable back via /status last_cycle (verifies the running loop was updated)
    assert live_client.get("/status").json()["last_cycle"]["pcc_setpoint_kw"] == -123.0


def test_setpoint_reactive_accepted_but_ignored(live_client):
    resp = live_client.post(
        "/setpoint", json={"pcc_setpoint_kw": 10.0, "reactive_setpoint_kvar": 5.0}
    )
    assert resp.status_code == 200
    assert resp.json()["pcc_setpoint_kw"] == 10.0


def test_setpoint_missing_field_is_422(live_client):
    assert live_client.post("/setpoint", json={}).status_code == 422


def test_setpoint_non_numeric_is_422(live_client):
    assert live_client.post("/setpoint", json={"pcc_setpoint_kw": "abc"}).status_code == 422


def test_setpoint_without_live_loop_is_503(client):
    # default stub LoopHandle has no live setter -> not available
    tc, *_ = client
    assert tc.post("/setpoint", json={"pcc_setpoint_kw": 1.0}).status_code == 503


# ------------------------------------------------------- API auth (KNOWN_ISSUES #4)


def test_no_token_configured_means_auth_off(client):
    # Default/today's behavior: CONTROLLER_API_TOKEN unset -> every route works
    # with no header at all, matching every other test in this file.
    tc, *_ = client
    assert tc.get("/status").status_code == 200
    assert tc.get("/config/assets").status_code == 200


def test_protected_routes_reject_missing_or_wrong_key(client, monkeypatch):
    monkeypatch.setenv("CONTROLLER_API_TOKEN", "s3cret")
    tc, *_ = client
    assert tc.get("/status").status_code == 401
    assert tc.get("/status", headers={"X-API-Key": "wrong"}).status_code == 401
    assert tc.get("/config/assets").status_code == 401
    assert tc.post("/loop/start").status_code == 401


def test_protected_routes_accept_correct_key(client, monkeypatch):
    monkeypatch.setenv("CONTROLLER_API_TOKEN", "s3cret")
    tc, *_ = client
    headers = {"X-API-Key": "s3cret"}
    assert tc.get("/status", headers=headers).status_code == 200
    assert tc.get("/config/assets", headers=headers).status_code == 200
    raw = tc.get("/config/ems", headers=headers).json()
    assert tc.put("/config/ems", json=raw, headers=headers).status_code == 200


def test_health_and_loop_state_stay_open_even_with_token_set(client, monkeypatch):
    monkeypatch.setenv("CONTROLLER_API_TOKEN", "s3cret")
    tc, *_ = client
    assert tc.get("/health").status_code == 200          # container healthchecks
    assert tc.get("/loop/state").status_code == 200       # core polls this internally
