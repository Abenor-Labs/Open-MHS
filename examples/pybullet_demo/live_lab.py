#!/usr/bin/env python
"""A live KUKA iiwa over a lab bench, driven through the real Open-MHS middleware.

Not a scripted render. This opens a simulation window and serves the actual Open-MHS HTTP
API on top of it, so anything that speaks to the middleware — curl, an MCP client, Claude
Desktop — moves the arm on screen in real time, and is refused in real time when it asks
for something outside the declared envelope.

    export OPEN_MHS_AUTH_TOKEN="..."
    python examples/pybullet_demo/live_lab.py

The agent commands a **tool pose in metres**, not motor angles, so the safety limits in
`live_lab.mhs` describe a physical work envelope: a box above the bench. The arm can
easily reach outside it — commanding `tcp_z = 0.5` would drive the tool through a bench
surface that sits at 0.626 m — and the capability tag is the only thing that stops it.

Threading: PyBullet's Python API is not thread-safe and its GUI client belongs to the
thread that created it, so the simulator owns the MAIN thread and uvicorn runs on a
worker. The driver posts commands to a queue and reads a snapshot dict; it never touches
pybullet directly.
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

TAG_PATH = Path(__file__).with_name("live_lab.mhs")
SIM_HZ = 240.0
TOOL_LINK = 6
BENCH_TOP = 0.626
BENCH_CENTRE = 0.50
ARM_BASE = [0.15, 0.0, BENCH_TOP]   # the arm is bolted to the bench, as a real cell is
GRASP_RANGE = 0.12          # how close the tool must be before the gripper can take hold

ARM_METAL = [0.62, 0.65, 0.70, 1.0]
ARM_ACCENT = [0.86, 0.89, 0.93, 1.0]
ARM_ALARM = [0.92, 0.15, 0.16, 1.0]
BG = (0.02, 0.024, 0.03)

#: name -> (colour rgba, x, y) on the bench top.
PARTS = {
    "red_cube":   ([0.85, 0.10, 0.10, 1.0], 0.52, -0.18),
    "green_cube": ([0.15, 0.75, 0.25, 1.0], 0.58, 0.00),
    "blue_cube":  ([0.15, 0.35, 0.90, 1.0], 0.52, 0.18),
}
#: Elbow-up home pose. Measured, not guessed: it puts the tool inside the declared
#: envelope, so the cell does not start out of bounds.
REST_POSE = [0.0, 0.6, 0.0, -1.5, 0.0, 1.0, 0.0]

#: Real iiwa joint limits, handed to the IK solver so it cannot return a pose the robot
#: could not hold.
JOINT_LOW = [math.radians(d) for d in (-170, -120, -170, -120, -170, -120, -175)]
JOINT_HIGH = [math.radians(d) for d in (170, 120, 170, 120, 170, 120, 175)]
JOINT_RANGE = [h - lo for lo, h in zip(JOINT_LOW, JOINT_HIGH)]
#: Tool pointing straight down at the bench. Pinning it is what keeps the arm upright.
TOOL_DOWN = (0.0, 1.0, 0.0, 0.0)


class SimWorld:
    """Owns the PyBullet client. Every simulator call happens on one thread and no other."""

    def __init__(self, gui: bool = True) -> None:
        self._gui = gui
        self._commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._state: dict[str, Any] = {
            "tcp_x_actual": 0.60, "tcp_y_actual": 0.0, "tcp_z_actual": 0.95,
            "gripper_state": "open", "holding": "nothing",
            "nearest_object": "nothing", "nearest_object_range": 9.99,
        }
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._alarm_until = 0.0
        self.robot = -1
        self._parts: dict[str, int] = {}
        self._target = [0.60, 0.0, 0.95]
        self._grip = "open"
        self._held: str | None = None
        self._constraint = -1

    # --- built and stepped on the owning thread ---

    def build(self) -> None:
        opts = (f"--background_color_red={BG[0]} --background_color_green={BG[1]} "
                f"--background_color_blue={BG[2]}")
        p.connect(p.GUI if self._gui else p.DIRECT, options=opts)
        p.setAdditionalSearchPath(pybullet_data.getDataPath())
        p.setGravity(0, 0, -9.81)
        p.setTimeStep(1.0 / SIM_HZ)
        p.setPhysicsEngineParameter(numSolverIterations=150)

        # A dark slab, not `plane.urdf` - that ships the blue checkerboard, which is the
        # exact "default physics sample" look this demo is meant to avoid.
        floor_v = p.createVisualShape(p.GEOM_BOX, halfExtents=[4.0, 4.0, 0.02],
                                      rgbaColor=[0.055, 0.060, 0.070, 1.0],
                                      specularColor=[0.25, 0.27, 0.30])
        floor_c = p.createCollisionShape(p.GEOM_BOX, halfExtents=[4.0, 4.0, 0.02])
        p.createMultiBody(0, floor_c, floor_v, [0, 0, -0.02])
        # A dark enclosure. PyBullet's --background_color_* connect options do not reach
        # the offscreen renderer at all and did not take in the GUI either, so the "dark
        # lab" has to be geometry rather than a clear colour.
        for pos, half in (([0.4, -2.6, 1.6], [4.0, 0.02, 2.2]),
                          ([0.4, 2.6, 1.6], [4.0, 0.02, 2.2]),
                          ([-2.4, 0.0, 1.6], [0.02, 4.0, 2.2]),
                          ([3.4, 0.0, 1.6], [0.02, 4.0, 2.2]),
                          ([0.4, 0.0, 3.4], [4.0, 4.0, 0.02])):
            wall = p.createVisualShape(p.GEOM_BOX, halfExtents=half,
                                       rgbaColor=[0.035, 0.040, 0.050, 1.0],
                                       specularColor=[0.05, 0.05, 0.06])
            p.createMultiBody(0, baseVisualShapeIndex=wall, basePosition=pos)

        # Two recessed light strips in the floor, running along the bench rather than
        # slashing across the whole frame.
        for off in (-0.95, 0.95):
            strip = p.createVisualShape(p.GEOM_BOX, halfExtents=[0.9, 0.010, 0.002],
                                        rgbaColor=[0.0, 0.80, 0.90, 1.0],
                                        specularColor=[0, 0, 0])
            p.createMultiBody(0, baseVisualShapeIndex=strip, basePosition=[0.5, off, 0.001])
        # Rotated 90 deg so the bench runs across the shot rather than into the arm.
        bench = p.loadURDF("table/table.urdf", [BENCH_CENTRE, 0, 0],
                           p.getQuaternionFromEuler([0, 0, math.pi / 2]), useFixedBase=True)
        # The stock table is pale pine. Repaint it as a dark steel bench.
        for link in range(-1, p.getNumJoints(bench)):
            p.changeVisualShape(bench, link, rgbaColor=[0.16, 0.17, 0.19, 1.0],
                                specularColor=[0.30, 0.32, 0.35])
        p.loadURDF("tray/tray.urdf", [0.72, 0.22, BENCH_TOP], useFixedBase=True,
                   globalScaling=0.4)

        # Mounted ON the bench. With the arm on the floor its lower links foul the bench
        # edge on the way over, and the measured IK error across the envelope was ten
        # times worse.
        self.robot = p.loadURDF("kuka_iiwa/model.urdf", ARM_BASE, useFixedBase=True)
        for i, angle in enumerate(REST_POSE):
            p.resetJointState(self.robot, i, angle)
        self._paint(ARM_METAL)

        for name, (rgba, x, y) in PARTS.items():
            body = p.loadURDF("cube_small.urdf", [x, y, BENCH_TOP + 0.025])
            p.changeVisualShape(body, -1, rgbaColor=rgba)
            p.changeDynamics(body, -1, mass=0.08, lateralFriction=1.2)
            self._parts[name] = body

        if self._gui:
            for flag in (p.COV_ENABLE_GUI, p.COV_ENABLE_RGB_BUFFER_PREVIEW,
                         p.COV_ENABLE_DEPTH_BUFFER_PREVIEW,
                         p.COV_ENABLE_SEGMENTATION_MARK_PREVIEW):
                p.configureDebugVisualizer(flag, 0)
            p.configureDebugVisualizer(p.COV_ENABLE_SHADOWS, 1)
            p.resetDebugVisualizerCamera(cameraDistance=1.85, cameraYaw=52,
                                         cameraPitch=-18,
                                         cameraTargetPosition=[0.55, 0.0, 0.85])
        self._ready.set()

    def _paint(self, rgba: list[float]) -> None:
        for link in range(-1, p.getNumJoints(self.robot)):
            try:
                p.changeVisualShape(self.robot, link,
                                    rgbaColor=ARM_ACCENT if link in (0, 6) else rgba,
                                    specularColor=[0.4, 0.4, 0.45])
            except p.error:
                pass

    def _tool_position(self) -> list[float]:
        return list(p.getLinkState(self.robot, TOOL_LINK, computeForwardKinematics=1)[4])

    def _drive_to_target(self) -> None:
        """Solve IK for the current tool target and command the joints toward it.

        The tool orientation is pinned pointing straight down and the solver is given the
        real joint limits. Position-only IK admits infinitely many solutions, and without
        this the elbow and wrist flip between successive commands until the arm folds
        through itself and through the bench.
        """
        angles = p.calculateInverseKinematics(
            self.robot, TOOL_LINK, self._target,
            targetOrientation=TOOL_DOWN,
            lowerLimits=JOINT_LOW, upperLimits=JOINT_HIGH, jointRanges=JOINT_RANGE,
            restPoses=REST_POSE, maxNumIterations=300, residualThreshold=1e-5,
        )
        for i, angle in enumerate(angles[:7]):
            p.setJointMotorControl2(self.robot, i, p.POSITION_CONTROL,
                                    targetPosition=angle, force=400.0, maxVelocity=1.1)

    def _try_grasp(self) -> None:
        """Attach the nearest part if the tool is close enough. Otherwise close on air."""
        if self._held is not None:
            return
        tool = self._tool_position()
        name, dist = self._nearest(tool)
        if name is None or dist > GRASP_RANGE:
            return
        body = self._parts[name]
        pos, _ = p.getBasePositionAndOrientation(body)
        self._constraint = p.createConstraint(
            self.robot, TOOL_LINK, body, -1, p.JOINT_FIXED, [0, 0, 0],
            parentFramePosition=[c - t for c, t in zip(pos, tool)],
            childFramePosition=[0, 0, 0],
        )
        self._held = name

    def _release(self) -> None:
        if self._constraint >= 0:
            p.removeConstraint(self._constraint)
            self._constraint = -1
        self._held = None

    def _nearest(self, tool: list[float]) -> tuple[str | None, float]:
        best, best_d = None, 9.99
        for name, body in self._parts.items():
            pos, _ = p.getBasePositionAndOrientation(body)
            d = math.dist(pos, tool)
            if d < best_d:
                best, best_d = name, d
        return best, best_d

    def loop(self) -> None:
        """Step the simulation until shutdown. Blocks; run on the owning thread."""
        painted_alarm = False
        while not self._stop.is_set():
            while True:
                try:
                    channel, value = self._commands.get_nowait()
                except queue.Empty:
                    break
                if channel in ("tcp_x", "tcp_y", "tcp_z"):
                    self._target["xyz".index(channel[-1])] = float(value)
                elif channel == "gripper":
                    self._grip = str(value)
                    self._try_grasp() if self._grip == "closed" else self._release()

            self._drive_to_target()
            p.stepSimulation()

            tool = self._tool_position()
            near, near_d = self._nearest(tool)
            parts_pose = {}
            for name, body in self._parts.items():
                pos, _ = p.getBasePositionAndOrientation(body)
                for axis, v in zip("xyz", pos):
                    parts_pose[f"{name}_{axis}"] = round(v, 4)
            with self._lock:
                self._state.update(parts_pose)
                self._state.update({
                    "tcp_x_actual": round(tool[0], 4),
                    "tcp_y_actual": round(tool[1], 4),
                    "tcp_z_actual": round(tool[2], 4),
                    "gripper_state": self._grip,
                    "holding": self._held or "nothing",
                    "nearest_object": near or "nothing",
                    "nearest_object_range": round(near_d, 4),
                })

            alarm = time.monotonic() < self._alarm_until
            if alarm != painted_alarm:
                self._paint(ARM_ALARM if alarm else ARM_METAL)
                painted_alarm = alarm
            time.sleep(1.0 / SIM_HZ)
        p.disconnect()

    # --- called from HTTP handler threads ---

    def command(self, channel: str, value: Any) -> None:
        self._commands.put((channel, value))

    def read(self, sensor: str) -> Any:
        with self._lock:
            if sensor not in self._state:
                raise TransportError(f"no such channel on the cell: {sensor!r}")
            return self._state[sensor]

    def flash_alarm(self, seconds: float = 2.5) -> None:
        """Turn the arm red briefly, so a refusal is visible in the window."""
        self._alarm_until = time.monotonic() + seconds

    def shutdown(self) -> None:
        self._stop.set()


class LabTransport(Transport):
    """Bridges the driver to the simulator thread. Touches no pybullet API itself."""

    def __init__(self, world: SimWorld) -> None:
        self.world = world
        self.writes: list[tuple[str, Any]] = []

    async def transmit(self, target: str, value: Any) -> None:
        self.world.command(target, value)
        self.writes.append((target, value))

    async def acquire(self, target: str) -> Any:
        if target in ("tcp_x", "tcp_y", "tcp_z"):
            return self.world.read(f"{target}_actual")
        if target == "gripper":
            return self.world.read("gripper_state")
        return self.world.read(target)


class LiveArm(BaseDevice):
    """The cell as an Open-MHS device.

    `write` is wrapped, not reimplemented: the safety decision still comes entirely from
    `BaseDevice`. The override only turns the arm red when a refusal happens, so a viewer
    watching the window can see the block land. It cannot cause or prevent one.
    """

    def __init__(self, tag: Any, transport: LabTransport, **kwargs: Any) -> None:
        super().__init__(tag, transport, **kwargs)
        self._world = transport.world

    async def write(self, target: str, value: Any, *, confirmed: bool = False) -> dict[str, Any]:
        try:
            return await super().write(target, value, confirmed=confirmed)
        except SafetyLimitViolation:
            self._world.flash_alarm()
            raise


def build(gui: bool, token: str):
    """Bring up the cell and the middleware around it. Caller runs `world.loop()`."""
    world = SimWorld(gui=gui)
    world.build()

    device = LiveArm(json.loads(TAG_PATH.read_text(encoding="utf-8")), LabTransport(world))
    registry = Registry()
    registry.register(device.tag, device)
    return world, create_app(registry, load_mocks=False, auth_token=token)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live PyBullet lab cell served through the real Open-MHS middleware.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--headless", action="store_true", help="no window (for testing)")
    args = parser.parse_args()

    token = os.getenv("OPEN_MHS_AUTH_TOKEN")
    if not token:
        print("OPEN_MHS_AUTH_TOKEN is not set. This serves live hardware control; the "
              "middleware will not start without it.", file=sys.stderr)
        return 1

    world, app = build(not args.headless, token)
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    threading.Thread(target=server.run, name="open-mhs-http", daemon=True).start()

    print(f"\n  Open-MHS lab cell ready on http://{args.host}:{args.port}")
    print("  device_id: kuka-lab-01   tool pose in metres, bounded to a work envelope")
    print(f"  parts on the bench: {', '.join(PARTS)}")
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
