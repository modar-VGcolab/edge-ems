"""Config lifecycle: load, validate, dry-run, atomic write.

Moved here from `controller.config_manager` under ADR-0001 so that Core (the
configuration authority) and the Controller share one implementation. The
Controller keeps a thin re-export shim for backward compatibility.

The active config is only ever replaced by one that passed full validation —
a bad PUT can never leave a service with a broken file.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import yaml
from common.config_models import (
    AssetConfigFile,
    EdgeEmsConfigFile,
    validate_asset_config,
    validate_edge_ems_config,
)
from common.data_model import DataModel

ASSETS = "assets"
EMS = "ems"


class ConfigManager:
    def __init__(self, dm: DataModel, asset_path: str | Path, ems_path: str | Path):
        self._dm = dm
        self._paths = {ASSETS: Path(asset_path), EMS: Path(ems_path)}
        self._lock = threading.Lock()
        self._assets: AssetConfigFile | None = None
        self._ems: EdgeEmsConfigFile | None = None
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self._assets = validate_asset_config(self._read_raw(ASSETS), self._dm)
            self._ems = validate_edge_ems_config(self._read_raw(EMS), self._dm)

    @property
    def asset_config(self) -> AssetConfigFile:
        return self._assets

    @property
    def ems_config(self) -> EdgeEmsConfigFile:
        return self._ems

    def raw(self, which: str) -> dict:
        # The editable document exposed by the API: read VERBATIM so ${VAR}
        # placeholders (secrets) are never exposed or written back to disk.
        return yaml.safe_load(self._paths[which].read_text(encoding="utf-8"))

    def validate(self, which: str, raw: dict) -> list[str]:
        """Dry run: return validation errors without touching anything."""
        try:
            self._validate(which, raw)
            return []
        except ValueError as exc:  # pydantic ValidationError subclasses ValueError
            return [str(exc)]

    def update(self, which: str, raw: dict) -> None:
        """Validate, then atomically persist and swap the active config."""
        validated = self._validate(which, raw)
        with self._lock:
            path = self._paths[which]
            tmp = path.with_suffix(".tmp")
            tmp.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            tmp.replace(path)
            # Rebuild the live config from disk with ${VAR} expanded, so the
            # resolved secret survives a config PUT (the persisted file keeps the
            # placeholder). `validated` only confirmed the submitted document.
            _ = validated
            if which == ASSETS:
                self._assets = validate_asset_config(self._read_raw(ASSETS), self._dm)
            else:
                self._ems = validate_edge_ems_config(self._read_raw(EMS), self._dm)

    # -- internals --------------------------------------------------------------

    def _read_raw(self, which: str) -> dict:
        # Builds the LIVE config the service connects with (InfluxDB/MQTT):
        # expand ${VAR} from the environment so the real secret is used.
        text = self._paths[which].read_text(encoding="utf-8")
        return yaml.safe_load(os.path.expandvars(text))

    def _validate(self, which: str, raw: dict):
        if which == ASSETS:
            return validate_asset_config(raw, self._dm)
        if which == EMS:
            return validate_edge_ems_config(raw, self._dm)
        raise ValueError(f"unknown config kind '{which}'")
