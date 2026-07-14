"""Map-driven Modbus TCP servers for the HIL plant -- parity by construction.

Each server's datastore layout comes from the *same* register-map file core.py
reads, via the *same* codec the pymodbus simulator uses. We do not hand-place a
single register: addresses, types, word widths and scale factors are whatever
`common.register_map` + `common.modbus_codec` say, so the HIL servers, the
pymodbus simulator and the real ModbusTcpAdapter agree by construction.

A server is bound to a SiteModel and an asset class:
  * input points (rw == 'r')  are *written by the plant* each tick
    (plant -> registers): SoC, P, Q, headroom, PCC V/Hz/A...
  * setpoint points (rw in {'rw','w'}) are *read by the plant* each tick and
    applied to the converters (registers -> plant): battery P/Q setpoints,
    pv/load derate factors.

The Typhoon schematic attaches one of these servers per asset block; the
software plant-in-the-loop (hil.chil_runner) attaches the very same class to a
SiteModel. Either way the controller (core.py) connects unchanged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import yaml
from common.config_models import AssetConfigFile, validate_asset_config
from common.data_model import DataModel
from common.register_map import RegisterDef, RegisterMap, load_register_map
from simulator.device import SimulatedDevice

from hil.plant.models import SiteModel

# Neutral initial values for writable setpoints, so a controller read before the
# first write returns something sane (1.0 = no derate; 0 kW = idle battery).
_NEUTRAL_SETPOINT = {
    "active_power_setpoint_kw": 0.0,
    "reactive_power_setpoint_kvar": 0.0,
    "derate_factor_setpoint": 1.0,
}


def _is_input(reg: RegisterDef) -> bool:
    return reg.rw == "r" and reg.role is None  # canonical measurements only


def _is_setpoint(reg: RegisterDef) -> bool:
    return reg.rw in ("rw", "w") and reg.role is None  # canonical setpoints only


class HilModbusServer:
    """A Modbus TCP server for one asset, bound to a SiteModel.

    Wraps the simulator's SimulatedDevice (so the datastore is byte-for-byte the
    SIL one) and adds the plant binding: push measurements out, pull setpoints in.
    """

    def __init__(
        self,
        rmap: RegisterMap,
        site: SiteModel,
        *,
        host: str = "0.0.0.0",
        port: int = 502,
        unit_id: int = 1,
    ):
        self.rmap = rmap
        self.asset_class = rmap.asset_class
        self.site = site
        self.host = host
        self.port = port
        self.unit_id = unit_id
        initial = {
            name: _NEUTRAL_SETPOINT.get(name, 0.0)
            for name, reg in rmap.points.items()
            if _is_setpoint(reg)
        }
        self._device = SimulatedDevice(rmap, initial=initial)

    @property
    def context(self):  # pymodbus ServerContext, for embedding/testing
        return self._device.context

    @property
    def input_points(self) -> list[str]:
        return [n for n, r in self.rmap.points.items() if _is_input(r)]

    @property
    def setpoint_points(self) -> list[str]:
        return [n for n, r in self.rmap.points.items() if _is_setpoint(r)]

    # -- plant <-> registers -------------------------------------------------

    def push_inputs(self) -> None:
        """Plant measurements -> input registers (run after SiteModel.step)."""
        values = self.site.points(self.asset_class)
        for name in self.input_points:
            if name in values:
                self._device.set_point(name, values[name])

    def pull_setpoints(self) -> None:
        """Setpoint registers (written by the controller) -> plant converters."""
        for name in self.setpoint_points:
            value = self._device.get_point(name)
            self.site.apply_setpoint(self.asset_class, name, value)

    # -- read helpers (tests / parity) --------------------------------------

    def get_point(self, name: str) -> float:
        return self._device.get_point(name)

    def set_point(self, name: str, value: float) -> None:
        self._device.set_point(name, value)

    async def serve(self) -> None:
        await self._device.serve(self.host, self.port)


@dataclass(frozen=True)
class ServerSpec:
    """One server's identity, taken straight from asset_config comm blocks."""

    asset_id: str
    asset_class: str
    map_path: Path
    host: str
    port: int
    unit_id: int


def server_specs_from_config(
    asset_config: AssetConfigFile, repo_root: Path
) -> list[ServerSpec]:
    """Pull (host, port, unit_id, map) for every asset that has a comm block, so
    the HIL servers bind exactly where core.py expects them."""
    specs: list[ServerSpec] = []
    for a in asset_config.assets:
        if a.state != "active" or a.comm is None:
            continue
        specs.append(
            ServerSpec(
                asset_id=a.id,
                asset_class=a.asset_class,
                map_path=repo_root / a.comm.register_map,
                host=a.comm.host,
                port=a.comm.port,
                unit_id=a.comm.unit_id,
            )
        )
    return specs


def build_servers_for_site(
    asset_config_path: str | Path,
    data_model_path: str | Path,
    site: SiteModel,
    *,
    repo_root: Path | None = None,
    host_override: str | None = None,
    port_override: int | None = None,
) -> list[HilModbusServer]:
    """Instantiate one HilModbusServer per active asset with a comm block.

    `host_override`/`port_override` let a single test host run all servers on
    localhost with distinct ports (the real rig uses asset_config's hosts).
    """
    asset_config_path = Path(asset_config_path)
    rr = repo_root or asset_config_path.resolve().parents[1]
    dm = DataModel.load(data_model_path)
    raw = yaml.safe_load(asset_config_path.read_text(encoding="utf-8"))
    cfg = validate_asset_config(raw, dm)
    specs = server_specs_from_config(cfg, rr)

    servers: list[HilModbusServer] = []
    for i, spec in enumerate(specs):
        rmap = load_register_map(spec.map_path, dm)
        servers.append(
            HilModbusServer(
                rmap,
                site,
                host=host_override or spec.host,
                port=(port_override + i) if port_override is not None else spec.port,
                unit_id=spec.unit_id,
            )
        )
    return servers


def replicate_for_scale(
    template_map_path: str | Path,
    data_model_path: str | Path,
    sites: list[SiteModel],
    *,
    host: str = "0.0.0.0",
    base_port: int = 16000,
) -> list[HilModbusServer]:
    """Stand up N servers from one map for the 50-asset scale test (each on its
    own port). Each gets its own SiteModel so their states are independent."""
    dm = DataModel.load(data_model_path)
    rmap = load_register_map(template_map_path, dm)
    return [
        HilModbusServer(rmap, site, host=host, port=base_port + i, unit_id=1)
        for i, site in enumerate(sites)
    ]


async def serve_all(servers: list[HilModbusServer]) -> None:
    await asyncio.gather(*(s.serve() for s in servers))
