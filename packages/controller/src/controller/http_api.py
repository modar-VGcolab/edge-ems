"""HTTP API (plan task 11) — the only control surface core.py and operators use.

Loop endpoints drive a LoopHandle; the real control loop replaces the stub in
Phase 3 without changing this module's contract (system design §3.1).
"""

from __future__ import annotations

from common.data_model import DataModel
from fastapi import FastAPI, HTTPException

from controller.config_manager import ASSETS, EMS, ConfigManager


class LoopHandle:
    """Stub until Phase 3: tracks state, enforces start/stop semantics."""

    def __init__(self):
        self.state = "stopped"

    def start(self) -> bool:
        if self.state == "running":
            return False
        self.state = "running"
        return True

    def stop(self) -> bool:
        if self.state == "stopped":
            return False
        self.state = "stopped"
        return True


def _structural_change(cm: ConfigManager, raw: dict) -> bool:
    """Asset added/removed/reclassified — requires the loop to be stopped."""
    try:
        new = {(a["id"], a["class"]) for a in raw.get("assets", [])}
    except (TypeError, KeyError):
        return True  # malformed enough to count as structural; validation will speak
    old = {(a.id, a.asset_class) for a in cm.asset_config.assets}
    return new != old


def create_app(cm: ConfigManager, dm: DataModel, loop: LoopHandle | None = None) -> FastAPI:
    loop = loop or LoopHandle()
    app = FastAPI(title="edge-ems-controller", version="0.1.0")
    app.state.loop = loop

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "data_model_version": dm.version,
            "loop_state": loop.state,
        }

    @app.get("/status")
    def status() -> dict:
        cfg = cm.asset_config
        out = {
            "loop_state": loop.state,
            "site_id": cfg.site.id,
            "data_model_version": dm.version,
            "assets": {a.id: {"class": a.asset_class, "state": a.state} for a in cfg.assets},
        }
        # When a live LoopRunner is attached, surface the last cycle (mode, PCC
        # error, active mitigations) per system design 3.1.
        if hasattr(loop, "status"):
            out["last_cycle"] = loop.status()
        return out

    # -- config -----------------------------------------------------------------

    @app.get("/config/assets")
    def get_assets() -> dict:
        return cm.raw(ASSETS)

    @app.put("/config/assets")
    def put_assets(raw: dict) -> dict:
        if loop.state == "running" and _structural_change(cm, raw):
            raise HTTPException(409, "structural asset change requires loop stop")
        errors = cm.validate(ASSETS, raw)
        if errors:
            raise HTTPException(422, errors)
        cm.update(ASSETS, raw)
        return {"applied": True}

    @app.post("/config/assets/validate")
    def validate_assets(raw: dict) -> dict:
        errors = cm.validate(ASSETS, raw)
        return {"valid": not errors, "errors": errors}

    @app.get("/config/ems")
    def get_ems() -> dict:
        return cm.raw(EMS)

    @app.put("/config/ems")
    def put_ems(raw: dict) -> dict:
        errors = cm.validate(EMS, raw)
        if errors:
            raise HTTPException(422, errors)
        cm.update(EMS, raw)
        return {"applied": True}  # picked up at the next cycle boundary

    @app.post("/config/ems/validate")
    def validate_ems(raw: dict) -> dict:
        errors = cm.validate(EMS, raw)
        return {"valid": not errors, "errors": errors}

    # -- loop lifecycle -----------------------------------------------------------

    @app.post("/loop/start")
    def loop_start() -> dict:
        if not loop.start():
            raise HTTPException(409, "loop already running")
        return {"loop_state": loop.state}

    @app.post("/loop/stop")
    def loop_stop() -> dict:
        if not loop.stop():
            raise HTTPException(409, "loop already stopped")
        return {"loop_state": loop.state}

    @app.get("/loop/state")
    def loop_state() -> dict:
        # ADR-0001 Phase 4: Core consults this before committing a structural
        # asset change, so it can refuse one while the loop is running.
        return {"loop_state": loop.state}

    # -- live PCC setpoint --------------------------------------------------------

    @app.post("/setpoint")
    def set_setpoint(body: dict) -> dict:
        """Set the live PCC target on the running loop (no restart).

        The external-EMS gateway (S1 takeover/release) drives this per message so
        the controller follows an external EMS without a restart between commands.
        Body: {"pcc_setpoint_kw": <float>}; an optional "reactive_setpoint_kvar"
        is accepted but ignored for now (Q* is a future extension). Idempotent.
        """
        if "pcc_setpoint_kw" not in body:
            raise HTTPException(422, "pcc_setpoint_kw required")
        try:
            value = float(body["pcc_setpoint_kw"])
        except (TypeError, ValueError):
            raise HTTPException(422, "pcc_setpoint_kw must be a number") from None
        setter = getattr(loop, "set_pcc_setpoint_kw", None)
        if setter is None:
            raise HTTPException(503, "live setpoint requires a running LoopRunner")
        applied = setter(value)
        return {"pcc_setpoint_kw": applied}

    return app
