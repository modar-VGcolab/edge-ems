"""Tests for the SunSpec firmware-reconciliation tooling (scripts/).

There is no real device here, so these exercise the walker/diff on *synthetic*
dumps derived from our own map images and on deliberately tampered copies. They
prove the tooling reproduces a known-good chain, reports a clean diff against the
map it came from, and flags the discrepancies that matter on real firmware
(scale factors, model placement, point types). They do NOT validate any map
against real hardware — that needs a real dump (see the prompt).
"""

import copy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import diff_dump_vs_map as ddm  # noqa: E402
import sunspec_discovery as sd  # noqa: E402

SUNSPEC_MAPS = ["custom_bess_v1.yaml", "custom_pv_inverter_v1.yaml", "grid_meter_v1.yaml",
                "flexible_load_v1.yaml", "meter_v1.yaml"]

# mirrors tests/contract/test_sunspec_chain.py
EXPECTED_CHAIN = {
    "custom_bess_v1.yaml": [(1, 66), (701, 153), (704, 65), (713, 7)],
    "custom_pv_inverter_v1.yaml": [(1, 66), (701, 153), (704, 65)],
    "grid_meter_v1.yaml": [(701, 153)],
    "flexible_load_v1.yaml": [(1, 66), (701, 153), (704, 65)],
    "meter_v1.yaml": [(701, 153)],
}
# the device-chosen SF our generator publishes today (maps/sunspec/generate.py)
EXPECTED_701_SF = {"A_SF": -1, "V_SF": -1, "Hz_SF": -2, "W_SF": 2, "PF_SF": -3,
                   "VA_SF": 2, "Var_SF": 2, "TotWh_SF": 0, "TotVarh_SF": 0, "Tmp_SF": -1}


@pytest.mark.parametrize("mapfile", SUNSPEC_MAPS)
def test_walk_reproduces_chain_and_scale_factors(repo_root, mapfile):
    dump = sd.dump_from_map(repo_root, mapfile)
    assert dump["suns_marker_ok"]
    assert dump["model_chain"] == EXPECTED_CHAIN[mapfile]
    m701 = next(m for m in dump["models"] if m["id"] == 701)
    assert m701["scale_factors"] == EXPECTED_701_SF


@pytest.mark.parametrize("mapfile", SUNSPEC_MAPS)
def test_raw_dump_reader_roundtrips(repo_root, mapfile):
    """A saved raw register dump ingests through RawDumpReader to the same chain."""
    raw = sd.raw_dump_from_map(repo_root, mapfile)
    catalog = sd.load_model_catalog(repo_root)
    dump = sd.walk(sd.RawDumpReader.from_obj(raw), catalog)
    assert dump["model_chain"] == EXPECTED_CHAIN[mapfile]
    # also accept the sequential-list form
    seq_words = [raw["words"][raw["base"] + i] for i in range(len(raw["words"]))]
    seq = {"base": raw["base"], "words": seq_words}
    dump2 = sd.walk(sd.RawDumpReader.from_obj(seq), catalog)
    assert dump2["model_chain"] == EXPECTED_CHAIN[mapfile]


@pytest.mark.parametrize("mapfile", SUNSPEC_MAPS)
def test_self_dump_diff_is_clean(repo_root, mapfile):
    dump = sd.dump_from_map(repo_root, mapfile)
    report = ddm.diff(dump, repo_root, mapfile)
    assert report["ok"], report["discrepancies"]
    assert report["summary"]["scale_factor_issues"] == 0
    # every canonical binding that lives inside a walked model must verify
    for c in report["canonical"]:
        assert c["status"] in ("ok", "manual")


def test_diff_flags_scale_factor_mismatch(repo_root):
    """The headline case: device publishes a different sunssf than the map chose."""
    dump = sd.dump_from_map(repo_root, "custom_bess_v1.yaml")
    tampered = copy.deepcopy(dump)
    m701 = next(m for m in tampered["models"] if m["id"] == 701)
    m701["scale_factors"]["W_SF"] = -1  # real device: W_SF=-1, map chose +2
    report = ddm.diff(tampered, repo_root, "custom_bess_v1.yaml")
    assert not report["ok"]
    assert any("W_SF" in d for d in report["scale_factor_issues"])
    # the canonical point governed by W_SF is flagged with both SF values
    ap = next(c for c in report["canonical"] if c["point"] == "active_power_kw")
    assert ap["status"] == "MISMATCH"
    assert ap["map_sf"] == 2 and ap["device_sf"] == -1


def test_diff_flags_missing_model(repo_root):
    dump = sd.dump_from_map(repo_root, "custom_bess_v1.yaml")
    tampered = copy.deepcopy(dump)
    tampered["models"] = [m for m in tampered["models"] if m["id"] != 713]
    tampered["model_chain"] = [(m["id"], m["header_length"]) for m in tampered["models"]]
    report = ddm.diff(tampered, repo_root, "custom_bess_v1.yaml")
    assert not report["ok"]
    assert any("713" in d for d in report["model_issues"])
    soc = next(c for c in report["canonical"] if c["point"] == "soc_pct")
    assert soc["status"] == "MISMATCH"


def test_diff_flags_point_type_mismatch(repo_root):
    dump = sd.dump_from_map(repo_root, "grid_meter_v1.yaml")
    tampered = copy.deepcopy(dump)
    m701 = next(m for m in tampered["models"] if m["id"] == 701)
    w = next(p for p in m701["points"] if p["name"] == "W")
    w["type"] = "int32"  # real device implements W as int32, map says int16
    report = ddm.diff(tampered, repo_root, "grid_meter_v1.yaml")
    assert not report["ok"]
    assert any("W@" in d and "type" in d for d in report["point_issues"])


def test_diff_flags_model_length_mismatch(repo_root):
    dump = sd.dump_from_map(repo_root, "custom_pv_inverter_v1.yaml")
    tampered = copy.deepcopy(dump)
    m704 = next(m for m in tampered["models"] if m["id"] == 704)
    m704["header_length"] = 60  # different firmware version / optional points
    tampered["model_chain"] = [(m["id"], m["header_length"]) for m in tampered["models"]]
    report = ddm.diff(tampered, repo_root, "custom_pv_inverter_v1.yaml")
    assert not report["ok"]
    assert any("length/version" in d for d in report["model_issues"])


def test_walk_rejects_non_sunspec_image(repo_root):
    # every repo map is SunSpec now, so synthesize a device whose base words are
    # not the SunS marker and confirm the walk refuses it.
    catalog = sd.load_model_catalog(repo_root)
    reader = sd.RawDumpReader.from_obj({"base": 40001, "words": {40001: 0x1234, 40002: 0x5678}})
    with pytest.raises(sd.SunSError):
        sd.walk(reader, catalog)
