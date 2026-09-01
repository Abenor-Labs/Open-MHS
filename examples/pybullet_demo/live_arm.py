#!/usr/bin/env python
"""A live KUKA iiwa in a PyBullet window, driven through the real Open-MHS middleware.

This is not a scripted render. It opens a simulation window and serves the actual
Open-MHS HTTP API on top of it, so anything that can speak to the middleware — curl, an
MCP client, Claude Desktop — moves the arm on screen in real time, and gets refused in
real time when it asks for something outside the declared envelope.

    python examples/pybullet_demo/live_arm.py

Then, from anywhere:

    curl -X POST localhost:8000/rpc -H "Authorization: Bearer $OPEN_MHS_AUTH_TOKEN" \
      -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,
      "method":"mhs.write","params":{"device_id":"kuka-live-01",
      "target":"joint_0","value":45}}'

Threading: PyBullet's Python API is not thread-safe, so every simulator call happens on
one dedicated thread. The driver posts commands to a queue and reads a snapshot dict; it
never touches pybullet itself. Without that split, an HTTP handler calling into the
simulator while the step loop is mid-frame corrupts the client.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pybullet as p
import pybullet_data
import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from drivers.base import BaseDevice  # noqa: E402
from drivers.transport import Transport, TransportError  # noqa: E402
from server.errors import SafetyLimitViolation  # noqa: E402
from server.main import create_app  # noqa: E402
from server.registry import Registry  # noqa: E402

TAG_PATH = Path(__file__).with_name("live_arm.mhs")
SIM_HZ = 240.0
N_JOINTS = 7

# Same look as the recorded demo: bright metal against a dark room.
ARM_METAL = [0.62, 0.65, 0.70, 1.0]
ARM_ACCENT = [0.86, 0.89, 0.93, 1.0]
ARM_ALARM = [0.92, 0.15, 0.16, 1.0]
FLOOR_RGBA = [0.075, 0.082, 0.095, 1.0]
ACCENT_CYAN = [0.0, 0.75, 0.85, 1.0]
BG = (0.02, 0.024, 0.03)


class SimWorld:
    """Owns the PyBullet client. Every simulator call happens on one thread and no other.

    That thread is the MAIN thread when there is a window: OpenGL contexts on Windows and
    macOS belong to the thread that created them, and driving a GUI client from a worker
    is how you get a window that opens and then never repaints. uvicorn is the one moved
    to a background thread instead.
    """

    def __init__(self, gui: bool = True) -> None:
        self._gui = gui
        self._commands: queue.Queue[tuple[int, float]] = queue.Queue()
        self._state: dict[str, float] = {f"joint_{i}_actual": 0.0 for i in range(N_JOINTS)}
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._alarm_until = 0.0
        self.robot = -1

    # --- called from the simulator thread ---

    def build(self) -> None:
        """Create the client and the scene. Call on the thread that will run the loop."""
        opts = (f"--background_color_red={BG[0]} --background_color_green={BG[1]} "
                f"--background_color_blue={BG[2]}")
        p.connect(p.GUI if self._gui else p.DIRECT, options=opts)
        p.setAdditionalSearchPath(pybullet_data.getDataPath())
        p.setGravity(0, 0, -9.81)
        p.setTimeStep(1.0 / SIM_HZ)

        slab = p.createVisualShape(p.GEOM_BOX, halfExtents=[3.0, 3.0, 0.02],
                                   rgbaColor=FLOOR_RGBA, specularColor=[0.35, 0.38, 0.42])
        coll = p.createCollisionShape(p.GEOM_BOX, halfExtents=[3.0, 3.0, 0.02])
        p.createMultiBody(0, coll, slab, [0, 0, -0.02])
        for off in (-1.15, 1.15):
            strip = p.createVisualShape(p.GEOM_BOX, halfExtents=[2.4, 0.012, 0.002],
                                        rgbaColor=ACCENT_CYAN, specularColor=[0, 0, 0])
            p.createMultiBody(0, baseVisualShapeIndex=strip, basePosition=[0, off, 0.001])

        self.robot = p.loadURDF("kuka_iiwa/model.urdf", [0, 0, 0], useFixedBase=True)
        self._paint(ARM_METAL)

        if self._gui:
            for flag in (p.COV_ENABLE_GUI, p.COV_ENABLE_RGB_BUFFER_PREVIEW,
                         p.COV_ENABLE_DEPTH_BUFFER_PREVIEW,
                         p.COV_ENABLE_SEGMENTATION_MARK_PREVIEW):
                p.configureDebugVisualizer(flag, 0)
            p.configureDebugVisualizer(p.COV_ENABLE_SHADOWS, 1)
            p.resetDebugVisualizerCamera(cameraDistance=2.6, cameraYaw=48,
                                         cameraPitch=-12, cameraTargetPosition=[0, 0, 0.65])

    def _paint(self, rgba: list[float]) -> None:
        for link in range(-1, p.getNumJoints(self.robot)):
            try:
                p.changeVisualShape(self.robot, link,
                                    rgbaColor=ARM_ACCENT if link in (0, 6) else rgba,
                                    specularColor=[0.4, 0.4, 0.45])
            except p.error:
                pass

    def loop(self) -> None:
        """Step the simulation until shutdown. Blocks; run it on the owning thread."""
        painted_alarm = False
        while not self._stop.is_set():
            while True:
                try:
                    index, radians = self._commands.get_nowait()
                except queue.Empty:
                    break
                p.setJointMotorControl2(self.robot, index, p.POSITION_CONTROL,
                                        targetPosition=radians, force=800.0, maxVelocity=0.9)   # ~52 deg/s: fast enough to
                                        # watch, slow enough to settle inside the
                                        # tag's declared settle_time_ms
            p.stepSimulation()

            snapshot = {f"joint_{i}_actual": math.degrees(p.getJointState(self.robot, i)[0])
                        for i in range(N_JOINTS)}
            with self._lock:
                self._state.update(snapshot)

            alarm = time.monotonic() < self._alarm_until
            if alarm != painted_alarm:
                self._paint(ARM_ALARM if alarm else ARM_METAL)
                painted_alarm = alarm

            time.sleep(1.0 / SIM_HZ)
        p.disconnect()

    # --- called from HTTP handler threads ---

    def wait_ready(self, timeout: float = 30.0) -> None:
        if not self._ready.wait(timeout):
            raise TransportError("the simulator window did not come up")

    def command(self, index: int, degrees: float) -> None:
        self._commands.put((index, math.radians(degrees)))

    def read(self, sensor: str) -> float:
        with self._lock:
            if sensor not in self._state:
                raise TransportError(f"no such channel on the simulator: {sensor!r}")
            return round(self._state[sensor], 3)

    def flash_alarm(self, seconds: float = 2.5) -> None:
        """Turn the arm red for a moment. Used to make a refusal visible on camera."""
        self._alarm_until = time.monotonic() + seconds

    def shutdown(self) -> None:
        self._stop.set()


class LiveTransport(Transport):
    """Bridges the driver to the simulator thread. Touches no pybullet API itself."""

    def __init__(self, world: SimWorld) -> None:
        self.world = world
        self.writes: list[tuple[str, Any]] = []

    async def transmit(self, target: str, value: Any) -> None:
        index = int(target.rsplit("_", 1)[1])
        self.world.command(index, float(value))
        self.writes.append((target, value))

    async def acquire(self, target: str) -> Any:
        if target.endswith("_actual"):
            return self.world.read(target)
        # Reading back a commanded value: report the measured one.
        return self.world.read(f"{target}_actual")


class LiveArm(BaseDevice):
    """The KUKA iiwa as an Open-MHS device.

    `write` is wrapped, not reimplemented: the safety decision still comes entirely from
    `BaseDevice`. The override only turns the arm red when a refusal happens, so a viewer
    watching the window can see the block land. It cannot cause or prevent one.
    """

    def __init__(self, tag: Any, transport: LiveTransport, **kwargs: Any) -> None:
        super().__init__(tag, transport, **kwargs)
        self._world = transport.world

    async def write(self, target: str, value: Any, *, confirmed: bool = False) -> dict[str, Any]:
        try:
            return await super().write(target, value, confirmed=confirmed)
        except SafetyLimitViolation:
            self._world.flash_alarm()
            raise


def build(gui: bool, token: str):
    """Bring up the simulator and the middleware around it. Returns (world, app).

    The caller is responsible for running `world.loop()` on this same thread.
    """
    world = SimWorld(gui=gui)
    world.build()
    world._ready.set()

    tag = json.loads(TAG_PATH.read_text(encoding="utf-8"))
    device = LiveArm(tag, LiveTransport(world))

    registry = Registry()
    registry.register(device.tag, device)
    app = create_app(registry, load_mocks=False, auth_token=token)
    return world, app


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live PyBullet arm served through the real Open-MHS middleware.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--headless", action="store_true", help="no window (for testing)")
    args = parser.parse_args()

    token = os.getenv("OPEN_MHS_AUTH_TOKEN")
    if not token:
        print("OPEN_MHS_AUTH_TOKEN is not set. This serves live hardware control; "
              "the middleware will not start without it.", file=sys.stderr)
        return 1

    world, app = build(not args.headless, token)

    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    )
    threading.Thread(target=server.run, name="open-mhs-http", daemon=True).start()

    print(f"\n  Open-MHS live arm ready on http://{args.host}:{args.port}")
    print("  device_id: kuka-live-01   7 axes, every bound tighter than the URDF")
    print("  Ctrl-C to stop.\n")
    try:
        world.loop()          # the main thread owns the window
    except KeyboardInterrupt:
        pass
    finally:
        world.shutdown()
        server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
