"""Asyncio wiring of the external-EMS container.

Runs three cooperating pieces against one MQTT connection and a single monotonic
clock:
  * emulator   — publishes a random external P* each publish-interval, silent
                 during the outage window (so the watchdog can take over).
  * gateway    — subscribes to the external topic, re-evaluates freshness each
                 tick, forwards the effective PCC setpoint to the controller, and
                 writes takeover state to InfluxDB.
  * status API — FastAPI GET /status on STATUS_PORT (served on a daemon thread).

A real external EMS can replace the emulator by publishing to the same topic;
set EXT_OUTAGE_AT_S beyond the run length to disable the simulated outage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import uvicorn
from paho.mqtt.client import Client as MqttClient

from ext_ems.config import load_settings
from ext_ems.emulator import Emulator
from ext_ems.forwarder import ControllerForwarder
from ext_ems.gateway import Gateway, Watchdog
from ext_ems.influx import InfluxStateWriter
from ext_ems.status_api import create_status_app

log = logging.getLogger("ext_ems.main")


def _build_mqtt(settings) -> MqttClient:
    client = MqttClient(client_id=f"ext-ems-{settings.site_id}")
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
    return client


async def run() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info(
        "ext-ems starting: site=%s topic=%s time_scale=%g "
        "(publish=%.1fs watchdog=%.1fs outage=%.1fs reconnect=%.1fs)",
        settings.site_id,
        settings.external_topic,
        settings.time_scale,
        settings.publish_interval,
        settings.watchdog_timeout,
        settings.outage_at,
        settings.reconnect_at,
    )

    forwarder = ControllerForwarder(settings.controller_url)
    influx = InfluxStateWriter(
        settings.influx_url,
        settings.influx_org,
        settings.influx_bucket,
        settings.influx_token,
        settings.site_id,
    )
    watchdog = Watchdog(settings.watchdog_timeout, settings.self_consumption_kw)
    gateway = Gateway(watchdog, forwarder.forward, influx.write)
    lock = threading.Lock()

    # --- MQTT: receive external setpoints --------------------------------------
    mqtt = _build_mqtt(settings)

    def on_connect(client, _u, _f, rc):
        log.info("mqtt connected rc=%s; subscribing %s", rc, settings.external_topic)
        client.subscribe(settings.external_topic, qos=1)

    def on_message(_client, _u, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            sp = float(payload["pcc_setpoint_kw"])
        except Exception as exc:  # noqa: BLE001 - ignore malformed external messages
            log.warning("dropping malformed external message: %s", exc)
            return
        with lock:
            gateway.handle_external(sp, time.monotonic())
        log.info("received external P*=%.1f kW", sp)

    mqtt.on_connect = on_connect
    mqtt.on_message = on_message
    mqtt.connect(settings.mqtt_broker, settings.mqtt_port)
    mqtt.loop_start()

    # --- status API on a daemon thread -----------------------------------------
    def status_provider(now: float) -> dict:
        with lock:
            return gateway.status(now)

    status_app = create_status_app(status_provider)
    config = uvicorn.Config(
        status_app, host="0.0.0.0", port=settings.status_port, log_level="warning"
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, name="status-api", daemon=True).start()

    # --- emulator: publish external setpoints ----------------------------------
    emulator = Emulator(
        publish=lambda topic, body: mqtt.publish(topic, body, qos=1),
        topic=settings.external_topic,
        p_min_kw=settings.p_min_kw,
        p_max_kw=settings.p_max_kw,
        publish_interval_s=settings.publish_interval,
        outage_at_s=settings.outage_at,
        reconnect_at_s=settings.reconnect_at,
    )

    start = time.monotonic()

    async def emulator_loop():
        # First setpoint immediately so following starts without waiting a full
        # interval; then on the publish cadence.
        while True:
            emulator.publish_tick(time.monotonic() - start)
            await asyncio.sleep(settings.publish_interval)

    async def gateway_loop():
        while True:
            with lock:
                gateway.tick(time.monotonic())
            await asyncio.sleep(settings.tick_interval_s)

    try:
        await asyncio.gather(emulator_loop(), gateway_loop())
    finally:
        mqtt.loop_stop()
        forwarder.close()
        influx.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
