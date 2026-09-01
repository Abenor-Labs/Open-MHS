"""The robosuite workcell: one MuJoCo environment, owned by one thread.

MuJoCo and robosuite are not thread-safe, and the offscreen renderer's GL context belongs
to the thread that created it. So the environment is built and stepped on a single thread,
and everything else — the HTTP handlers, the drivers — talks to it through a command queue
and a state snapshot. No driver ever touches `env` directly.

Every number in here that could have been guessed was measured instead. See README.md.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

import cv2
import numpy as np

TABLE_TOP_Z = 0.800          # robosuite Lift: table_offset = (0, 0, 0.8)
CAMERA = "agentview"
CAM_SIZE = 256
CONTROL_HZ = 20

#: Proportional gain for the delta servo. OSC_POSE takes deltas clamped to +/-0.05 m per
#: step, so the action is normalised to [-1, 1] and the controller scales it.
SERVO_KP = 10.0

#: HSV bands per block colour. Hue wraps at 180 in OpenCV, so red needs two.
#: These are wide enough to survive shading and narrow enough that no two blocks overlap.
HSV_BANDS: dict[str, list[tuple[tuple[int, int, int], tuple[int, int, int]]]] = {
    "red_block":    [((0, 120, 70), (10, 255, 255)), ((170, 120, 70), (180, 255, 255))],
    "green_block":  [((40, 90, 60), (85, 255, 255))],
    "blue_block":   [((100, 110, 60), (130, 255, 255))],
    "yellow_block": [((20, 120, 90), (35, 255, 255))],
}
MIN_BLOB_PIXELS = 20


class Workcell:
    """Owns the robosuite environment. All MuJoCo calls happen on the stepping thread."""

    def __init__(self, render: bool = False, interactive: bool = False) -> None:
        self._render = render
        self._interactive = interactive
        self._viewer = None
        self._commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._state: dict[str, Any] = {
            "tcp_x_actual": 0.0, "tcp_y_actual": 0.0, "tcp_z_actual": 1.05,
            "gripper_actual": "open", "grasping": False,
        }
        for _name in HSV_BANDS:
            self._state.update({
                f"{_name}_x": 0.0, f"{_name}_y": 0.0, f"{_name}_z": TABLE_TOP_Z,
                f"{_name}_source": "ground_truth", f"{_name}_pixels": 0,
            })
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._target = np.array([0.0, 0.0, 1.05])
        self._grip = "open"
        self.env = None

    # --- owning thread only ---

    def build(self) -> None:
        import robosuite as suite
        from robosuite.controllers import load_composite_controller_config
        from robosuite.environments.base import register_env

        from multi_cube_env import MultiBlockCell

        register_env(MultiBlockCell)
        config = load_composite_controller_config(controller=None, robot="Panda")
        self.env = suite.make(
            "MultiBlockCell",
            robots="Panda",
            controller_configs=config,
            has_renderer=self._render,
            has_offscreen_renderer=True,
            use_camera_obs=True,
            camera_names=CAMERA,
            camera_heights=CAM_SIZE,
            camera_widths=CAM_SIZE,
            camera_depths=True,
            control_freq=CONTROL_HZ,
            horizon=10_000_000,
            ignore_done=True,
            hard_reset=False,
        )
        self._obs = self.env.reset()

        if self._interactive:
            # robosuite's own on-screen renderer is a fixed camera with no mouse input.
            # MuJoCo's passive viewer is the real thing: orbit with left-drag, pan with
            # right-drag, zoom on the wheel, plus the standard viewer panels. It is driven
            # by us calling sync() each step rather than owning the loop.
            import mujoco.viewer
            self._viewer = mujoco.viewer.launch_passive(
                self.env.sim.model._model, self.env.sim.data._data,
                show_left_ui=False, show_right_ui=False,
            )

        self._target = np.asarray(self._obs["robot0_eef_pos"], dtype=float).copy()
        self._publish()
        self._ready.set()

    def _eef(self) -> np.ndarray:
        return np.asarray(self._obs["robot0_eef_pos"], dtype=float)

    def _locate_blocks(self) -> dict[str, tuple[np.ndarray, str, int]]:
        """Find every block by looking at it. Falls back per block, and says which.

        The RGB frame comes back vertically flipped relative to the depth convention used
        by robosuite's deprojection helpers, so both are flipped before use. Getting that
        backwards silently doubles the error instead of raising.

        A block that is occluded — by the arm, or by another block — reports
        `ground_truth` rather than a fabricated estimate. Per block, not globally: the
        agent can trust the ones it can actually see.
        """
        from robosuite.utils import camera_utils

        rgb = self._obs.get(f"{CAMERA}_image")
        depth = self._obs.get(f"{CAMERA}_depth")
        found: dict[str, tuple[np.ndarray, str, int]] = {}

        hsv = None
        real_depth = cam_to_world = None
        if rgb is not None and depth is not None:
            image = np.ascontiguousarray(np.flipud(rgb)).astype(np.uint8)
            hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
            real_depth = camera_utils.get_real_depth_map(
                self.env.sim, np.ascontiguousarray(np.flipud(depth)))
            cam_to_world = np.linalg.inv(camera_utils.get_camera_transform_matrix(
                self.env.sim, CAMERA, CAM_SIZE, CAM_SIZE))

        for name, bands in HSV_BANDS.items():
            truth = self.env.block_position(name)
            if hsv is None:
                found[name] = (truth, "ground_truth", 0)
                continue
            mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
            for low, high in bands:
                mask |= cv2.inRange(hsv, low, high)
            pixels = int(mask.sum() // 255)
            if pixels < MIN_BLOB_PIXELS:
                found[name] = (truth, "ground_truth", pixels)
                continue
            rows, cols = np.nonzero(mask)
            centroid = np.array([float(rows.mean()), float(cols.mean())])
            world = camera_utils.transform_from_pixels_to_world(
                pixels=centroid, depth_map=real_depth,
                camera_to_world_transform=cam_to_world)
            found[name] = (np.asarray(world, dtype=float), "vision", pixels)
        return found

    def _grasping(self) -> str:
        """Which block the gripper actually has, by contact check. 'nothing' if none."""
        for block in getattr(self.env, "blocks", []):
            try:
                if self.env._check_grasp(gripper=self.env.robots[0].gripper,
                                         object_geoms=block):
                    return block.name
            except Exception:  # noqa: BLE001 - a missing helper must not stop the cell
                continue
        return "nothing"

    def _publish(self) -> None:
        eef = self._eef()
        blocks = self._locate_blocks()
        update: dict[str, Any] = {
            "tcp_x_actual": round(float(eef[0]), 4),
            "tcp_y_actual": round(float(eef[1]), 4),
            "tcp_z_actual": round(float(eef[2]), 4),
            "gripper_actual": self._grip,
            "grasping": self._grasping(),
        }
        for name, (pos, source, pixels) in blocks.items():
            update[f"{name}_x"] = round(float(pos[0]), 4)
            update[f"{name}_y"] = round(float(pos[1]), 4)
            update[f"{name}_z"] = round(float(pos[2]), 4)
            update[f"{name}_source"] = source
            update[f"{name}_pixels"] = pixels
        with self._lock:
            self._state.update(update)

    def loop(self) -> None:
        """Step the environment until shutdown. Blocks; run on the owning thread."""
        while not self._stop.is_set():
            while True:
                try:
                    channel, value = self._commands.get_nowait()
                except queue.Empty:
                    break
                if channel in ("tcp_x", "tcp_y", "tcp_z"):
                    self._target["xyz".index(channel[-1])] = float(value)
                elif channel == "gripper_state":
                    self._grip = str(value)

            error = self._target - self._eef()
            action = np.zeros(7)
            action[:3] = np.clip(error * SERVO_KP, -1.0, 1.0)
            action[6] = 1.0 if self._grip == "closed" else -1.0
            self._obs, *_ = self.env.step(action)
            self._publish()
            if self._viewer is not None:
                if not self._viewer.is_running():
                    break
                self._viewer.sync()
            elif self._render:
                self.env.render()

    # --- called from HTTP handler threads ---

    def wait_ready(self, timeout: float = 180.0) -> None:
        if not self._ready.wait(timeout):
            raise RuntimeError("the robosuite environment did not come up")

    def command(self, channel: str, value: Any) -> None:
        self._commands.put((channel, value))

    def read(self, channel: str) -> Any:
        with self._lock:
            if channel not in self._state:
                raise KeyError(channel)
            return self._state[channel]

    def shutdown(self) -> None:
        self._stop.set()
        time.sleep(0.2)
        if self._viewer is not None:
            try:
                self._viewer.close()
            except Exception:      # noqa: BLE001 - closing must not raise on the way out
                pass
