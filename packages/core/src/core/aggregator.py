"""Aggregation by asset class (plan task 15).

The controller sees one virtual asset per class; core.py owns the mapping from
N physical assets to that aggregate (and back again in the dispatcher).

Rules: power/current points are summed; percentage points (``*_pct``) are
weighted averages (weight = nominal capacity, defaulting to 1.0). Assets whose
point is not GOOD are excluded from that point's aggregate; if nothing
contributes, the aggregate point is COMM_FAIL.
"""

from __future__ import annotations

import time

from common.data_model import DataModel

from core.adapters.base import COMM_FAIL, GOOD, PointValue


def aggregate_class(
    asset_class: str,
    dm: DataModel,
    readings: dict[str, dict[str, PointValue]],
    weights: dict[str, float] | None = None,
) -> dict[str, PointValue]:
    """Aggregate per-asset readings into one virtual asset.

    readings: {asset_id: {point_name: PointValue}}
    weights:  {asset_id: weight} for ``*_pct`` averaging (e.g. capacity_kwh).
    """
    point_names = dm.aggregates.get(asset_class)
    if point_names is None:
        raise ValueError(f"asset class '{asset_class}' is not aggregated (see data model)")
    ts = time.time()
    out: dict[str, PointValue] = {}
    for pname in point_names:
        values: list[float] = []
        ws: list[float] = []
        for asset_id, points in readings.items():
            pv = points.get(pname)
            if pv is None or pv.quality != GOOD or pv.value is None:
                continue
            values.append(pv.value)
            ws.append((weights or {}).get(asset_id, 1.0))
        if not values:
            out[pname] = PointValue(None, ts, COMM_FAIL)
        elif pname.endswith("_pct"):
            total_w = sum(ws)
            out[pname] = PointValue(
                sum(v * w for v, w in zip(values, ws)) / (total_w if total_w else len(values)),
                ts,
                GOOD,
            )
        else:
            out[pname] = PointValue(sum(values), ts, GOOD)
    return out
