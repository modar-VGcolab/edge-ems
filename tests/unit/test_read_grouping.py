"""plan_reads grouping logic, tested on synthetic maps (decoupled from device
layouts so it stays valid as register maps evolve, e.g. SunSpec)."""
from common.modbus_codec import holding_offset
from common.register_map import RegisterMap
from core.adapters.modbus_tcp import plan_reads


def _map(points: dict) -> RegisterMap:
    return RegisterMap.model_validate(
        {"map_version": "t", "asset_class": "pcc", "points": points}
    )


def test_contiguous_inputs_become_one_block():
    rmap = _map({
        "a": {"address": 40001, "type": "uint16"},
        "b": {"address": 40002, "type": "uint16"},
        "c": {"address": 40003, "type": "uint16"},
    })
    blocks = plan_reads(rmap, ["a", "b", "c"])
    assert len(blocks) == 1
    assert blocks[0].start == holding_offset(40001)
    assert blocks[0].count == 3


def test_distant_points_split_into_two_blocks():
    rmap = _map({
        "a": {"address": 40001, "type": "uint16"},
        "b": {"address": 40101, "type": "int32"},
    })
    blocks = plan_reads(rmap, ["a", "b"])
    assert len(blocks) == 2
    assert blocks[1].start == holding_offset(40101)
    assert blocks[1].count == 2


def test_small_gap_is_merged():
    # offsets 0 and 3: gap of 2 <= max_gap -> single read
    rmap = _map({
        "a": {"address": 40001, "type": "uint16"},
        "b": {"address": 40004, "type": "uint16"},
    })
    blocks = plan_reads(rmap, ["a", "b"], max_gap=4)
    assert len(blocks) == 1
    assert blocks[0].start == 0
