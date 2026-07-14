"""Controller runtime wiring (Phase 3 service entry).

Assembles the live ControlLoop from config and runs it on a background thread at
the configured period, with overrun-skip so a slow cycle never lets ticks pile
up (system design 5). The HTTP API drives this via /loop/start and /loop/stop.

The InfluxDB read path and the MQTT publish path live here (not in the tested
core algorithms); they are thin and only exercised in SIL.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable

from common.data_model import DataModel
from common.points import GOOD, STALE, PointValue

from controller.config_manager import ConfigManager
from controller.control_loop import (
    ControlLoop,
    CycleResult,
    Snapshot,
    battery_limits_from_config,
    derate_limits_from_config,
    params_from_config,
    pcc_params_from_config,
)
from controller.data_connector import InfluxAggregateReader
from controller.droop import DroopController
from controller.edge_controller import EdgeController
from controller.modes import ModeController


class LiveSnapshotReader:
    """Reads the `aggregate` (battery/pv/flexible_load) and `pcc` measurements
    from InfluxDB into a Snapshot, marking points older than timeout STALE."""

    def __init__(
        self,
        dm: DataModel,
        site_id: str,
        agg_reader: InfluxAggregateReader,
        pcc_query,  # callable returning iterable of (field, value, ts_unix)
        agg_classes: Iterable[str] = ("battery", "pv", "flexible_load"),
        timeout_s: float = 2.0,
    ):
        self._dm = dm
        self._site_id = site_id
        self._agg = agg_reader
        self._pcc_query = pcc_query
        self._agg_classes = list(agg_classes)
        self._timeout = timeout_s

    def __call__(self, now: float) -> Snapshot:
        aggregates = self._agg.read(self._agg_classes, now=now)
        pcc: dict[str, PointValue] = {}
        for field, value, ts in self._pcc_query():
            if self._dm.validate_fields("pcc", [field], direction="input"):
                continue
            quality = GOOD if (now - ts) <= self._timeout else STALE
            pcc[field] = PointValue(value, ts, quality)
        return Snapshot(aggregates=aggregates, pcc=pcc)


def build_control_loop(cm: ConfigManager, dm: DataModel, *, read_snapshot, publish, write_control):
    """Construct a ControlLoop from the active config and injected I/O callables."""
    ac, ec = cm.asset_config, cm.ems_config
    params = params_from_config(ec)
    edge = EdgeController(params, battery_limits_from_config(ac), derate_limits_from_config(ac))
    base, max_feed = pcc_params_from_config(ac)
    droop = DroopController(ec.droop, base) if ec.droop.enabled else None
    return ControlLoop(
        edge=edge,
        modes=ModeController(hold_max_s=ec.controller.hold_max_s),
        droop=droop,
        read_snapshot=read_snapshot,
        publish=publish,
        pcc_base_kw=base,
        max_feed_kw=max_feed,
        pcc_setpoint_kw=ec.controller.pcc_setpoint_kw,
        write_control=write_control,
    )


class LoopRunner:
    """Background driver with the LoopHandle interface (start/stop/state) plus the
    last cycle result for /status. Scheduling uses a monotonic clock and skips a
    tick rather than running long (overrun protection)."""

    def __init__(self, loop: ControlLoop, period_s: float):
        self._loop = loop
        self._period = period_s
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last: CycleResult | None = None
        self._overruns = 0

    @property
    def state(self) -> str:
        return "running" if self._thread and self._thread.is_alive() else "stopped"

    @property
    def last_result(self) -> CycleResult | None:
        return self._last

    @property
    def overruns(self) -> int:
        return self._overruns

    @property
    def pcc_setpoint_kw(self) -> float:
        """The live PCC target the running loop regulates to."""
        return self._loop.pcc_setpoint_kw

    def set_pcc_setpoint_kw(self, value: float) -> float:
        """Set the live PCC target on the *running* loop instance (no restart).

        The external-EMS gateway calls this via POST /setpoint so it can drive the
        controller's PCC target message-by-message. Returns the applied value.
        """
        self._loop.pcc_setpoint_kw = float(value)
        return self._loop.pcc_setpoint_kw

    def start(self) -> bool:
        if self.state == "running":
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="control-loop", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> bool:
        if self.state != "running":
            return False
        self._stop.set()
        self._thread.join(timeout=self._period * 5)
        self._thread = None
        return True

    def status(self) -> dict:
        r = self._last
        if r is None:
            return {
                "loop_state": self.state,
                "overruns": self._overruns,
                "pcc_setpoint_kw": self._loop.pcc_setpoint_kw,
            }
        return {
            "loop_state": self.state,
            "overruns": self._overruns,
            "pcc_setpoint_kw": self._loop.pcc_setpoint_kw,
            "mode": r.mode,
            "pcc_error_kw": r.pcc_error_kw,
            "battery_setpoint_kw": r.battery_setpoint_kw,
            "derate_factor": r.derate_factor,
            "curtail_factor": r.curtail_factor,
            "loop_duration_ms": r.loop_duration_ms,
            "data_fresh": r.data_fresh,
            "battery_ok": r.battery_ok,
        }

    def _run(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            try:
                self._last = self._loop.run_once(time.time())
            except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
                pass
            next_tick += self._period
            sleep = next_tick - time.monotonic()
            if sleep < 0:  # overrun: drop missed ticks, resync
                self._overruns += 1
                next_tick = time.monotonic()
                sleep = 0.0
            self._stop.wait(sleep)
