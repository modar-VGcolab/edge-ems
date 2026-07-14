"""External-EMS emulator: random P* publisher with an outage/reconnect timeline.

Publishes a fresh random active-power setpoint (within the PCC limits) on the
external topic every publish-interval, EXCEPT during a simulated outage window
[outage_at, reconnect_at) when it goes silent so the gateway's watchdog can take
over. Setpoints are pure-random each run (within EXT_P_MIN_KW..EXT_P_MAX_KW), so
no two runs are identical. A real external EMS later replaces this by publishing
to the same topic.
"""

from __future__ import annotations

import json
import logging
import random
import time

log = logging.getLogger("ext_ems.emulator")


class Emulator:
    def __init__(
        self,
        publish: "callable",  # publish(topic, payload_str) -> None
        topic: str,
        p_min_kw: float,
        p_max_kw: float,
        publish_interval_s: float,
        outage_at_s: float,
        reconnect_at_s: float,
        rng: random.Random | None = None,
    ):
        self._publish = publish
        self._topic = topic
        self._p_min = p_min_kw
        self._p_max = p_max_kw
        self._interval = publish_interval_s
        self._outage_at = outage_at_s
        self._reconnect_at = reconnect_at_s
        self._rng = rng or random.Random()
        self._seq = 0

    def _in_outage(self, elapsed: float) -> bool:
        return self._outage_at <= elapsed < self._reconnect_at

    def next_setpoint(self) -> float:
        return round(self._rng.uniform(self._p_min, self._p_max), 1)

    def make_message(self, elapsed: float) -> dict | None:
        """The message that *would* be published at this elapsed time, or None
        during the outage window. Pure (no I/O) for testing."""
        if self._in_outage(elapsed):
            return None
        self._seq += 1
        return {
            "pcc_setpoint_kw": self.next_setpoint(),
            "seq": self._seq,
            "ts": time.time(),
        }

    def publish_tick(self, elapsed: float) -> dict | None:
        msg = self.make_message(elapsed)
        if msg is None:
            log.info("emulator: in outage window (elapsed=%.1fs) -> staying silent", elapsed)
            return None
        self._publish(self._topic, json.dumps(msg))
        log.info("emulator: published P*=%.1f kW seq=%d", msg["pcc_setpoint_kw"], msg["seq"])
        return msg
