"""SIL fixtures. These tests only run when EDGE_EMS_SIL=1 and the stack is up;
otherwise they skip so the default `pytest` stays infra-free. The assertion
unit tests (test_sil_assertions.py) always run."""

import os

import pytest

SIL_ENABLED = os.environ.get("EDGE_EMS_SIL") == "1"


@pytest.fixture(scope="session")
def sil_env():
    if not SIL_ENABLED:
        pytest.skip("SIL disabled (set EDGE_EMS_SIL=1 with the stack running)")
    return {
        "influx_url": os.environ.get("INFLUX_URL", "http://localhost:8086"),
        "influx_org": os.environ.get("INFLUX_ORG", "edge"),
        "influx_bucket": os.environ.get("INFLUX_BUCKET", "edge_ems"),
        "influx_token": os.environ.get("INFLUX_TOKEN", "change-me"),
        "controller_url": os.environ.get("CONTROLLER_URL", "http://localhost:5000"),
        "site_id": os.environ.get("SITE_ID", "vgcolab-01"),
    }
