"""50-asset loop-budget benchmark over real Modbus servers (prompt sections 6/8).

Stands up N map-generated HIL Modbus servers on localhost, connects the *real*
core ModbusTcpAdapter to each, and times a full read cycle (all N assets polled,
read-grouped exactly as core does) over many iterations. This is the loop-budget
measurement at scale: it records the per-cycle latency histogram and asserts the
worst case and p95 stay under the 250 ms budget of the 1 s control period.

It doubles as the end-to-end proof that the server generator is wire-compatible
with the controller's adapter: if the adapter can connect, read-group and decode
every point from a generated server, parity holds on the wire and not just in
the datastore.

    python -m hil.scale_bench --count 50 --cycles 200
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from common.data_model import DataModel
from common.modbus_codec import NUMERIC_TYPES
from common.register_map import load_register_map
from core.adapters.modbus_tcp import ModbusTcpAdapter

from hil.plant.models import SiteModel
from hil.servers import HilModbusServer

_REPO = Path(__file__).resolve().parents[1]
BUDGET_MS = 250.0


def _histogram(samples_ms: list[float], bins=(1, 2, 5, 10, 25, 50, 100, 250, 1e9)) -> str:
    lines = []
    lo = 0.0
    n = len(samples_ms)
    for hi in bins:
        c = sum(1 for s in samples_ms if lo <= s < hi)
        bar = "#" * int(40 * c / n) if n else ""
        label = f"<{hi:g}ms" if hi < 1e9 else ">=250ms"
        lines.append(f"  {label:>8} | {bar:<40} {c}")
        lo = hi
    return "\n".join(lines)


async def _bench(count: int, cycles: int, base_port: int) -> dict:
    dm = DataModel.load(_REPO / "data_model.yaml")
    rmap = load_register_map(_REPO / "maps" / "custom_bess_v1.yaml", dm)
    sites = [SiteModel() for _ in range(count)]
    servers = [
        HilModbusServer(rmap, sites[i], host="127.0.0.1", port=base_port + i)
        for i in range(count)
    ]
    for s, site in zip(servers, sites):
        site.battery.soc_pct = 50.0
        s.push_inputs()

    server_tasks = [asyncio.create_task(s.serve()) for s in servers]
    await asyncio.sleep(1.0)  # let the listeners bind

    adapters = [
        ModbusTcpAdapter("127.0.0.1", rmap, port=base_port + i, unit_id=1)
        for i in range(count)
    ]
    await asyncio.gather(*(a.connect() for a in adapters))
    # Every readable *numeric* point: the wire-compatibility proof covers all
    # telemetry. A full walkable SunSpec image also carries 'string' identity
    # registers (manufacturer, model, serial) and padding, which are structural
    # and decode to no value -- core never polls them either.
    names = [
        n for n, r in rmap.points.items() if r.rw == "r" and r.type in NUMERIC_TYPES
    ]

    # warm-up
    await asyncio.gather(*(a.read_points(names) for a in adapters))

    latencies: list[float] = []
    comm_fail = 0
    for _ in range(cycles):
        t0 = time.perf_counter()
        results = await asyncio.gather(*(a.read_points(names) for a in adapters))
        latencies.append((time.perf_counter() - t0) * 1000.0)
        for res in results:
            if any(pv.quality != "GOOD" for pv in res.values()):
                comm_fail += 1

    for a in adapters:
        await a.disconnect()
    for t in server_tasks:
        t.cancel()

    latencies.sort()
    worst = latencies[-1]
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    p50 = latencies[len(latencies) // 2]
    return {
        "count": count, "cycles": cycles, "comm_fail": comm_fail,
        "p50_ms": p50, "p95_ms": p95, "worst_ms": worst,
        "under_budget": worst < BUDGET_MS, "samples": latencies,
    }


def run(count: int = 50, cycles: int = 200, base_port: int = 16000) -> dict:
    return asyncio.run(_bench(count, cycles, base_port))


def main() -> int:
    ap = argparse.ArgumentParser(description="HIL 50-asset loop-budget benchmark")
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--cycles", type=int, default=200)
    ap.add_argument("--base-port", type=int, default=16000)
    args = ap.parse_args()

    r = run(args.count, args.cycles, args.base_port)
    print(f"\n{r['count']} servers x {r['cycles']} full poll cycles "
          f"(real ModbusTcpAdapter, read-grouped)")
    print(f"  comm failures: {r['comm_fail']}")
    print(f"  full-poll latency: p50={r['p50_ms']:.1f} ms  p95={r['p95_ms']:.1f} ms  "
          f"worst={r['worst_ms']:.1f} ms  (budget {BUDGET_MS:.0f} ms)")
    print("  histogram:")
    print(_histogram(r["samples"]))
    ok = r["under_budget"] and r["comm_fail"] == 0
    print(f"\n  {'PASS' if ok else 'FAIL'}: 50-asset poll "
          f"{'within' if r['under_budget'] else 'OVER'} the 250 ms budget.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
