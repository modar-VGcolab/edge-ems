"""Profile playback: drive simulated device points from a CSV timeline.

CSV columns: time_s, point, value — applied in time order.
"""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
from pathlib import Path

from simulator.device import SimulatedDevice


@dataclass(frozen=True)
class ProfileStep:
    time_s: float
    point: str
    value: float


def load_profile_csv(path: str | Path) -> list[ProfileStep]:
    steps: list[ProfileStep] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            steps.append(
                ProfileStep(
                    time_s=float(row["time_s"]),
                    point=row["point"].strip(),
                    value=float(row["value"]),
                )
            )
    return sorted(steps, key=lambda s: s.time_s)


async def play(device: SimulatedDevice, steps: list[ProfileStep], speed: float = 1.0) -> None:
    """Apply steps at their timestamps; speed>1 compresses time (useful in tests)."""
    now = 0.0
    for step in steps:
        delay = (step.time_s - now) / speed
        if delay > 0:
            await asyncio.sleep(delay)
        device.set_point(step.point, step.value)
        now = step.time_s
