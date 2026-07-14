"""Controller config client (ADR-0001 Phase 3) — consume config from Core.

Core is the configuration authority. When ``CONFIG_SOURCE=core`` the Controller
fetches the data model and per-site config from Core's API and writes them to a
local **last-known-good** cache. The existing ``ConfigManager`` then reads those
cache files exactly as it reads hand-placed files today, so nothing downstream
changes.

Fail-operational (ADR-0001): if Core is unreachable but a cache exists, the
Controller runs on the cache and logs a warning. Only a cold start with neither
Core nor a cache is fatal — and then with an actionable message.

Secrets never travel resolved: Core serves ``${VAR}`` placeholders verbatim, the
cache keeps the placeholder, and ``ConfigManager`` expands it from the
Controller's own environment.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# cache filename -> Core endpoint
_TARGETS = {
    "data_model.yaml": "/config/data_model",
    "asset_config.yaml": "/config/assets",
    "edge_ems_config.yaml": "/config/ems",
}


@dataclass(frozen=True)
class ConfigPaths:
    data_model: Path
    assets: Path
    ems: Path


def _urllib_get_json(url: str, timeout_s: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 - internal URL
        return json.loads(resp.read().decode("utf-8"))


class CoreConfigClient:
    def __init__(
        self,
        base_url: str,
        cache_dir: str | Path,
        *,
        timeout_s: float = 5.0,
        get_json=_urllib_get_json,
    ):
        self._base = base_url.rstrip("/")
        self._cache = Path(cache_dir)
        self._timeout = timeout_s
        self._get = get_json

    def _paths(self) -> ConfigPaths:
        return ConfigPaths(
            data_model=self._cache / "data_model.yaml",
            assets=self._cache / "asset_config.yaml",
            ems=self._cache / "edge_ems_config.yaml",
        )

    def _all_cached(self) -> bool:
        return all(p.exists() for p in (self._paths().data_model,
                                        self._paths().assets, self._paths().ems))

    @staticmethod
    def _atomic_write_yaml(path: Path, data: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        tmp.replace(path)

    def sync(self) -> ConfigPaths:
        """Fetch from Core and refresh the cache; on failure fall back to the
        last-known-good cache. Raise only if neither source is available."""
        self._cache.mkdir(parents=True, exist_ok=True)
        try:
            fetched = {fn: self._get(self._base + ep, self._timeout)
                       for fn, ep in _TARGETS.items()}
        except Exception as exc:  # noqa: BLE001 - any transport/parse error -> fallback
            if self._all_cached():
                log.warning("Core config unreachable (%s); using last-known-good cache", exc)
                return self._paths()
            raise RuntimeError(
                f"Core config unreachable and no local cache at {self._cache}: {exc}. "
                "Start Core first, or run with CONFIG_SOURCE=file."
            ) from exc
        for fn, data in fetched.items():
            self._atomic_write_yaml(self._cache / fn, data)
        log.info("config synced from Core at %s -> %s", self._base, self._cache)
        return self._paths()


class ConfigWatcher:
    """Keep the Controller's config fresh (ADR-0001 Phase 5).

    Re-pulls from Core when a ``config-changed`` signal arrives on MQTT, and on a
    slow backstop interval in case a signal is missed. After each successful
    re-pull it calls ``on_synced`` (typically ``ConfigManager.reload``) so the
    live config reflects the refreshed cache.

    Transport is injected: ``handle_message`` is wired to the MQTT on_message and
    ``backstop_tick`` to a timer, so the policy here is unit-testable without a
    broker.
    """

    def __init__(self, client: "CoreConfigClient", on_synced, *,
                 site_id: str, backstop_s: float = 300.0):
        self._client = client
        self._on_synced = on_synced
        self.topic = f"site/{site_id}/config/changed"
        self.backstop_s = backstop_s

    def _resync(self) -> None:
        self._client.sync()
        self._on_synced()

    def handle_message(self, payload: bytes | str | None = None) -> None:
        """Called for a message on ``self.topic`` — re-pull regardless of payload
        (the signal only says 'something changed'; the truth is fetched)."""
        try:
            self._resync()
        except Exception:  # noqa: BLE001 - keep last-known-good; backstop retries
            log.warning("config re-pull after change signal failed; keeping current config")

    def backstop_tick(self) -> None:
        """Periodic safety re-pull in case a change signal was missed."""
        try:
            self._resync()
        except Exception:  # noqa: BLE001
            log.warning("backstop config re-pull failed; keeping current config")
