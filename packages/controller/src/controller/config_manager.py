"""Backward-compatible re-export.

The config lifecycle moved to ``common.config_manager`` under ADR-0001 so Core
(the configuration authority) and the Controller share one implementation.
Existing imports of ``controller.config_manager`` keep working unchanged.
"""

from __future__ import annotations

from common.config_manager import ASSETS, EMS, ConfigManager

__all__ = ["ASSETS", "EMS", "ConfigManager"]
