"""Controller config client tests (ADR-0001 Phase 3).

The Controller consumes config from Core with a last-known-good cache. The
client's HTTP fetch is backed here by Core's real config app via TestClient, so
the round-trip exercises the actual Core endpoints (no mock payloads).
"""

import shutil

import pytest
from common.config_manager import ConfigManager
from common.data_model import DataModel
from controller.config_client import CoreConfigClient
from core.config_api import create_config_app
from fastapi.testclient import TestClient


@pytest.fixture()
def core_server(repo_root, dm, tmp_path):
    """A live Core config app over throwaway config copies."""
    a = tmp_path / "src_assets.yaml"
    e = tmp_path / "src_ems.yaml"
    shutil.copy(repo_root / "configs" / "asset_config.example.yaml", a)
    shutil.copy(repo_root / "configs" / "edge_ems_config.example.yaml", e)
    cm = ConfigManager(dm, a, e)
    tc = TestClient(create_config_app(cm, dm, repo_root / "data_model.yaml"))
    return tc


def _client(core_tc, cache_dir, *, up=True):
    def get_json(url, timeout):
        if not up:
            raise OSError("core unreachable")
        return core_tc.get(url).json()  # url is just the path (base_url="")
    return CoreConfigClient("", cache_dir, get_json=get_json)


def test_sync_populates_cache_and_builds_config(core_server, tmp_path, dm):
    cache = tmp_path / "cache"
    paths = _client(core_server, cache).sync()
    assert paths.assets.exists() and paths.ems.exists() and paths.data_model.exists()
    # the cached files are consumable by the same ConfigManager
    dm2 = DataModel.load(paths.data_model)
    cm = ConfigManager(dm2, paths.assets, paths.ems)
    assert cm.asset_config.site.id == "vgcolab-01"
    assert dm2.version == "0.1"


def test_last_known_good_used_when_core_down(core_server, tmp_path):
    cache = tmp_path / "cache"
    _client(core_server, cache, up=True).sync()         # warm the cache
    paths = _client(core_server, cache, up=False).sync()  # Core down -> fall back
    assert paths.assets.exists()  # served from cache, no exception


def test_cold_start_without_core_or_cache_raises(core_server, tmp_path):
    cache = tmp_path / "empty_cache"
    with pytest.raises(RuntimeError) as exc:
        _client(core_server, cache, up=False).sync()
    assert "no local cache" in str(exc.value)


def test_secret_placeholder_preserved_in_cache(core_server, tmp_path, dm, monkeypatch):
    cache = tmp_path / "cache"
    paths = _client(core_server, cache).sync()
    on_disk = paths.ems.read_text(encoding="utf-8")
    assert "${INFLUX_TOKEN}" in on_disk and "real-secret" not in on_disk
    # live config resolves the secret from the Controller's own environment
    monkeypatch.setenv("INFLUX_TOKEN", "real-secret-xyz")
    cm = ConfigManager(DataModel.load(paths.data_model), paths.assets, paths.ems)
    assert cm.ems_config.influxdb.token == "real-secret-xyz"
