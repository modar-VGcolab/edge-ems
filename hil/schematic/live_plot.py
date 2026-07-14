"""Rolling live plot of the edge-EMS closed loop, straight from InfluxDB.

Runs as a SEPARATE host process (no Docker, no controller coupling) — a crash
here never affects the loop. Queries the InfluxDB that the stack publishes on
localhost:8086 and shows a rolling dual-axis plot: power on top (PCC active
power, battery power, and the controller's battery command), SOC on the bottom.

Reads the same series core/controller write:
  * measurement "pcc"          field active_power_kw   -> PCC power (regulated var)
  * measurement "battery"      field active_power_kw   -> battery power (response)
  * measurement "battery"      field soc_pct           -> battery SOC
  * measurement "control"      field pi_output_kw      -> battery setpoint (command)
  * measurement "external_ems" field active_source     -> S1 takeover/release
        (1 = following external EMS, 0 = self-consumption after a watchdog
        takeover; only present when the ext-ems profile is running)

Usage (in .venv-rig, with the stack up):
    pip install matplotlib influxdb-client        # one-time
    python -m hil.schematic.live_plot
    python hil/schematic/live_plot.py --window 120 --bucket edge_ems

Token resolution: --token, else $INFLUX_TOKEN, else INFLUX_TOKEN from configs/.env.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from influxdb_client import InfluxDBClient

_REPO = Path(__file__).resolve().parents[2]
REFRESH_MS = 1000


def _token_from_env_file() -> str | None:
    env = _REPO / "configs" / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("INFLUX_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def _series(query_api, bucket: str, window_s: float, measurement: str,
            field: str) -> tuple[list[float], list[float]]:
    """Return (epoch_seconds[], value[]) for one measurement/field over the window."""
    flux = (
        f'from(bucket: "{bucket}") '
        f'|> range(start: -{int(window_s)}s) '
        f'|> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}") '
        f'|> keep(columns: ["_time", "_value"]) '
        f'|> sort(columns: ["_time"])'
    )
    ts: list[float] = []
    vs: list[float] = []
    try:
        for table in query_api.query(flux):
            for rec in table.records:
                ts.append(rec.get_time().timestamp())
                vs.append(float(rec.get_value()))
    except Exception as e:  # noqa: BLE001 - transient query errors shouldn't kill the plot
        print(f"query warning ({measurement}.{field}): {e}", file=sys.stderr)
    return ts, vs


def main() -> None:
    ap = argparse.ArgumentParser(description="Live InfluxDB plot of the edge-EMS loop")
    ap.add_argument("--url", default=os.environ.get("INFLUX_URL", "http://localhost:8086"))
    ap.add_argument("--org", default=os.environ.get("INFLUX_ORG", "edge"))
    ap.add_argument("--bucket", default=os.environ.get("INFLUX_BUCKET", "edge_ems"))
    ap.add_argument("--token", default=None)
    ap.add_argument("--window", type=float, default=60.0, help="rolling window [s]")
    args = ap.parse_args()

    token = args.token or os.environ.get("INFLUX_TOKEN") or _token_from_env_file()
    if not token:
        sys.exit("No InfluxDB token: pass --token, set $INFLUX_TOKEN, or add it to configs/.env")

    client = InfluxDBClient(url=args.url, token=token, org=args.org)
    qa = client.query_api()
    print(f"Plotting {args.url} bucket={args.bucket} (rolling {args.window:.0f} s; "
          "close window or Ctrl-C to quit)")

    fig, (ax_p, ax_soc) = plt.subplots(2, 1, sharex=True, figsize=(10, 7))
    fig.canvas.manager.set_window_title("edge-EMS closed loop — live")

    (ln_pcc,) = ax_p.plot([], [], label="PCC active_power_kw")
    (ln_bess,) = ax_p.plot([], [], label="battery active_power_kw")
    (ln_cmd,) = ax_p.plot([], [], label="battery command (pi_output_kw)",
                          linestyle="--", linewidth=0.9)
    ax_p.set_ylabel("Power [kW]")
    ax_p.axhline(0.0, linewidth=0.8, color="grey")  # PCC self-consumption target
    ax_p.legend(loc="upper left")
    ax_p.grid(True, alpha=0.3)

    (ln_soc,) = ax_soc.plot([], [], label="battery soc_pct", color="tab:green")
    ax_soc.set_ylabel("SOC [%]")
    ax_soc.set_xlabel("time [s]")
    ax_soc.set_ylim(0, 100)
    ax_soc.legend(loc="upper left")
    ax_soc.grid(True, alpha=0.3)

    # S1 external-EMS source on a twin axis of the SOC plot: 1 = following the
    # external EMS, 0 = self-consumption (watchdog takeover). Stepped so the
    # takeover/release edges are crisp against PCC power and SOC.
    ax_src = ax_soc.twinx()
    (ln_src,) = ax_src.plot([], [], label="external active_source", color="tab:red",
                            drawstyle="steps-post", linewidth=1.2)
    ax_src.set_ylabel("active source")
    ax_src.set_ylim(-0.1, 1.1)
    ax_src.set_yticks([0, 1])
    ax_src.set_yticklabels(["self", "ext"])
    ax_src.legend(loc="upper right")

    def update(_frame):
        series = {
            "pcc": _series(qa, args.bucket, args.window, "pcc", "active_power_kw"),
            "bess": _series(qa, args.bucket, args.window, "battery", "active_power_kw"),
            "cmd": _series(qa, args.bucket, args.window, "control", "pi_output_kw"),
            "soc": _series(qa, args.bucket, args.window, "battery", "soc_pct"),
            "src": _series(qa, args.bucket, args.window, "external_ems", "active_source"),
        }
        # common t0 across whatever series have data, so x-axis is shared seconds
        all_t = [t for ts, _ in series.values() for t in ts]
        if not all_t:
            return ln_pcc, ln_bess, ln_cmd, ln_soc, ln_src
        t0 = min(all_t)
        rel = lambda ts: [t - t0 for t in ts]
        ln_pcc.set_data(rel(series["pcc"][0]), series["pcc"][1])
        ln_bess.set_data(rel(series["bess"][0]), series["bess"][1])
        ln_cmd.set_data(rel(series["cmd"][0]), series["cmd"][1])
        ln_soc.set_data(rel(series["soc"][0]), series["soc"][1])
        ln_src.set_data(rel(series["src"][0]), series["src"][1])
        t_max = max(all_t) - t0
        ax_p.set_xlim(max(0.0, t_max - args.window), max(t_max, 1.0))
        ax_p.relim()
        ax_p.autoscale_view(scalex=False)
        return ln_pcc, ln_bess, ln_cmd, ln_soc, ln_src

    _anim = FuncAnimation(fig, update, interval=REFRESH_MS, cache_frame_data=False)
    plt.tight_layout()
    try:
        plt.show()
    finally:
        client.close()


if __name__ == "__main__":
    main()
