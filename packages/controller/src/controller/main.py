"""Controller service entry (Phase 3): wires ConfigManager + live ControlLoop +
HTTP API. /loop/start and /loop/stop drive the background LoopRunner.

The InfluxDB/MQTT clients are created in build_runner so the API (config,
validate, health) still imports without the buses; the loop reports
staleness/SAFE through its own mode ladder once running.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from common.data_model import DataModel

from controller.config_manager import ConfigManager
from controller.data_connector import InfluxAggregateReader, MqttSetpointPublisher
from controller.http_api import create_app
from controller.runtime import LiveSnapshotReader, LoopRunner, build_control_loop

_PCC_FLUX = """
from(bucket: "{bucket}")
  |> range(start: -{window}s)
  |> filter(fn: (r) => r._measurement == "pcc" and r.site_id == "{site_id}")
  |> last()
"""


def _pcc_query_factory(client, bucket, site_id, window_s=60):
    def query():  # pragma: no cover - live path exercised in SIL
        flux = _PCC_FLUX.format(bucket=bucket, window=window_s, site_id=site_id)
        for table in client.query_api().query(flux):
            for rec in table.records:
                yield (rec.get_field(), float(rec.get_value()), rec.get_time().timestamp())

    return query


def build_runner(cm: ConfigManager, dm: DataModel) -> LoopRunner:  # pragma: no cover - SIL
    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import SYNCHRONOUS
    from paho.mqtt.client import Client as MqttClient

    ec = cm.ems_config
    site_id = cm.asset_config.site.id
    influx = InfluxDBClient(url=ec.influxdb.url, token=ec.influxdb.token, org=ec.influxdb.org)
    write_api = influx.write_api(write_options=SYNCHRONOUS)

    agg_reader = InfluxAggregateReader(
        dm,
        site_id,
        bucket=ec.influxdb.bucket,
        timeout_s=ec.controller.timeout_period,
        url=ec.influxdb.url,
        token=ec.influxdb.token,
        org=ec.influxdb.org,
    )
    reader = LiveSnapshotReader(
        dm,
        site_id,
        agg_reader,
        _pcc_query_factory(influx, ec.influxdb.bucket, site_id),
        timeout_s=ec.controller.timeout_period,
    )

    mqtt = MqttClient(client_id=ec.mqtt.client_id)
    if ec.mqtt.username:
        mqtt.username_pw_set(ec.mqtt.username, ec.mqtt.password)
    mqtt.connect(ec.mqtt.broker_address, ec.mqtt.broker_port)
    mqtt.loop_start()
    publisher = MqttSetpointPublisher(dm, site_id, mqtt)

    def publish(asset_class, setpoints, now):
        return publisher.publish_setpoints(asset_class, setpoints, now=now)

    def write_control(fields, now):
        p = Point("control").tag("site_id", site_id)
        for k, v in fields.items():
            p = p.field(k, v)
        write_api.write(bucket=ec.influxdb.bucket, org=ec.influxdb.org, record=p)

    loop = build_control_loop(
        cm, dm, read_snapshot=reader, publish=publish, write_control=write_control
    )
    return LoopRunner(loop, ec.controller.update_period)


def _resolve_config():  # pragma: no cover - service entry
    """ADR-0001 Phase 3: pick the config source.

    CONFIG_SOURCE=core  -> sync from Core into a last-known-good cache, then read
                           those cache files with the same ConfigManager.
    CONFIG_SOURCE=file  -> (default) read local files exactly as before.
    """
    source = os.environ.get("CONFIG_SOURCE", "file").lower()
    if source == "core":
        from controller.config_client import CoreConfigClient

        base = os.environ.get("CORE_CONFIG_URL", "http://core:5100")
        cache = os.environ.get("CONFIG_CACHE_DIR", "configs/.cache")
        paths = CoreConfigClient(base, cache).sync()
        dm = DataModel.load(paths.data_model)
        return dm, ConfigManager(dm, paths.assets, paths.ems)

    dm = DataModel.load(Path(os.environ.get("DATA_MODEL_PATH", "data_model.yaml")))
    cm = ConfigManager(
        dm,
        asset_path=os.environ.get("ASSET_CONFIG_PATH", "configs/asset_config.yaml"),
        ems_path=os.environ.get("EMS_CONFIG_PATH", "configs/edge_ems_config.yaml"),
    )
    return dm, cm


def _maybe_start_config_watcher(cm):  # pragma: no cover - live wiring
    """Phase 5: when consuming from Core, re-pull on the MQTT change signal and on
    a slow backstop, then reload the live config from the refreshed cache."""
    if os.environ.get("CONFIG_SOURCE", "file").lower() != "core":
        return
    import threading
    import time

    from controller.config_client import ConfigWatcher, CoreConfigClient

    base = os.environ.get("CORE_CONFIG_URL", "http://core:5100")
    cache = os.environ.get("CONFIG_CACHE_DIR", "configs/.cache")
    backstop = float(os.environ.get("CONFIG_BACKSTOP_S", "300"))
    site_id = cm.asset_config.site.id
    watcher = ConfigWatcher(
        CoreConfigClient(base, cache), cm.reload, site_id=site_id, backstop_s=backstop
    )

    from paho.mqtt.client import Client as MqttClient

    ec = cm.ems_config
    cli = MqttClient(client_id=f"controller-cfg-{site_id}")
    if ec.mqtt.username:
        cli.username_pw_set(ec.mqtt.username, ec.mqtt.password)
    cli.on_message = lambda _c, _u, msg: watcher.handle_message(msg.payload)
    try:
        cli.connect(ec.mqtt.broker_address, ec.mqtt.broker_port)
        cli.subscribe(watcher.topic, qos=1)
        cli.loop_start()
    except Exception:  # noqa: BLE001 - signal is best-effort; backstop still runs
        pass

    def _backstop_loop():
        while True:
            time.sleep(backstop)
            watcher.backstop_tick()

    threading.Thread(target=_backstop_loop, daemon=True).start()


def build_app():  # pragma: no cover - service entry
    dm, cm = _resolve_config()
    runner = build_runner(cm, dm)
    app = create_app(cm, dm, loop=runner)
    _maybe_start_config_watcher(cm)
    return app


def main() -> None:  # pragma: no cover - service entry
    app = build_app()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("HTTP_PORT", "5000")))


if __name__ == "__main__":
    main()
