"""Open-MHS driver for the Panda arm in the robosuite workcell.

The device is a Cartesian tool pose, not a set of joint angles, so the safety limits in
`panda_arm.mhs` describe a physical work envelope. All the safety behaviour — bounds,
the human-confirmation gate, closed-loop verification against the feedback sensors — is
inherited from `BaseDevice` unchanged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from open_mhs.drivers.base import BaseDevice          # noqa: E402
from open_mhs.drivers.transport import Transport, TransportError   # noqa: E402

TAG_PATH = Path(__file__).with_name("panda_arm.mhs")


class PandaArmTransport(Transport):
    """Bridges the driver to the workcell thread. Touches no MuJoCo API itself."""

    def __init__(self, cell: Any) -> None:
        self.cell = cell
        self.writes: list[tuple[str, Any]] = []

    async def transmit(self, target: str, value: Any) -> None:
        self.cell.command(target, value)
        self.writes.append((target, value))

    async def acquire(self, target: str) -> Any:
        # A commanded channel reads back through its measured counterpart.
        lookup = {"tcp_x": "tcp_x_actual", "tcp_y": "tcp_y_actual",
                  "tcp_z": "tcp_z_actual", "tcp_yaw": "tcp_yaw_actual",
                  "gripper_state": "gripper_actual"}
        try:
            return self.cell.read(lookup.get(target, target))
        except KeyError as exc:
            raise TransportError(f"no such channel on the workcell: {target!r}") from exc


class PandaArmDevice(BaseDevice):
    """The Panda as an Open-MHS device."""

    def __init__(self, cell: Any, **kwargs: Any) -> None:
        tag = json.loads(TAG_PATH.read_text(encoding="utf-8"))
        super().__init__(tag, PandaArmTransport(cell), **kwargs)
