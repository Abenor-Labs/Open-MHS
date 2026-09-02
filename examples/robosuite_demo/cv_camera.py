"""Open-MHS driver for the workcell's vision system.

Read-only. It reports where the camera thinks the cube is, and — importantly — whether
that estimate actually came from the camera. `pose_source` reads `vision` when a red blob
was found and deprojected, and `ground_truth` when the cube was occluded and the simulator
was asked instead. The device never presents a value it did not measure as if it had.
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

TAG_PATH = Path(__file__).with_name("cv_camera.mhs")


class CVCameraTransport(Transport):
    """Read-only link to the workcell's perception state."""

    def __init__(self, cell: Any) -> None:
        self.cell = cell
        self.writes: list[tuple[str, Any]] = []

    async def transmit(self, target: str, value: Any) -> None:
        # Unreachable through Open-MHS: the tag declares no actuators, so the middleware
        # refuses any write with -32602 before a driver is ever called. Belt and braces.
        raise TransportError(f"{target!r} is a camera reading and cannot be written")

    async def acquire(self, target: str) -> Any:
        try:
            return self.cell.read(target)
        except KeyError as exc:
            raise TransportError(f"no such channel on the camera: {target!r}") from exc


class CVCameraDevice(BaseDevice):
    """The bench camera as an Open-MHS device. Sensors only."""

    def __init__(self, cell: Any, **kwargs: Any) -> None:
        tag = json.loads(TAG_PATH.read_text(encoding="utf-8"))
        super().__init__(tag, CVCameraTransport(cell), **kwargs)
