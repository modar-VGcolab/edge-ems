"""POST the effective PCC setpoint to the controller's live /setpoint endpoint.

Best-effort: a controller hiccup must not crash the gateway. A failed POST
returns False so the caller keeps the previous "last forwarded" value and
retries on the next change/tick.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("ext_ems.forwarder")


class ControllerForwarder:
    def __init__(self, controller_url: str, api_token: str = "", timeout_s: float = 2.0):
        self._url = controller_url.rstrip("/") + "/setpoint"
        # /setpoint is gated when the controller has CONTROLLER_API_TOKEN set
        # (KNOWN_ISSUES #4); an empty token means auth is off there too, so
        # sending an empty header is harmless against an ungated controller.
        headers = {"X-API-Key": api_token} if api_token else {}
        self._client = httpx.Client(timeout=timeout_s, headers=headers)

    def forward(self, pcc_setpoint_kw: float) -> bool:
        try:
            resp = self._client.post(self._url, json={"pcc_setpoint_kw": pcc_setpoint_kw})
            resp.raise_for_status()
            log.info("forwarded PCC setpoint %.1f kW -> controller", pcc_setpoint_kw)
            return True
        except Exception as exc:  # noqa: BLE001 - controller loss must not kill the gateway
            log.warning("failed to forward setpoint %.1f kW: %s", pcc_setpoint_kw, exc)
            return False

    def close(self) -> None:
        self._client.close()
