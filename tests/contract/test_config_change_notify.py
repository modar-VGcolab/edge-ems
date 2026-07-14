"""Phase 5 — change notification + re-pull (ADR-0001)."""

import shutil

import pytest
from common.config_manager import ConfigManager
from controller.config_client import ConfigWatcher
from core.config_api import create_config_app
from fastapi.testclient import TestClient


@pytest.fixture()
def client(repo_root, dm, tmp_path):
    a = tmp_path / "assets.yaml"
    e = tmp_path / "ems.yaml"
    shutil.copy(repo_root / "configs" / "asset_config.example.yaml", a)
    shutil.copy(repo_root / "configs" / "edge_ems_config.example.yaml", e)
    cm = ConfigManager(dm, a, e)
    events = []
    app = create_config_app(cm, dm, repo_root / "data_model.yaml",
                            on_change=lambda kind, info: events.append((kind, info)))
    return TestClient(app), events


def test_ems_put_emits_change(client):
    tc, events = client
    raw = tc.get("/config/ems").json()
    raw["controller"]["Kp"] = 0.7
    body = tc.put("/config/ems", json=raw).json()
    assert events and events[-1][0] == "ems"
    assert "hash" in events[-1][1] and body["hash"] == events[-1][1]["hash"]


def test_assets_put_emits_change_with_structural_flag(client):
    tc, events = client
    raw = tc.get("/config/assets").json()
    raw["assets"][1]["flexibility"]["limits"]["max_charge_kw"] = 870
    tc.put("/config/assets", json=raw)
    kind, info = events[-1]
    assert kind == "assets" and info["structural"] is False and "hash" in info


def test_invalid_put_does_not_emit(client):
    tc, events = client
    raw = tc.get("/config/ems").json()
    raw["controller"]["timeout_period"] = 0.1  # invalid -> 422
    assert tc.put("/config/ems", json=raw).status_code == 422
    assert events == []


def test_notification_failure_does_not_break_write(repo_root, dm, tmp_path):
    a = tmp_path / "assets.yaml"
    e = tmp_path / "ems.yaml"
    shutil.copy(repo_root / "configs" / "asset_config.example.yaml", a)
    shutil.copy(repo_root / "configs" / "edge_ems_config.example.yaml", e)
    cm = ConfigManager(dm, a, e)

    def boom(kind, info):
        raise RuntimeError("broker down")

    tc = TestClient(create_config_app(cm, dm, repo_root / "data_model.yaml", on_change=boom))
    raw = tc.get("/config/ems").json()
    raw["controller"]["Kp"] = 0.6
    assert tc.put("/config/ems", json=raw).status_code == 200  # write still succeeds


# -- ConfigWatcher policy ------------------------------------------------------

class _FakeClient:
    def __init__(self, fail=False):
        self.syncs = 0
        self.fail = fail

    def sync(self):
        self.syncs += 1
        if self.fail:
            raise OSError("core down")


def test_watcher_topic_uses_site_id():
    w = ConfigWatcher(_FakeClient(), lambda: None, site_id="vgcolab-01")
    assert w.topic == "site/vgcolab-01/config/changed"


def test_watcher_resyncs_and_reloads_on_signal():
    fake = _FakeClient()
    reloaded = []
    w = ConfigWatcher(fake, lambda: reloaded.append(True), site_id="s1")
    w.handle_message(b'{"kind":"ems"}')
    assert fake.syncs == 1 and reloaded == [True]


def test_watcher_backstop_resyncs():
    fake = _FakeClient()
    reloaded = []
    w = ConfigWatcher(fake, lambda: reloaded.append(True), site_id="s1", backstop_s=5)
    w.backstop_tick()
    assert fake.syncs == 1 and reloaded == [True]


def test_watcher_keeps_running_when_resync_fails():
    fake = _FakeClient(fail=True)
    reloaded = []
    w = ConfigWatcher(fake, lambda: reloaded.append(True), site_id="s1")
    w.handle_message()  # must not raise
    assert fake.syncs == 1 and reloaded == []  # on_synced skipped on failure
