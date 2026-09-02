"""Mock peristaltic pump: the third device in the reference cell.

Exists so the shipped middleware has something with `max_duration_s` for the watchdog to
enforce, and so the multi-device example has an instrument that is not an arm.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from drivers.base import BaseDevice
from drivers.transport import InMemoryTransport

TAG_PATH = Path(__file__).parent / "tags" / "bench_pump.mhs"
TRAY_ML = 5.0
ML_PER_COMMAND_PER_UNIT_FLOW = 0.01


class MockPump(BaseDevice):
    def __init__(
        self,
        tag: Any = TAG_PATH,
        transport: InMemoryTransport | None = None,
        **kwargs: Any,
    ) -> None:
        transport = transport or InMemoryTransport(
            {"flow_rate": 0.0, "flow_actual": 0.0, "tray_level": 0.0},
            feedback_map={"flow_rate": "flow_actual"},
        )
        super().__init__(tag, transport, **kwargs)

    async def on_transmit(self, target: str, value: Any) -> None:
        """Every accepted flow command drips a little into the tray.

        So a reader can see a consequence of running the pump. This is the simulator's
        ground truth, not a sensor model.
        """
        if target == "flow_rate" and isinstance(self._transport, InMemoryTransport):
            state = self._transport.state
            state["tray_level"] = round(
                min(TRAY_ML, state["tray_level"] + float(value) * ML_PER_COMMAND_PER_UNIT_FLOW),
                3,
            )
