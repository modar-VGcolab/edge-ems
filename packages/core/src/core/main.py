"""Core service entry (Phase 2/3): build adapters from config, then run the
orchestration cycle at the configured period.

Everything in this module is live-wiring (Modbus adapters, InfluxDB, MQTT
clients) and is exercised in SIL, not unit tests; the testable cycle logic is in
core.orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
from pathlib import Path

import yaml
from common.config_manager import ConfigManager
from common.config_models import validate_asset_config, validate_edge_ems_config
from common.data_model import DataModel
from common.logging_setup import configure_logging
from common.register_map import load_register_map

from core.adapters.modbus_tcp import ModbusTcpAdapter
from core.config_api import create_config_app, http_loop_state_provider
from core.dispatcher import SetpointDispatcher
from core.influx_writer import InfluxWriter
from core.orchestrator import Orchestrator
from core.watchdog import SetpointWatchdog

log = logging.getLogger(__name__)


def _load(path):
    # Expand ${VAR} from the environment (e.g. influxdb.token: "${INFLUX_TOKEN}")
    # before parsing, so secrets live in the environment, not the YAML file.
    raw = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(os.path.expandvars(raw))


def build_adapters(ac, dm):  # pragma: no cover - needs hardware/sim
    adapters, classes, input_points, weights = {}, {}, {}, {}
    for a in ac.assets:
        if a.state != "active" or a.comm is None:
            continue
        rmap = load_register_map(Path(a.comm.register_map), dm)
        adapters[a.id] = ModbusTcpAdapter(
            a.comm.host, rmap, port=a.comm.port, unit_id=a.comm.unit_id
        )
        classes[a.id] = a.asset_class
        # read only the input points this device actually exposes (e.g. the PCC
        # active_power_setpoint_kw is HTTP-set, not on the meter)
        in_pts = dm.asset_classes[a.asset_class].points_by_direction("input")
        input_points[a.id] = [pt for pt in in_pts if pt in rmap.points]
        if "capacity_kwh" in a.nominal:
            weights[a.id] = a.nominal["capacity_kwh"]
    return adapters, classes, input_points, weights


async def run() -> None:  # pragma: no cover - service entry
    from paho.mqtt.client import Client as MqttClient

    dm_path = os.environ.get("DATA_MODEL_PATH", "data_model.yaml")
    asset_path = os.environ.get("ASSET_CONFIG_PATH", "configs/asset_config.yaml")
    ems_path = os.environ.get("EMS_CONFIG_PATH", "configs/edge_ems_config.yaml")
    dm = DataModel.load(Path(dm_path))
    ac = validate_asset_config(_load(asset_path), dm)
    ec = validate_edge_ems_config(_load(ems_path), dm)
    configure_logging(ec.logging)
    site_id = ac.site.id

    # ADR-0001: Core is the configuration authority. Serve the config API on an
    # internal-only port (:5100, not published to the host in compose), reusing
    # the shared ConfigManager. Runs alongside the orchestration loop.
    import uvicorn

    cm = ConfigManager(dm, asset_path, ems_path)
    controller_url = os.environ.get("CONTROLLER_URL", "http://controller:5000")
    config_port = int(os.environ.get("CORE_CONFIG_PORT", "5100"))

    # Phase 5: publish a retained MQTT signal whenever config is committed, so the
    # Controller re-pulls. Best-effort — a notify failure never blocks a write.
    import json as _json

    from paho.mqtt.client import Client as _CfgMqtt

    _notify = _CfgMqtt(client_id=f"core-cfg-{site_id}")
    if ec.mqtt.username:
        _notify.username_pw_set(ec.mqtt.username, ec.mqtt.password)
    try:
        _notify.connect(ec.mqtt.broker_address, ec.mqtt.broker_port)
        _notify.loop_start()
    except Exception:  # noqa: BLE001 - notifications are best-effort
        log.warning("config-change MQTT notifier failed to connect", exc_info=True)
    _cfg_topic = f"site/{site_id}/config/changed"

    def _on_change(kind, info):  # noqa: ANN001
        _notify.publish(_cfg_topic, _json.dumps({"kind": kind, **info}), qos=1, retain=True)

    config_server = uvicorn.Server(
        uvicorn.Config(
            create_config_app(
                cm, dm, dm_path,
                loop_state=http_loop_state_provider(controller_url),
                on_change=_on_change,
            ),
            host="0.0.0.0",  # noqa: S104 - container-internal; not published in compose
            port=config_port,
            log_level="warning",
        )
    )
    asyncio.create_task(config_server.serve())

    adapters, classes, input_points, weights = build_adapters(ac, dm)
    for ad in adapters.values():
        try:
            await ad.connect()
        except Exception:  # noqa: BLE001 - reconnect handled in the read path
            log.warning("initial connect failed for %r (will retry on read)", ad, exc_info=True)

    writer = InfluxWriter(
        dm,
        site_id,
        bucket=ec.influxdb.bucket,
        org=ec.influxdb.org,
        url=ec.influxdb.url,
        token=ec.influxdb.token,
    )
    dispatcher = SetpointDispatcher(
        dm, site_id, adapters, classes, max_age_s=ec.controller.timeout_period
    )
    watchdog = SetpointWatchdog(adapters, classes, period_s=ec.controller.update_period)
    orch = Orchestrator(
        dm, site_id, adapters, classes, input_points, writer, dispatcher, watchdog, weights
    )

    inbound: queue.Queue = queue.Queue()
    mqtt = MqttClient(client_id=f"core-{site_id}")
    if ec.mqtt.username:
        mqtt.username_pw_set(ec.mqtt.username, ec.mqtt.password)

    import json

    def on_message(_c, _u, msg):
        try:
            inbound.put_nowait(json.loads(msg.payload))
        except Exception:  # noqa: BLE001
            log.warning("dropped malformed setpoint message on %r", msg.topic, exc_info=True)

    mqtt.on_message = on_message
    mqtt.connect(ec.mqtt.broker_address, ec.mqtt.broker_port)
    mqtt.subscribe(f"site/{site_id}/setpoints/+", qos=1)
    mqtt.loop_start()
    watchdog.start(asyncio.get_event_loop().time())

    period = ec.controller.update_period
    log.info("core: polling %d devices at %ss for site %s", len(adapters), period, site_id)
    while True:
        import time as _t

        now = _t.time()
        await orch.poll_cycle(now)
        while not inbound.empty():
            await orch.handle_setpoint(inbound.get_nowait(), now)
        await orch.safety_cycle(now)
        await asyncio.sleep(period)


def main() -> int:  # pragma: no cover - service entry
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
