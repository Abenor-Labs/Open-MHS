"""Mock temperature sensor — the minimal conforming Open-MHS device.

Read-only: it declares no actuators, so `write()` is rejected for every target and there
are no safety limits to enforce. Useful as the smallest possible end-to-end exercise of
discovery plus the `read` primitive.

Run standalone to register against a live middleware instance:

    python -m drivers.mock_temp_sensor --registry http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import asyncio
import random
from typing import Any

from drivers.base import BaseDevice
from drivers.transport import InMemoryTransport

TAG_PATH = "examples/mock_temp_sensor.mhs"


class MockTempSensor(BaseDevice):
    """Two-channel environmental sensor with reproducible drift.

    Drift is driven by a seeded RNG owned by the instance, so successive reads vary the
    way real hardware does while a test run stays deterministic and instances never share
    state with each other.
    """

    def __init__(
        self,
        tag: Any = TAG_PATH,
        transport: InMemoryTransport | None = None,
        *,
        seed: int = 0,
        base_temp_c: float = 21.0,
        base_humidity_pct: float = 43.0,
        drift_c: float = 0.25,
        **kwargs: Any,
    ) -> None:
        transport = transport or InMemoryTransport(
            {
                "ambient_temp": base_temp_c,
                "relative_humidity": base_humidity_pct,
                "link_state": "online",
            }
        )
        super().__init__(tag, transport, **kwargs)
        self._rng = random.Random(seed)
        self._drift_c = drift_c

    def decode(self, target: str, raw: Any) -> Any:
        """Apply sensor noise, then round to the precision the tag claims."""
        if target == "ambient_temp":
            return round(raw + self._rng.uniform(-self._drift_c, self._drift_c), 2)
        if target == "relative_humidity":
            return round(raw + self._rng.uniform(-1.0, 1.0), 1)
        return raw


async def _main() -> None:
    import httpx

    parser = argparse.ArgumentParser(description="Run the Open-MHS mock temperature sensor.")
    parser.add_argument("--registry", default=None, help="Registry base URL")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between reads")
    args = parser.parse_args()

    device = MockTempSensor()
    async with httpx.AsyncClient(timeout=5.0) as client:
        result = await device.register(client, args.registry)
        print(f"registered: {result}")
        while True:
            temp = await device.read("ambient_temp")
            rh = await device.read("relative_humidity")
            print(f"{device.device_id}  ambient_temp={temp} degC  relative_humidity={rh} %")
            await client.post(f"{(args.registry or '').rstrip('/')}/devices/{device.device_id}/heartbeat")
            await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(_main())
