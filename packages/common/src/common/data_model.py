"""Loader for data_model.yaml — the canonical data model (ontology).

Single source of truth for every named quantity crossing an interface.
Nothing else in the codebase may define point names, units, or signs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_META_KEYS = {"unit", "type", "direction"}


@dataclass(frozen=True)
class PointDef:
    name: str
    unit: str
    type: str
    direction: str  # "input" | "output"
    meta: dict[str, Any]


@dataclass(frozen=True)
class AssetClassDef:
    name: str
    description: str
    flexibility: dict[str, Any]
    points: dict[str, PointDef]
    limits_schema: dict[str, dict[str, Any]]
    nominal_schema: dict[str, dict[str, Any]]

    @property
    def control(self) -> str:
        return str(self.flexibility.get("control", "none"))

    def points_by_direction(self, direction: str) -> dict[str, PointDef]:
        return {n: p for n, p in self.points.items() if p.direction == direction}


@dataclass(frozen=True)
class DataModel:
    version: str
    asset_classes: dict[str, AssetClassDef]
    aggregates: dict[str, list[str]]

    @classmethod
    def load(cls, path: str | Path) -> DataModel:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        classes: dict[str, AssetClassDef] = {}
        for cname, cdef in (raw.get("asset_classes") or {}).items():
            points: dict[str, PointDef] = {}
            for pname, pdef in (cdef.get("points") or {}).items():
                points[pname] = PointDef(
                    name=pname,
                    unit=str(pdef["unit"]),
                    type=str(pdef["type"]),
                    direction=str(pdef["direction"]),
                    meta={k: v for k, v in pdef.items() if k not in _META_KEYS},
                )
            classes[cname] = AssetClassDef(
                name=cname,
                description=str(cdef.get("description", "")),
                flexibility=cdef.get("flexibility") or {"control": "none"},
                points=points,
                limits_schema=cdef.get("limits_schema") or {},
                nominal_schema=cdef.get("nominal_schema") or {},
            )
        model = cls(
            version=str(raw["data_model_version"]),
            asset_classes=classes,
            aggregates=raw.get("aggregates") or {},
        )
        errors = model._self_check()
        if errors:
            raise ValueError("data model self-check failed: " + "; ".join(errors))
        return model

    def _self_check(self) -> list[str]:
        errors: list[str] = []
        for cname, fields in self.aggregates.items():
            cls_ = self.asset_classes.get(cname)
            if cls_ is None:
                errors.append(f"aggregates: unknown asset class '{cname}'")
                continue
            for f in fields:
                if f not in cls_.points:
                    errors.append(f"aggregates.{cname}: unknown point '{f}'")
        for cname, cdef in self.asset_classes.items():
            for pname, p in cdef.points.items():
                if p.direction not in ("input", "output"):
                    errors.append(f"{cname}.{pname}: invalid direction '{p.direction}'")
        return errors

    def validate_fields(
        self,
        asset_class: str,
        fields: Iterable[str],
        direction: str | None = None,
    ) -> list[str]:
        """Return error strings for any field not defined in the model."""
        cls_ = self.asset_classes.get(asset_class)
        if cls_ is None:
            return [f"unknown asset class '{asset_class}'"]
        allowed = cls_.points if direction is None else cls_.points_by_direction(direction)
        kind = f"{direction} point" if direction else "point"
        return [f"{asset_class}: unknown {kind} '{f}'" for f in fields if f not in allowed]
