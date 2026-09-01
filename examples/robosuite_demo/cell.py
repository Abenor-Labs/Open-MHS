"""The robosuite workcell: one MuJoCo environment, owned by one thread.

MuJoCo and robosuite are not thread-safe, and the offscreen renderer's GL context belongs
to the thread that created it. So the environment is built and stepped on a single thread,
and everything else — the HTTP handlers, the drivers — talks to it through a command queue
and a state snapshot. No driver ever touches `env` directly.

Every number in here that could have been guessed was measured instead. See README.md.
"""

from __future__ import annotations

import math
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

#: Wrist camera, defined on the Panda's last link in robosuite's robot.xml with fovy 75.
#: This is the arm's own point of view -- what it would see if it had eyes at the tool.
#: Rendered only when asked for: a second offscreen camera costs real time every step.
POV_CAMERA = "robot0_eye_in_hand"
POV_RENDER = 320             # offscreen render size
POV_INSET = 260              # side of the picture-in-picture drawn into the viewer
POV_MARGIN = 14              # gap from the viewer's top-right corner
POV_WINDOW = "Open-MHS - robot POV"   # only used when there is no viewer to inset into

#: Render the wrist view every Nth step rather than every step. MEASURED: carried in
#: `camera_names` it costs 102 ms/step, which drops the whole cell from 19 Hz to 6.5 Hz --
#: the observable pipeline costs far more than the render. Called directly it is 29 ms,
#: and at 1-in-4 that amortises to ~7 ms. A human watching a wrist camera does not need
#: 20 Hz; the control loop does.
POV_EVERY = 4

#: Proportional gain for the delta servo. OSC_POSE takes deltas clamped to +/-0.05 m per
#: step, so the action is normalised to [-1, 1] and the controller scales it.
SERVO_KP = 10.0

#: Proportional gain for the wrist-yaw servo. OSC_POSE's action[3:6] is an axis-angle
#: delta; only the z component is driven, so roll and pitch stay where the reset put them
#: and the tool remains vertical.
YAW_KP = 2.0

