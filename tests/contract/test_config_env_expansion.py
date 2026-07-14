"""Regression for KNOWN_ISSUES #1: config ${VAR} placeholders must be expanded
from the environment for the LIVE config (so secrets like INFLUX_TOKEN resolve),
while the editable/persisted document keeps the placeholder (no secret leak)."""
import shutil

from controller.config_manager import EMS, ConfigManager


def test_core_load_expands_env(monkeypatch, tmp_path):
    monkeypatch.setenv("INFLUX_TOKEN", "real-secret-123")
    from core.main import _load

    p = tmp_path / "ems.yaml"
    p.write_text(
        'influxdb:\n  url: "http://x:8086"\n  org: edge\n  bucket: edge_ems\n'
        '  token: "${INFLUX_TOKEN}"\n',
        encoding="utf-8",
    )
    assert _load(str(p))["influxdb"]["token"] == "real-secret-123"


def _copy_examples(repo_root, tmp_path):
    a, e = tmp_path / "asset.yaml", tmp_path / "ems.yaml"
    shutil.copy(repo_root / "configs/asset_config.example.yaml", a)
    shutil.copy(repo_root / "configs/edge_ems_config.example.yaml", e)
    return a, e


def test_live_config_expands_token_but_raw_keeps_placeholder(monkeypatch, tmp_path, repo_root, dm):
    monkeypatch.setenv("INFLUX_TOKEN", "real-secret-123")
    a, e = _copy_examples(repo_root, tmp_path)
    cm = ConfigManager(dm, a, e)
    # live config the controller connects with -> resolved secret
    assert cm.ems_config.influxdb.token == "real-secret-123"
    # editable document exposed by the API -> placeholder preserved
    assert cm.raw(EMS)["influxdb"]["token"] == "${INFLUX_TOKEN}"


def test_put_roundtrip_keeps_file_verbatim_and_live_expanded(monkeypatch, tmp_path, repo_root, dm):
    monkeypatch.setenv("INFLUX_TOKEN", "real-secret-123")
    a, e = _copy_examples(repo_root, tmp_path)
    cm = ConfigManager(dm, a, e)
    cm.update(EMS, cm.raw(EMS))  # a GET -> PUT round-trip through the API
    text = e.read_text(encoding="utf-8")
    assert "${INFLUX_TOKEN}" in text          # file keeps the placeholder
    assert "real-secret-123" not in text       # secret is never persisted
    assert cm.ems_config.influxdb.token == "real-secret-123"  # live still resolved
