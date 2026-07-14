"""FastAPI status surface for the gateway (GET /status, GET /health).

Exposes the live takeover state so an operator (or the rig procedure) can watch
the FOLLOWING <-> SELF_CONSUMPTION transitions without reading logs or InfluxDB.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from fastapi import FastAPI


def create_status_app(status_provider: Callable[[float], dict]) -> FastAPI:
    app = FastAPI(title="ext-ems-gateway", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/status")
    def status() -> dict:
        return status_provider(time.monotonic())

    return app
