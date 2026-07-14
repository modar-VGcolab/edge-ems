from pathlib import Path

import pytest
import yaml
from common.data_model import DataModel

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def dm() -> DataModel:
    return DataModel.load(REPO_ROOT / "data_model.yaml")


@pytest.fixture()
def asset_config_raw() -> dict:
    return yaml.safe_load(
        (REPO_ROOT / "configs" / "asset_config.example.yaml").read_text(encoding="utf-8")
    )


@pytest.fixture()
def edge_ems_config_raw() -> dict:
    return yaml.safe_load(
        (REPO_ROOT / "configs" / "edge_ems_config.example.yaml").read_text(encoding="utf-8")
    )
