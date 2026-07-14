"""Modbus TCP implementation of DeviceAdapter (plan task 13).

- Register grouping: contiguous/near-contiguous points are read in one request.
- SunSpec live scaling: a point with `sf_address` is decoded raw * 10^SF, where
  the (static) scale-factor register is read once and cached.
- Failures never raise out of read/write: points come back quality=COMM_FAIL
  and reconnects use capped exponential backoff.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from common.modbus_codec import _words_to_int, decode, encode, holding_offset
from common.register_map import RegisterDef, RegisterMap
from pymodbus.client import AsyncModbusTcpClient

from core.adapters.base import COMM_FAIL, GOOD, AdapterHealth, PointValue, WriteResult


@dataclass(frozen=True)
class ReadBlock:
    start: int  # zero-based holding offset
    count: int
    points: list[tuple[str, RegisterDef]]


def plan_reads(rmap: RegisterMap, names: list[str], max_gap: int = 4) -> list[ReadBlock]:
    """Group requested points into minimal contiguous read requests."""
    regs = sorted(
        ((n, rmap.points[n]) for n in names),
        key=lambda item: holding_offset(item[1].address),
    )
    blocks: list[ReadBlock] = []
    current: list[tuple[str, RegisterDef]] = []
    start = end = 0
    for name, reg in regs:
        off = holding_offset(reg.address)
        if current and off - end > max_gap:
            blocks.append(ReadBlock(start, end - start, current))
            current = []
        if not current:
            start = off
        current.append((name, reg))
        end = off + reg.width
    if current:
        blocks.append(ReadBlock(start, end - start, current))
    return blocks


class ModbusTcpAdapter:
    def __init__(
        self,
        host: str,
        rmap: RegisterMap,
        port: int = 502,
        unit_id: int = 1,
        timeout: float = 2.0,
        max_gap: int = 4,
        max_backoff: float = 30.0,
    ):
        self._host = host
        self._port = port
        self._unit_id = unit_id
        self._timeout = timeout
        self._max_gap = max_gap
        self._max_backoff = max_backoff
        self.rmap = rmap
        self._client: AsyncModbusTcpClient | None = None
        self._health = AdapterHealth()
        self._backoff = 1.0
        self._next_attempt = 0.0
        self._sf_cache: dict[int, int] = {}  # sf_address -> scale factor (static)

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        self._client = AsyncModbusTcpClient(self._host, port=self._port, timeout=self._timeout)
        ok = await self._client.connect()
        if not ok:
            raise ConnectionError(f"could not connect to {self._host}:{self._port}")
        self._health.connected = True

    async def disconnect(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._health.connected = False

    def health(self) -> AdapterHealth:
        return self._health

    async def _ensure_connected(self) -> bool:
        if self._client is not None and self._client.connected:
            return True
        now = time.monotonic()
        if now < self._next_attempt:
            return False
        try:
            await self.connect()
            self._backoff = 1.0
            return True
        except Exception as exc:  # noqa: BLE001 - adapter must never raise upward
            self._record_failure(str(exc))
            self._next_attempt = now + self._backoff
            self._backoff = min(self._backoff * 2.0, self._max_backoff)
            return False

    # -- SunSpec scale factors (static; read once and cached) ----------------

    async def _ensure_sf(self, names: list[str]) -> None:
        need = {
            self.rmap.points[n].sf_address
            for n in names
            if self.rmap.points[n].sf_address is not None
            and self.rmap.points[n].sf_address not in self._sf_cache
        }
        for addr in need:
            try:
                rr = await self._client.read_holding_registers(
                    holding_offset(addr), count=1, device_id=self._unit_id
                )
                if not rr.isError():
                    self._sf_cache[addr] = _words_to_int("sunssf", list(rr.registers))
            except Exception:  # noqa: BLE001
                pass

    def _sf_for(self, reg: RegisterDef) -> int | None:
        return self._sf_cache.get(reg.sf_address) if reg.sf_address is not None else None

    # -- data path -----------------------------------------------------------

    async def read_points(self, names: list[str]) -> dict[str, PointValue]:
        ts = time.time()
        if not await self._ensure_connected():
            return {n: PointValue(None, ts, COMM_FAIL) for n in names}
        await self._ensure_sf(names)
        out: dict[str, PointValue] = {}
        for block in plan_reads(self.rmap, names, self._max_gap):
            try:
                rr = await self._client.read_holding_registers(
                    block.start, count=block.count, device_id=self._unit_id
                )
                if rr.isError():
                    raise OSError(str(rr))
                words = rr.registers
            except Exception as exc:  # noqa: BLE001
                self._record_failure(str(exc))
                for name, _ in block.points:
                    out[name] = PointValue(None, ts, COMM_FAIL)
                continue
            for name, reg in block.points:
                o = holding_offset(reg.address) - block.start
                value = decode(reg, list(words[o : o + reg.width]), sf=self._sf_for(reg))
                out[name] = PointValue(value, ts, GOOD)
            self._record_ok()
        return out

    async def write_points(self, values: dict[str, float]) -> WriteResult:
        if not await self._ensure_connected():
            return WriteResult(False, {n: "not connected" for n in values})
        await self._ensure_sf(list(values))
        errors: dict[str, str] = {}
        for name, value in values.items():
            reg = self.rmap.points.get(name)
            if reg is None:
                errors[name] = "unknown point"
                continue
            if reg.rw == "r":
                errors[name] = "register is read-only"
                continue
            try:
                rr = await self._client.write_registers(
                    holding_offset(reg.address), encode(reg, value, sf=self._sf_for(reg)),
                    device_id=self._unit_id,
                )
                if rr.isError():
                    raise OSError(str(rr))
            except Exception as exc:  # noqa: BLE001
                self._record_failure(str(exc))
                errors[name] = str(exc)
        if not errors:
            self._record_ok()
        return WriteResult(not errors, errors)

    # -- bookkeeping ----------------------------------------------------------

    def _record_ok(self) -> None:
        self._health.connected = True
        self._health.consecutive_failures = 0
        self._health.last_ok_ts = time.time()

    def _record_failure(self, error: str) -> None:
        self._health.connected = False
        self._health.consecutive_failures += 1
        self._health.last_error = error