#: A cube has 4-fold symmetry, so any yaw is equivalent modulo 90 degrees. Both the block
#: yaw the camera reports and the wrist yaw the arm accepts are wrapped into [0, 90).
YAW_PERIOD_DEG = 90.0

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

    def __init__(
        self, render: bool = False, interactive: bool = False, pov: bool = False
    ) -> None:
        self._render = render
        self._interactive = interactive
        self._pov = pov
        self._pov_open = False
        self._tick = 0
        self._viewer = None
        self._commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._state: dict[str, Any] = {
            "tcp_x_actual": 0.0, "tcp_y_actual": 0.0, "tcp_z_actual": 1.05,
            "tcp_yaw_actual": 0.0,
            "gripper_actual": "open", "grasping": False,
        }
        for _name in HSV_BANDS:
            self._state.update({
                f"{_name}_x": 0.0, f"{_name}_y": 0.0, f"{_name}_z": TABLE_TOP_Z,
                f"{_name}_yaw": 0.0,
                f"{_name}_source": "ground_truth", f"{_name}_pixels": 0,
            })
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._target = np.array([0.0, 0.0, 1.05])
        #: Commanded wrist yaw in degrees, or None to hold whatever it currently has.
        self._yaw_target: float | None = None
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
            # ONLY the bench camera is an observation. The wrist view is rendered on
            # demand in `_pov_frame` instead: as an observable it cost 102 ms/step and
            # dropped the cell from 19 Hz to 6.5 Hz, against 29 ms for the same render
            # called directly. The pipeline, not the pixels, was the expense.
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

    def _eef_yaw(self) -> float:
        """Wrist yaw in degrees, wrapped to [0, 90) to match a cube's symmetry."""
        from robosuite.utils import transform_utils

        mat = transform_utils.quat2mat(np.asarray(self._obs["robot0_eef_quat"], dtype=float))
        return math.degrees(math.atan2(mat[1, 0], mat[0, 0])) % YAW_PERIOD_DEG

    @staticmethod
    def _yaw_error(target_deg: float, actual_deg: float) -> float:
        """Shortest signed rotation from actual to target, given 90-degree symmetry.

        A square is indistinguishable every 90 degrees, so the arm should never turn more
        than 45 to line up. Without this the wrist would happily take the long way round
        and wind itself into a joint limit.
        """
        delta = (target_deg - actual_deg) % YAW_PERIOD_DEG
        if delta > YAW_PERIOD_DEG / 2:
            delta -= YAW_PERIOD_DEG
        return delta

    def _locate_blocks(self) -> dict[str, tuple[np.ndarray, str, int, float | None]]:
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
                found[name] = (truth, "ground_truth", 0, None)
                continue
            mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
            for low, high in bands:
                mask |= cv2.inRange(hsv, low, high)

            # Keep only the LARGEST connected blob. Averaging every matching pixel is
            # wrong whenever anything else in frame falls inside the colour band: parts of
            # the Panda sit inside the blue range, and a 79-pixel patch of gripper was
            # enough to drag blue's reported position clean across the table, following
            # the arm rather than the cube. Yaw went with it. One colour match is not one
            # object, and the camera must not average two things into a fiction.
            count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
            if count <= 1:
                found[name] = (truth, "ground_truth", 0, None)
                continue
            largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            pixels = int(stats[largest, cv2.CC_STAT_AREA])
            if pixels < MIN_BLOB_PIXELS:
                found[name] = (truth, "ground_truth", pixels, None)
                continue
            mask = np.where(labels == largest, 255, 0).astype(np.uint8)
            rows, cols = np.nonzero(mask)
            centroid = np.array([float(rows.mean()), float(cols.mean())])
            world = camera_utils.transform_from_pixels_to_world(
                pixels=centroid, depth_map=real_depth,
                camera_to_world_transform=cam_to_world)
            yaw = self._block_yaw(mask, real_depth, cam_to_world, camera_utils)
            found[name] = (np.asarray(world, dtype=float), "vision", pixels, yaw)
        return found

    @staticmethod
    def _block_yaw(mask, real_depth, cam_to_world, camera_utils) -> float | None:
        """World-frame yaw of a block, in degrees, from its silhouette. None if unusable.

        `cv2.minAreaRect` gives an angle in IMAGE space, and this camera views the table
        at a tilt, so that angle is not the world yaw — a square on the table projects to
        a non-square quadrilateral. Reporting it directly would be a sensor that lies.

        So the rectangle's corners are deprojected to world coordinates and the angle is
        taken there. Corners are pulled 25% toward the centre first: a depth sample exactly
        on the silhouette edge can land on the background behind it, which deprojects to a
        point metres away and swings the answer wildly.
        """
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        box = cv2.boxPoints(cv2.minAreaRect(max(contours, key=cv2.contourArea)))
        inner = box.mean(axis=0) + (box - box.mean(axis=0)) * 0.75

        corners = []
        for col, row in inner:                      # boxPoints is (x=col, y=row)
            corners.append(np.asarray(camera_utils.transform_from_pixels_to_world(
                pixels=np.array([float(row), float(col)]), depth_map=real_depth,
                camera_to_world_transform=cam_to_world), dtype=float))

        # Use the longer of the two adjacent edges: the shorter one is more sensitive to
        # a single bad depth sample.
        edges = [corners[1] - corners[0], corners[3] - corners[0]]
        edge = max(edges, key=lambda e: float(np.hypot(e[0], e[1])))
        if float(np.hypot(edge[0], edge[1])) < 1e-4:
            return None
        return math.degrees(math.atan2(float(edge[1]), float(edge[0]))) % YAW_PERIOD_DEG

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
            "tcp_yaw_actual": round(self._eef_yaw(), 2),
            "gripper_actual": self._grip,
            "grasping": self._grasping(),
        }
        for name, (pos, source, pixels, yaw) in blocks.items():
            update[f"{name}_x"] = round(float(pos[0]), 4)
            update[f"{name}_y"] = round(float(pos[1]), 4)
            update[f"{name}_z"] = round(float(pos[2]), 4)
            update[f"{name}_source"] = source
            update[f"{name}_pixels"] = pixels
            # -1 means "not measured": the block is occluded or its silhouette gave
            # no usable rectangle. Never a fabricated angle.
            update[f"{name}_yaw"] = round(float(yaw), 2) if yaw is not None else -1.0
        with self._lock:
            self._state.update(update)

    def _pov_frame(self) -> "np.ndarray | None":
        """The wrist view, square, with a border and a tool-centre crosshair.

        Returned in the raw (vertically flipped) orientation the renderer produced, because
        `viewer.set_images` flips once itself. Flipping here as well would stand the world
        on its head — the same convention trap that doubles the CV error in `_locate_blocks`
        when you get it backwards.
        """
        try:
            frame = self.env.sim.render(width=POV_RENDER, height=POV_RENDER,
                                        camera_name=POV_CAMERA)
        except Exception:  # noqa: BLE001 - a viewer garnish must never stop the cell
            return None
        img = cv2.resize(np.ascontiguousarray(frame), (POV_INSET, POV_INSET),
                         interpolation=cv2.INTER_AREA)

        amber = (255, 200, 80)          # RGB here: this goes to MuJoCo, not to OpenCV
        cv2.rectangle(img, (0, 0), (POV_INSET - 1, POV_INSET - 1), amber, 2)

        # Tool centre. The wrist camera looks along the approach axis, so the crosshair is
        # roughly where the jaws will close.
        c = POV_INSET // 2
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            cv2.line(img, (c + dx * 5, c + dy * 5), (c + dx * 15, c + dy * 15),
                     amber, 1, cv2.LINE_AA)

        # Label sits at the image bottom, which the viewer's flip puts at the top.
        cv2.putText(img, "WRIST CAM", (8, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                    (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, "WRIST CAM", (8, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                    amber, 1, cv2.LINE_AA)
        return img

    def _telemetry_lines(self) -> tuple[str, str]:
        """Two aligned columns for the viewer's text overlay: label, then value.

        This is what Open-MHS BELIEVES, printed beside a picture of what the arm SEES.
        Watching the two disagree is how a perception bug becomes obvious: this session had
        the bench camera reporting the robot's own gripper as a blue block for hours, and a
        block whose source reads `ground_truth` while nothing occludes it says so here.
        """
        with self._lock:
            state = dict(self._state)

        held = state.get("grasping", "nothing")
        labels = ["tcp x", "tcp y", "tcp z", "wrist yaw", "gripper", "holding", ""]
        values = [
            f"{state.get('tcp_x_actual', 0):+.3f} m",
            f"{state.get('tcp_y_actual', 0):+.3f} m",
            f"{state.get('tcp_z_actual', 0):.3f} m",
            f"{state.get('tcp_yaw_actual', 0):.1f} deg",
            str(state.get("gripper_actual", "?")),
            str(held),
            "",
        ]
        for name in HSV_BANDS:
            source = state.get(f"{name}_source", "?")
            labels.append(name.replace("_block", ""))
            values.append(
                f"{state.get(f'{name}_x', 0):+.3f},{state.get(f'{name}_y', 0):+.3f}  "
                f"{source} {state.get(f'{name}_pixels', 0)}px"
            )
        return "\n".join(labels), "\n".join(values)

    def _show_pov(self) -> None:
        """Put the wrist view into the viewer as a fixed top-right inset.

        Runs on the stepping thread, the only one allowed to touch a rendered frame — the
        GL context belongs to whoever created it, and moving this to an HTTP worker returns
        garbage rather than raising.

        With no interactive viewer to inset into, falls back to its own window so `--pov`
        still does something useful alongside `--render`.
        """
        img = self._pov_frame()
        if img is None:
            return

        if self._viewer is not None:
            import mujoco

            viewport = self._viewer.viewport
            # MjrRect is OpenGL-style: the origin is BOTTOM-left, so the top-right corner
            # is a high y. Recomputed every frame so the inset stays pinned when the window
            # is resized.
            rect = mujoco.MjrRect(
                max(0, viewport.width - POV_INSET - POV_MARGIN),
                max(0, viewport.height - POV_INSET - POV_MARGIN),
                POV_INSET,
                POV_INSET,
            )
            self._viewer.set_images((rect, img))
            self._viewer.set_texts((
                mujoco.mjtFontScale.mjFONTSCALE_100,
                mujoco.mjtGridPos.mjGRID_BOTTOMLEFT,
                *self._telemetry_lines(),
            ))
            self._pov_open = True
            return

        # No viewer: stand-alone window. OpenCV wants BGR and the un-flipped image.
        cv2.imshow(POV_WINDOW, np.flipud(img)[:, :, ::-1])
        cv2.waitKey(1)
        self._pov_open = True

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
                elif channel == "tcp_yaw":
                    self._yaw_target = float(value)
                elif channel == "gripper_state":
                    self._grip = str(value)

            error = self._target - self._eef()
            action = np.zeros(7)
            action[:3] = np.clip(error * SERVO_KP, -1.0, 1.0)
            # action[3:6] is an axis-angle delta. Roll and pitch stay at zero so the tool
            # holds the vertical approach the reset gave it; only yaw is driven, and only
            # once something has asked for it.
            if self._yaw_target is not None:
                yaw_error = self._yaw_error(self._yaw_target, self._eef_yaw())
                action[5] = float(np.clip(math.radians(yaw_error) * YAW_KP, -1.0, 1.0))
            action[6] = 1.0 if self._grip == "closed" else -1.0
            self._obs, *_ = self.env.step(action)
            self._publish()
            self._tick += 1
            if self._pov and self._tick % POV_EVERY == 0:
                self._show_pov()
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
        if self._pov_open:
            try:
                cv2.destroyWindow(POV_WINDOW)
            except Exception:  # noqa: BLE001 - a stuck window must not block exit
                pass
        time.sleep(0.2)
        if self._viewer is not None:
            try:
                self._viewer.close()
            except Exception:      # noqa: BLE001 - closing must not raise on the way out
                pass
