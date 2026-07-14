"""CLI: launch N simulated devices from a register map.

Example:
    python -m simulator.main --map maps/custom_bess_v1.yaml --port 15020 --count 3 \
        --profile profiles/charge_cycle.csv --speed 10
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from common.data_model import DataModel
from common.register_map import load_register_map

from simulator.device import SimulatedDevice
from simulator.profiles import load_profile_csv, play


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="edge-ems device simulator")
    p.add_argument("--map", required=True, help="register map yaml")
    p.add_argument("--data-model", default="data_model.yaml")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=15020, help="first port; device i uses port+i")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--profile", default=None, help="CSV timeline (time_s,point,value)")
    p.add_argument("--speed", type=float, default=1.0)
    return p.parse_args()


async def run(args: argparse.Namespace) -> None:
    dm = DataModel.load(Path(args.data_model))
    rmap = load_register_map(Path(args.map), dm)
    devices = [SimulatedDevice(rmap) for _ in range(args.count)]
    tasks = [asyncio.create_task(d.serve(args.host, args.port + i)) for i, d in enumerate(devices)]
    print(
        f"simulator: {args.count}x {rmap.device or rmap.asset_class} "
        f"on {args.host}:{args.port}..{args.port + args.count - 1}"
    )
    if args.profile:
        steps = load_profile_csv(args.profile)
        tasks += [asyncio.create_task(play(d, steps, args.speed)) for d in devices]
    await asyncio.gather(*tasks)


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
