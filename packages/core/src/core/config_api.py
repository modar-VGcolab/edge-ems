"""Core configuration API (ADR-0001) — Core is the config authority.

Serves the per-site configuration (assets, ems) for read / dry-run / update and
exposes the versioned data model read-only. It reuses the shared
``common.config_manager.ConfigManager`` so behavior is identical to the
Controller's former config surface — the Phase 0 characterization tests freeze
that contract.

Scope notes:
  * The data model is a versioned, release-shipped artifact: served read-only,
    never uploaded at runtime (ADR-0001).
  * Phase 4 loop-state gating: a *structural* asset change (asset added /
    removed / reclassified) is refused (409) while the Controller's control loop
    is running. Core learns the loop state through an injected ``loop_state``
    provider (default: HTTP GET to the Controller's /loop/state). If the state
    cannot be confirmed "stopped" (provider missing is the only no-gate case;
    an error/unknown is treated conservatively as not-stopped), the change is
    refused.
  * Phase 5 change notification: on every committed change Core calls the
    injected ``on_change(kind, info)`` hook (info carries a content hash and, for
    assets, the structural flag). ``main`` wires this to a retained MQTT publish
    on ``site/{site_id}/config/changed``; the Controller re-pulls on the signal.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

import yaml
from common.config_manager import ASSETS, EMS, ConfigManager
from common.data_model import DataModel
from fastapi import FastAPI, HTTPException


def is_structural_change(cm: ConfigManager, raw: dict) -> bool:
    """Asset added/removed/reclassified — refused while the loop runs."""
    try:
        new = {(a["id"], a["class"]) for a in raw.get("assets", [])}
    except (TypeError, KeyError):
        return True  # malformed enough to count as structural; validation speaks
    old = {(a.id, a.asset_class) for a in cm.asset_config.assets}
    return new != old


def _content_hash(raw: dict) -> str:
    return hashlib.sha256(
        yaml.safe_dump(raw, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def http_loop_state_provider(controller_url: str, timeout_s: float = 2.0):
    """Default provider: ask the Controller for its loop state over HTTP."""
    url = controller_url.rstrip("/") + "/loop/state"

    def provider() -> str | None:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8")).get("loop_state")

    return provider


def _confirmed_state(loop_state) -> str:
    """Return the loop state, or 'unknown' if the provider errors."""
    try:
        return loop_state() or "unknown"
    except Exception:  # noqa: BLE001 - unreachable controller -> conservative gate
        return "unknown"


def _emit(on_change, kind: str, info: dict) -> None:
    if on_change is None:
        return
    try:
        on_change(kind, info)
    except Exception:  # noqa: BLE001 - notification must never fail a committed write
        pass


def create_config_app(
    cm: ConfigManager,
    dm: DataModel,
    data_model_path: str | Path,
    *,
    loop_state=None,
    on_change=None,
) -> FastAPI:
    app = FastAPI(title="edge-ems-core-config", version="0.1.0")
    app.state.cm = cm

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "data_model_version": dm.version}

    # -- data model (read-only, versioned artifact) -----------------------------

    @app.get("/config/data_model")
    def get_data_model() -> dict:
        return yaml.safe_load(Path(data_model_path).read_text(encoding="utf-8"))

    # -- assets -----------------------------------------------------------------

    @app.get("/config/assets")
    def get_assets() -> dict:
        return cm.raw(ASSETS)

    @app.put("/config/assets")
    def put_assets(raw: dict) -> dict:
        errors = cm.validate(ASSETS, raw)
        if errors:
            raise HTTPException(422, errors)
        structural = is_structural_change(cm, raw)  # classify BEFORE applying
        if structural and loop_state is not None:
            state = _confirmed_state(loop_state)
            if state != "stopped":
                raise HTTPException(
                    409,
                    f"structural asset change requires the control loop stopped "
                    f"(loop is {state})",
                )
        cm.update(ASSETS, raw)
        info = {"hash": _content_hash(raw), "structural": structural}
        _emit(on_change, ASSETS, info)
        return {"applied": True, **info}

    @app.post("/config/assets/validate")
    def validate_assets(raw: dict) -> dict:
        errors = cm.validate(ASSETS, raw)
        return {"valid": not errors, "errors": errors}

    # -- ems --------------------------------------------------------------------

    @app.get("/config/ems")
    def get_ems() -> dict:
        return cm.raw(EMS)

    @app.put("/config/ems")
    def put_ems(raw: dict) -> dict:
        errors = cm.validate(EMS, raw)
        if errors:
            raise HTTPException(422, errors)
        cm.update(EMS, raw)
        info = {"hash": _content_hash(raw)}
        _emit(on_change, EMS, info)
        return {"applied": True, **info}

    @app.post("/config/ems/validate")
    def validate_ems(raw: dict) -> dict:
        errors = cm.validate(EMS, raw)
        return {"valid": not errors, "errors": errors}

    return app
