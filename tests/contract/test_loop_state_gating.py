"""Phase 4 — loop-state gating handshake (ADR-0001).

Core refuses a structural asset change while the Controller's loop is running,
and (conservatively) while the loop state cannot be confirmed stopped.
"""

import shutil

import pytest
from common.config_manager import ConfigManager
from controller.config_manager import ConfigManager as CtrlCM
from controller.http_api import create_app
from core.config_api import create_config_app
from fastapi.testclient import TestClient


def _fresh_cm(repo_root, dm, tmp_path):
    a = tmp_path / "assets.yaml"
    e = tmp_path / "ems.yaml"
    shutil.copy(repo_root / "configs" / "asset_config.example.yaml", a)
    shutil.copy(repo_root / "configs" / "edge_ems_config.example.yaml", e)
    return ConfigManager(dm, a, e)


def _app(repo_root, dm, cm, provider):
    return TestClient(
        create_config_app(cm, dm, repo_root / "data_model.yaml", loop_state=provider)
    )


def _remove_an_asset(tc):
    raw = tc.get("/config/assets").json()
    raw["assets"] = [x for x in raw["assets"] if x["id"] != "meter-01"]
    return raw


def test_structural_refused_while_running(repo_root, dm, tmp_path):
    tc = _app(repo_root, dm, _fresh_cm(repo_root, dm, tmp_path), lambda: "running")
    assert tc.put("/config/assets", json=_remove_an_asset(tc)).status_code == 409


def test_structural_allowed_while_stopped(repo_root, dm, tmp_path):
    tc = _app(repo_root, dm, _fresh_cm(repo_root, dm, tmp_path), lambda: "stopped")
    assert tc.put("/config/assets", json=_remove_an_asset(tc)).status_code == 200


def test_non_structural_allowed_while_running(repo_root, dm, tmp_path):
    tc = _app(repo_root, dm, _fresh_cm(repo_root, dm, tmp_path), lambda: "running")
    raw = tc.get("/config/assets").json()
    raw["assets"][1]["flexibility"]["limits"]["max_charge_kw"] = 880
    assert tc.put("/config/assets", json=raw).status_code == 200


def test_structural_refused_when_controller_unreachable(repo_root, dm, tmp_path):
    def boom():
        raise OSError("controller unreachable")
    tc = _app(repo_root, dm, _fresh_cm(repo_root, dm, tmp_path), boom)
    assert tc.put("/config/assets", json=_remove_an_asset(tc)).status_code == 409


def test_no_gating_when_provider_absent(repo_root, dm, tmp_path):
    # Phase 2 behavior preserved: without a provider, structural changes apply.
    tc = TestClient(
        create_config_app(_fresh_cm(repo_root, dm, tmp_path), dm,
                          repo_root / "data_model.yaml")
    )
    assert tc.put("/config/assets", json=_remove_an_asset(tc)).status_code == 200


def test_controller_exposes_loop_state(repo_root, dm, tmp_path):
    a = tmp_path / "a.yaml"
    e = tmp_path / "e.yaml"
    shutil.copy(repo_root / "configs" / "asset_config.example.yaml", a)
    shutil.copy(repo_root / "configs" / "edge_ems_config.example.yaml", e)
    tc = TestClient(create_app(CtrlCM(dm, a, e), dm))
    assert tc.get("/loop/state").json()["loop_state"] == "stopped"
    tc.post("/loop/start")
    assert tc.get("/loop/state").json()["loop_state"] == "running"
