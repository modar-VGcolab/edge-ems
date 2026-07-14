"""The protocol seam: core talks to devices only through this interface.

Adding another device protocol (e.g. DNP3) later means a new module implementing DeviceAdapter —
nothing above this layer changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from common.points import COMM_FAIL, GOOD, STALE, PointValue

__all__ = [
    "COMM_FAIL",
    "GOOD",
    "STALE",
    "PointValue",
    "WriteResult",
    "AdapterHealth",
    "DeviceAdapter",
]


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class AdapterHealth:
    connected: bool = False
    consecutive_failures: int = 0
    last_ok_ts: float | None = None
    last_error: str | None = None


class DeviceAdapter(Protocol):
    async def connect(self) -> None: ...

    async def read_points(self, names: list[str]) -> dict[str, PointValue]: ...

    async def write_points(self, values: dict[str, float]) -> WriteResult: ...

    async def disconnect(self) -> None: ...

    def health(self) -> AdapterHealth: ...
