"""SIL scenario orchestration (plan task 26 / Gate G2).

Turns each scenario's `setup` hints into an ordered list of actions that put the
running stack into the condition the scenario tests, then drives them. Action
*planning* is pure (`build_actions`) so it is unit-tested without infrastructure;
*execution* is delegated to an Executor, with a live implementation (controller
HTTP API + `docker compose` for fault injection) and a fake for tests.

Action verbs:
    ("ems_droop", bool)     enable/disable droop via PUT /config/ems
    ("sim_profiles", dict)  recreate sim-* containers with scenario stimulus CSVs
    ("await_steady", secs)  settle after recreating sims (not part of the window)
    ("loop_start",)         POST /loop/start (idempotent: 409 tolerated)
    ("loop_stop",)          POST /loop/stop
    ("sleep", seconds)      let the loop run
    ("reload", path)        hot config reload via PUT /config/assets
    ("pause", service)      docker compose pause <service>  (fault injection)
    ("unpause", service)    docker compose unpause <service>
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from scenarios import Scenario

Action = tuple[Any, ...]

# A short settle before perturbing, so the controller is in steady RUN first.
_SETTLE_S = 5


def build_actions(scenario: Scenario) -> list[Action]:
    """Pure: scenario -> ordered actions. Mirrors scenarios.SCENARIOS setup keys."""
    s = scenario.setup
    dur = scenario.duration_s
    actions: list[Action] = [("ems_droop", bool(s.get("droop", False)))]

    # Per-scenario stimulus: recreate the named sim containers with their CSVs,
    # then settle (the settle is not counted in the measurement window).
    if "sim_profiles" in s:
        actions.append(("sim_profiles", s["sim_profiles"]))
        actions.append(("await_steady", _SETTLE_S))

    actions.append(("loop_start",))

    if s.get("fault") == "influx_stall":
        # Pause core so it stops writing aggregates -> controller sees stale data
        # and must walk RUN -> HOLD -> SAFE, then recover when core resumes.
        actions += [
            ("sleep", _SETTLE_S),
            ("pause", "core"),
            ("sleep", max(1, dur - _SETTLE_S)),
            ("unpause", "core"),
        ]
    elif "reload" in s:
        half = max(1, dur // 2)
        # Lead with a RUN settle so the fetched window (duration+10 s) clears any
        # non-RUN tail from the preceding scenario (e.g. stale_data's SAFE), which
        # would otherwise leak into this scenario's series. Not counted in window.
        actions += [
            ("await_steady", 3 * _SETTLE_S),
            ("sleep", half),
            ("reload", s["reload"]),
            ("sleep", dur - half),
        ]
    else:
        actions += [("sleep", dur)]

    return actions


class FakeExecutor:
    """Records actions instead of performing them (for unit tests)."""

    def __init__(self):
        self.log: list[Action] = []

    def run(self, actions: list[Action]) -> None:
        for a in actions:
            self.log.append(a)

    @property
    def verbs(self) -> list[str]:
        return [a[0] for a in self.log]


class LiveExecutor:  # pragma: no cover - drives the real stack
    """Executes actions against the running stack."""

    def __init__(
        self,
        controller_url: str,
        compose_file: str = "deploy/docker-compose.yml",
        sleep: Callable[[float], None] | None = None,
    ):
        import time

        self._url = controller_url.rstrip("/")
        self._compose = compose_file
        self._sleep = sleep or time.sleep

    def run(self, actions: list[Action]) -> None:
        for verb, *args in actions:
            getattr(self, f"_do_{verb}")(*args)

    def _do_ems_droop(self, enabled: bool) -> None:
        import requests

        cfg = requests.get(f"{self._url}/config/ems", timeout=10).json()
        cfg = copy.deepcopy(cfg)
        cfg["droop"]["enabled"] = enabled
        cfg["droop"]["p_f_droop"]["enabled"] = enabled
        r = requests.put(f"{self._url}/config/ems", json=cfg, timeout=10)
        r.raise_for_status()

    def _do_loop_start(self) -> None:
        import requests

        r = requests.post(f"{self._url}/loop/start", timeout=10)
        if r.status_code not in (200, 409):  # 409 = already running
            r.raise_for_status()

    def _do_loop_stop(self) -> None:
        import requests

        requests.post(f"{self._url}/loop/stop", timeout=10)

    def _do_sleep(self, seconds: float) -> None:
        self._sleep(seconds)

    # sim service -> env var consumed by its compose `command` profile path
    _SIM_PROFILE_ENV = {
        "sim-grid": "SIM_GRID_PROFILE",
        "sim-bess": "SIM_BESS_PROFILE",
        "sim-pv": "SIM_PV_PROFILE",
    }

    def _do_sim_profiles(self, mapping: dict[str, str]) -> None:
        import os

        services = []
        for svc, profile in mapping.items():
            os.environ[self._SIM_PROFILE_ENV[svc]] = profile
            services.append(svc)
        # Recreate only the named sims; they re-read the SIM_*_PROFILE env on
        # create. --no-deps leaves influx/mosquitto/core/controller untouched.
        self._compose_cmd("up", "-d", "--no-deps", "--force-recreate", *services)

    def _do_await_steady(self, seconds: float) -> None:
        # Let core reconnect to the recreated sims and refresh the aggregates
        # (a brief HOLD during recreate clears well within hold_max_s).
        self._sleep(seconds)

    def _do_reload(self, path: str) -> None:
        import requests
        import yaml

        with open(path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        r = requests.put(f"{self._url}/config/assets", json=cfg, timeout=10)
        r.raise_for_status()

    def _do_pause(self, service: str) -> None:
        self._compose_cmd("pause", service)

    def _do_unpause(self, service: str) -> None:
        self._compose_cmd("unpause", service)

    def _compose_cmd(self, *args: str) -> None:
        import subprocess

        subprocess.run(["docker", "compose", "-f", self._compose, *args], check=True)


def run_scenario(scenario: Scenario, executor) -> None:
    """Plan and execute one scenario's setup on the stack."""
    executor.run(build_actions(scenario))
