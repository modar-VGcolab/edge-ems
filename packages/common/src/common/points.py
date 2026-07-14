"""Point values and quality flags — shared vocabulary for both services.

Quality enum matches data_model.yaml: GOOD (fresh), STALE (older than the
timeout), COMM_FAIL (adapter reported failure / nothing contributed).
"""

from __future__ import annotations

from dataclasses import dataclass

GOOD = "GOOD"
STALE = "STALE"
COMM_FAIL = "COMM_FAIL"


@dataclass(frozen=True)
class PointValue:
    value: float | None
    ts: float  # unix seconds
    quality: str = GOOD
