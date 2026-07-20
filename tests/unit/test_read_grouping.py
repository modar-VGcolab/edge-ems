"""plan_reads grouping logic, tested on synthetic maps (decoupled from device
layouts so it stays valid as register maps evolve, e.g. SunSpec)."""
from common.modbus_codec import holding_offset
from common.register_map import RegisterMap
from core.adapters.modbus_tcp import MAX_READ_REGISTERS, plan_reads


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


# -- span cap (Modbus 125-register limit) ------------------------------------
#
# Regression: a densely populated map (e.g. a full walkable SunSpec image) used
# to group into ONE block spanning hundreds of registers. The device rejects
# such a request, which surfaces as COMM_FAIL on every point while the socket
# stays up -- it reads like a comms fault, not an illegal request.


def _dense_map(n: int) -> RegisterMap:
    # n contiguous uint16 points -> a span of n registers if grouped as one
    return _map({f"p{i}": {"address": 40001 + i, "type": "uint16"} for i in range(n)})


def test_no_block_exceeds_the_modbus_limit():
    rmap = _dense_map(300)
    blocks = plan_reads(rmap, [f"p{i}" for i in range(300)])
    assert len(blocks) > 1  # must have split
    assert all(b.count <= MAX_READ_REGISTERS for b in blocks)


def test_split_blocks_cover_every_point_exactly_once():
    names = [f"p{i}" for i in range(300)]
    blocks = plan_reads(_dense_map(300), names)
    covered = [n for b in blocks for n, _ in b.points]
    assert sorted(covered) == sorted(names)  # nothing dropped or duplicated


def test_block_at_the_limit_is_not_split():
    n = MAX_READ_REGISTERS  # exactly 125 registers -> still one legal request
    blocks = plan_reads(_dense_map(n), [f"p{i}" for i in range(n)])
    assert len(blocks) == 1
    assert blocks[0].count == MAX_READ_REGISTERS


def test_wide_registers_respect_the_limit():
    # int32 points are 2 registers each; the cap must count registers, not points
    rmap = _map({f"w{i}": {"address": 40001 + 2 * i, "type": "int32"} for i in range(100)})
    blocks = plan_reads(rmap, [f"w{i}" for i in range(100)])
    assert all(b.count <= MAX_READ_REGISTERS for b in blocks)
