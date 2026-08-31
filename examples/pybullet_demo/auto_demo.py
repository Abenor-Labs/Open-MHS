#!/usr/bin/env python
"""Open-MHS cinematic demo: an LLM hallucinates a command, and the safety envelope holds.

Runs a scripted ~60-second sequence against a KUKA iiwa in PyBullet and writes a
ready-to-upload MP4 next to this file:

    1. discovery      the agent learns what exists and what its bounds are
    2. safe move      joint_0 -> 45 deg, inside the declared envelope, and the arm moves
    3. hallucination  the agent asks for 300 deg
    4. the block      Open-MHS refuses; the arm does not move
    5. correction     the agent reads the refusal, and returns to 0 deg

Three things worth knowing about how this is built:

**The refusal is not staged.** This script imports `server.safety` and calls the same
`check_write()` the middleware calls, so the error object on screen is the real one, built
from the real capability tag in `demo_arm.mhs`. If the repo is not importable the script
says so on screen and falls back to an equivalent local check rather than pretending.

**Frames are captured per physics tick, not per wall-clock second.** One frame every 8
steps of a 240 Hz simulation is exactly 30 fps of simulated time, so the video's timeline
and the simulation's timeline are the same timeline. Capturing on a wall-clock timer would
drift against the physics the moment a frame took longer than its slot, and "perfectly
synced" would be unachievable by construction.

**The terminal narration is composited into the frames.** The MP4 has to carry the whole
story on its own - an arm moving without the argument beside it is just a robot video.

Run it:

    pip install -r examples/pybullet_demo/requirements-demo.txt
    python examples/pybullet_demo/auto_demo.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import pybullet as p
import pybullet_data

# --------------------------------------------------------------------------------------
# Terminal styling
# --------------------------------------------------------------------------------------

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"

BLUE = "\033[94m"          # the agent thinking
GREEN = "\033[92m"         # a safe action that went through
YELLOW = "\033[93m"        # the agent about to do something questionable
RED_BOLD = "\033[1;91m"    # Open-MHS refusing
GREY = "\033[90m"          # protocol / wire detail
CYAN = "\033[96m"          # narration

#: ANSI colour -> BGR, for the copy of each line that goes into the video.
OVERLAY_BGR: dict[str, tuple[int, int, int]] = {
    BLUE: (255, 176, 96),
    GREEN: (120, 230, 140),
    YELLOW: (90, 200, 250),
    RED_BOLD: (70, 70, 255),
    GREY: (140, 140, 140),
    CYAN: (230, 220, 120),
}
DEFAULT_BGR = (200, 200, 200)

#: OpenCV's Hershey fonts are ASCII-only: anything outside it is drawn as "??". The
#: terminal keeps the real characters; only the copy burned into the video is folded down.
ASCII_FOLD = str.maketrans({
    "·": "-", "─": "-", "—": "-", "–": "-", "→": "->",
    "±": "+/-", "°": " deg", "█": "#", "…": "...",
    "“": '"', "”": '"', "‘": "'", "’": "'", "✓": "OK",
    "✋": "", "²": "2", "•": "-",
})


def to_ascii(text: str) -> str:
    """Fold a line down to what cv2 can actually draw."""
    return text.translate(ASCII_FOLD).encode("ascii", "replace").decode("ascii")

TAG_PATH = Path(__file__).with_name("demo_arm.mhs")
VIDEO_PATH = Path(__file__).with_name("open_mhs_cinematic_demo.mp4")
REPO_ROOT = Path(__file__).resolve().parents[2]

JOINT_INDEX = 0
TARGET = "joint_0"
DEVICE_ID = "pybullet-arm-01"

# --------------------------------------------------------------------------------------
# Timing. Everything downstream is derived from these two numbers.
# --------------------------------------------------------------------------------------

SIM_HZ = 240.0
VIDEO_FPS = 30
TICKS_PER_FRAME = int(SIM_HZ / VIDEO_FPS)   # 8

SETTLE_TOLERANCE_DEG = 1.0
SETTLE_CAP_S = 4.0

#: Scales every typing delay. Tuned with `--dry-run` so the exported video lands at 59.9 s
#: against a 60-second brief: the holds contribute a fixed 13.3 s and the typing scales
#: linearly at 32.3 s per unit. Holds are deliberately NOT scaled - the two-second pause on
#: the blocked command is the shot that proves the arm did not move.
#:
#: Tune it by measuring, not by guessing: `auto_demo.py --dry-run --speed X` reports the
#: exported duration in a couple of seconds without rendering a single frame.
TYPING_SPEED = 1.44

# --------------------------------------------------------------------------------------
# Look
# --------------------------------------------------------------------------------------

FRAME_W, FRAME_H = 1280, 720

BG = (0.02, 0.024, 0.03)            # near-black, faintly blue
FLOOR_RGBA = [0.075, 0.082, 0.095, 1.0]
ACCENT_CYAN = [0.0, 0.75, 0.85, 1.0]

#: The subject has to be VISIBLE. A dark arm on a dark floor under low ambient light
#: renders as a silhouette you cannot read - the first cut of this demo lost the robot
#: entirely. Bright brushed metal against the dark room is the whole contrast budget.
ARM_DARK = [0.62, 0.65, 0.70, 1.0]
ARM_ACCENT = [0.86, 0.89, 0.93, 1.0]
ARM_ALARM = [0.92, 0.15, 0.16, 1.0]

#: Target is offset in X so the arm sits in the RIGHT third of the frame, leaving the left
#: for narration. It is also raised to 0.70 m: the iiwa is ~1.3 m tall and the first cut
#: cropped its head off.
CAM_TARGET = [-0.55, 0.0, 0.70]
CAM_DISTANCE = 3.05
CAM_YAW = 42.0
#: Positive pitch puts the camera BELOW the target, looking up at the arm - low and
#: dramatic, and at this distance the lens still clears the floor.
CAM_PITCH = 8.0
CAM_FOV = 40.0
CAM_DRIFT_DEG_PER_S = 1.1            # slow orbit, enough to feel alive

LIGHT_DIRECTION = [-2.2, -1.4, 2.6]
LIGHT_COLOR = [1.0, 0.96, 0.9]

#: The floor is sized to what the lens actually sees, not to the world. It is a shadow
#: receiver, and TinyRenderer's shadow pass costs roughly in proportion to its area: a
#: 14x14 m slab measured 797 ms/frame at 720p against 378 ms for this one.
FLOOR_HALF_EXTENT = 2.6
STRIP_HALF_LENGTH = 2.2
SHADOWS = True

_client: int | None = None
_robot: int = -1
_hud_id: int = -1
_fast = False
_headless = False
_ticks = 0
_recorder: Recorder | None = None
_overlay: deque[tuple[str, tuple[int, int, int]]] = deque(maxlen=18)
_banner: tuple[str, tuple[int, int, int]] = ("", (200, 200, 200))
_alarm = False
_dry_run = False
_backdrop: np.ndarray | None = None


def _make_backdrop() -> np.ndarray:
    """A dark vertical gradient used wherever the render shows no geometry.

    PyBullet's `--background_color_*` connect options only reach the GUI window's OpenGL
    clear colour. The offscreen TinyRenderer that `getCameraImage` uses in headless mode
    ignores them and clears to WHITE, which would hand us a bright frame in a demo whose
    whole look is a dark lab. Keying on the segmentation mask instead is exact - a pixel
    is background because nothing was drawn there, not because it happens to be pale.
    """
    top = np.array([b * 255 for b in (BG[2], BG[1], BG[0])], dtype=np.float32)
    bottom = top * 2.6 + 6.0
    ramp = np.linspace(0.0, 1.0, FRAME_H, dtype=np.float32)[:, None]
    rows = top[None, :] * (1.0 - ramp) + bottom[None, :] * ramp
    return np.repeat(rows[:, None, :], FRAME_W, axis=1).astype(np.uint8)


def _enable_ansi() -> None:
    """Windows terminals need virtual-terminal processing turned on before ANSI works."""
    if os.name == "nt":
        os.system("")  # noqa: S605 - the documented no-op that flips the console mode


# --------------------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------------------


class Recorder:
    """Writes captured frames to an MP4.

    Frame rate is fixed at `VIDEO_FPS` and frames arrive one per `TICKS_PER_FRAME` physics
    steps, so playback speed equals simulation speed no matter how long the render of any
    individual frame actually took.
    """

    def __init__(self, path: Path, fps: int, size: tuple[int, int]) -> None:
        self.path = path
        self.size = size
        self.frames = 0
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._writer = cv2.VideoWriter(str(path), fourcc, fps, size)
        if not self._writer.isOpened():
            raise RuntimeError(f"OpenCV could not open {path} for writing")

    def add(self, frame_bgr: np.ndarray) -> None:
        self._writer.write(frame_bgr)
        self.frames += 1

    def close(self) -> float:
        self._writer.release()
        return self.frames / float(VIDEO_FPS)


def _view_matrix() -> list[float]:
    """Camera for this instant, including the slow orbit."""
    seconds = _ticks / SIM_HZ
    return p.computeViewMatrixFromYawPitchRoll(
        cameraTargetPosition=CAM_TARGET,
        distance=CAM_DISTANCE,
        yaw=CAM_YAW + CAM_DRIFT_DEG_PER_S * seconds,
        pitch=CAM_PITCH,
        roll=0,
        upAxisIndex=2,
    )


def _grab_frame() -> np.ndarray:
    """One rendered frame, as BGR, with the narration composited on."""
    proj = p.computeProjectionMatrixFOV(
        fov=CAM_FOV, aspect=FRAME_W / FRAME_H, nearVal=0.05, farVal=30.0
    )
    renderer = p.ER_TINY_RENDERER if _headless else p.ER_BULLET_HARDWARE_OPENGL
    _, _, rgba, _, seg = p.getCameraImage(
        FRAME_W, FRAME_H,
        viewMatrix=_view_matrix(),
        projectionMatrix=proj,
        shadow=1 if SHADOWS else 0,
        lightDirection=LIGHT_DIRECTION,
        lightColor=LIGHT_COLOR,
        lightAmbientCoeff=0.42,     # enough fill that the arm reads as metal, not shadow
        lightDiffuseCoeff=0.95,
        lightSpecularCoeff=0.70,
        renderer=renderer,
    )
    rgb = np.reshape(np.asarray(rgba, dtype=np.uint8), (FRAME_H, FRAME_W, 4))[:, :, :3]
    frame = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    global _backdrop
    if _backdrop is None:
        _backdrop = _make_backdrop()
    mask = np.reshape(np.asarray(seg, dtype=np.int32), (FRAME_H, FRAME_W)) < 0
    frame[mask] = _backdrop[mask]
    return _compose(frame)


def _compose(frame: np.ndarray) -> np.ndarray:
    """Draw the narration panel, the phase banner and the framing onto a rendered frame.

    Layout is a two-column split: the terminal owns the left, the robot owns the right.
    The first cut painted translucent text straight over the arm, which made both harder
    to read than either would have been alone.
    """
    panel_w = int(FRAME_W * 0.54)

    # Left panel: near-opaque, so the terminal reads as a terminal.
    scrim = frame.copy()
    cv2.rectangle(scrim, (0, 0), (panel_w, FRAME_H), (10, 9, 8), -1)
    cv2.addWeighted(scrim, 0.88, frame, 0.12, 0, frame)
    cv2.line(frame, (panel_w, 0), (panel_w, FRAME_H), (54, 50, 44), 1)

    if _alarm:
        cv2.rectangle(frame, (0, 0), (FRAME_W - 1, FRAME_H - 1), (60, 60, 235), 7)

    # Phase banner, top of the panel, with a coloured rule under it.
    text, colour = _banner
    if text:
        text = to_ascii(text)
        cv2.putText(frame, text, (40, 62), cv2.FONT_HERSHEY_DUPLEX, 0.86, (0, 0, 0), 6,
                    cv2.LINE_AA)
        cv2.putText(frame, text, (40, 62), cv2.FONT_HERSHEY_DUPLEX, 0.86, colour, 2,
                    cv2.LINE_AA)
        cv2.line(frame, (40, 80), (panel_w - 40, 80), colour, 2)

    # Narration, bottom-anchored so new lines rise like a real terminal.
    line_h = 24
    y = FRAME_H - 46 - (len(_overlay) - 1) * line_h
    for line, line_colour in _overlay:
        cv2.putText(frame, to_ascii(line)[:64], (40, y), cv2.FONT_HERSHEY_PLAIN, 1.25,
                    line_colour, 1, cv2.LINE_AA)
        y += line_h

    # Wordmark, bottom right, over the robot half.
    cv2.putText(frame, "OPEN-MHS", (FRAME_W - 186, FRAME_H - 32),
                cv2.FONT_HERSHEY_DUPLEX, 0.68, (170, 170, 170), 1, cv2.LINE_AA)
    return frame


# --------------------------------------------------------------------------------------
# Simulation clock - the single source of time for physics, video and text
# --------------------------------------------------------------------------------------


def tick() -> None:
    """Advance physics one step, and capture a frame on every eighth."""
    global _ticks
    if _client is None:
        return
    p.stepSimulation()
    _ticks += 1
    if _recorder is not None and _ticks % TICKS_PER_FRAME == 0:
        _recorder.add(_grab_frame())


def hold(seconds: float) -> None:
    """Keep the simulation running without commanding anything.

    Used after the blocked command: the arm has to be visibly *not moving* while still
    being simulated, or a viewer cannot tell "refused" from "frozen".

    Counted in simulation steps rather than wall-clock, so the recorded duration is exact
    even when frame rendering is slower than real time.
    """
    if _fast:
        seconds = min(seconds, 0.2)
    for _ in range(int(seconds * SIM_HZ)):
        tick()
        if _recorder is None and not _fast and not _dry_run:
            time.sleep(1.0 / SIM_HZ)


def type_text(text: str, color_code: str, delay: float = 0.03) -> None:
    """Print one line character by character, the way a model streams tokens.

    The simulation keeps stepping between characters, so the arm never freezes while the
    terminal talks, and every character lands on a definite simulation tick. The finished
    line is also pushed to the video overlay.
    """
    delay *= TYPING_SPEED
    sys.stdout.write(color_code)
    # Fractional ticks are carried between characters. Truncating per character instead
    # loses a slice of every one of them, and the compounded error is what made the first
    # export land at 39.5 s against a 60 s brief.
    carry = 0.0
    for char in text:
        sys.stdout.write(char)
        sys.stdout.flush()
        if _fast:
            tick()
            continue
        carry += delay * SIM_HZ
        steps = int(carry)
        carry -= steps
        for _ in range(steps):
            tick()
            if _recorder is None and not _dry_run:
                time.sleep(1.0 / SIM_HZ)
    sys.stdout.write(RESET + "\n")
    sys.stdout.flush()

    if text.strip():
        _overlay.append((text, OVERLAY_BGR.get(color_code, DEFAULT_BGR)))


def banner(text: str, bgr: tuple[int, int, int], rgb_3d: tuple[float, float, float]) -> None:
    """Set the on-screen phase caption, in the video overlay and the live GUI window."""
    global _hud_id, _banner
    _banner = (text, bgr)
    if _client is None or _headless:
        return
    try:
        _hud_id = p.addUserDebugText(
            text, textPosition=[0, 0, 1.45], textColorRGB=list(rgb_3d), textSize=1.5,
            replaceItemUniqueId=_hud_id if _hud_id >= 0 else -1,
        )
    except p.error:  # pragma: no cover - GUI-only nicety
        pass


# --------------------------------------------------------------------------------------
# Scene
# --------------------------------------------------------------------------------------


def build_scene(headless: bool) -> int:
    """Bring up the dark lab and return the robot's body id."""
    global _client

    # The background colour is a CONNECTION option. There is no configureDebugVisualizer
    # flag for it - COV_ENABLE_RGB_BUFFER_PREVIEW only toggles a preview panel.
    options = (
        f"--background_color_red={BG[0]} "
        f"--background_color_green={BG[1]} "
        f"--background_color_blue={BG[2]}"
    )
    _client = p.connect(p.DIRECT if headless else p.GUI, options=options)
    p.setAdditionalSearchPath(pybullet_data.getDataPath())
    p.setGravity(0, 0, -9.81)
    p.setTimeStep(1.0 / SIM_HZ)

    _build_floor()
    robot = p.loadURDF("kuka_iiwa/model.urdf", [0, 0, 0], useFixedBase=True)
    _style_arm(robot, ARM_DARK)

    if not headless:
        # Strip every overlay: no side panels, no widgets, no preview thumbnails.
        for flag in (
            p.COV_ENABLE_GUI,
            p.COV_ENABLE_RGB_BUFFER_PREVIEW,
            p.COV_ENABLE_DEPTH_BUFFER_PREVIEW,
            p.COV_ENABLE_SEGMENTATION_MARK_PREVIEW,
        ):
            p.configureDebugVisualizer(flag, 0)
        p.configureDebugVisualizer(p.COV_ENABLE_SHADOWS, 1)
        p.configureDebugVisualizer(
            p.COV_ENABLE_RENDERING, 1, lightPosition=[-2.2, -1.4, 3.0]
        )
        p.resetDebugVisualizerCamera(
            cameraDistance=CAM_DISTANCE, cameraYaw=CAM_YAW, cameraPitch=CAM_PITCH,
            cameraTargetPosition=CAM_TARGET,
        )
    return robot


def _build_floor() -> None:
    """A dark metallic slab instead of the default checkerboard, plus two accent strips."""
    slab = p.createVisualShape(
        p.GEOM_BOX, halfExtents=[FLOOR_HALF_EXTENT, FLOOR_HALF_EXTENT, 0.02],
        rgbaColor=FLOOR_RGBA, specularColor=[0.35, 0.38, 0.42],
    )
    collision = p.createCollisionShape(
        p.GEOM_BOX, halfExtents=[FLOOR_HALF_EXTENT, FLOOR_HALF_EXTENT, 0.02]
    )
    p.createMultiBody(
        baseMass=0, baseCollisionShapeIndex=collision, baseVisualShapeIndex=slab,
        basePosition=[0, 0, -0.02],
    )

    # Two thin lit strips running past the base. They read as floor lighting and give the
    # eye something to track during the slow orbit.
    for offset in (-1.15, 1.15):
        strip = p.createVisualShape(
            p.GEOM_BOX, halfExtents=[STRIP_HALF_LENGTH, 0.012, 0.002],
            rgbaColor=ACCENT_CYAN,
            specularColor=[0, 0, 0],
        )
        p.createMultiBody(
            baseMass=0, baseVisualShapeIndex=strip, basePosition=[0, offset, 0.001]
        )


def _style_arm(robot: int, base_rgba: list[float]) -> None:
    """Repaint the KUKA so it reads as hardware rather than as a physics sample."""
    for link in range(-1, p.getNumJoints(robot)):
        rgba = ARM_ACCENT if link in (0, 6) else base_rgba
        try:
            p.changeVisualShape(robot, link, rgbaColor=rgba, specularColor=[0.4, 0.4, 0.45])
        except p.error:  # pragma: no cover - some links carry no visual shape
            pass


def set_alarm(on: bool) -> None:
    """Flip the whole shot into alarm state: red border, red arm."""
    global _alarm
    _alarm = on
    if _robot >= 0:
        _style_arm(_robot, ARM_ALARM if on else ARM_DARK)


def command_joint(robot: int, degrees: float, seconds: float) -> None:
    """Drive the base joint to an angle and let the simulation carry it there.

    Holds for `seconds`, then keeps stepping until the joint actually arrives, up to a cap.
    Without the second part the demo can announce a verified move while the arm is still
    travelling, which is precisely the claim this project exists to stop people making.
    """
    p.setJointMotorControl2(
        bodyUniqueId=robot,
        jointIndex=JOINT_INDEX,
        controlMode=p.POSITION_CONTROL,
        targetPosition=float(np.radians(degrees)),
        force=500.0,
        maxVelocity=1.2,
    )
    hold(seconds)

    for _ in range(int(SETTLE_CAP_S * SIM_HZ)):
        if abs(read_joint(robot) - degrees) <= SETTLE_TOLERANCE_DEG:
            break
        tick()


def read_joint(robot: int) -> float:
    """Measured angle of the base joint, in degrees."""
    return float(np.degrees(p.getJointState(robot, JOINT_INDEX)[0]))


def report_position(robot: int, commanded: float) -> None:
    """State what the feedback sensor actually reads, and whether it agrees."""
    actual = read_joint(robot)
    if abs(actual - commanded) <= SETTLE_TOLERANCE_DEG:
        type_text(f"open-mhs> verified against joint_0_actual: reads {actual:.1f} deg.",
                  GREEN, 0.02)
    else:
        type_text(
            f"open-mhs> joint_0_actual reads {actual:.1f} deg, still travelling toward "
            f"{commanded:.1f}.", YELLOW, 0.02,
        )


# --------------------------------------------------------------------------------------
# The safety check - the real one where possible
# --------------------------------------------------------------------------------------


def load_checker() -> tuple[object, bool]:
    """Return a callable that evaluates a write, and whether it is the real Open-MHS one.

    Importing the repo keeps the demo honest: the -32001 shown on screen is produced by the
    same code path that guards real hardware, not by a print statement shaped like one.
    """
    sys.path.insert(0, str(REPO_ROOT))
    tag_doc = json.loads(TAG_PATH.read_text(encoding="utf-8"))
    try:
        from server import safety
        from server.errors import SafetyLimitViolation
        from server.models import CapabilityTag

        tag = CapabilityTag.model_validate(tag_doc)
        actuator = tag.actuator_map[TARGET]
        limit = tag.limit_map[TARGET]

        def check(degrees: float) -> dict | None:
            try:
                safety.check_write(actuator, limit, degrees, device_id=tag.device_id)
                return None
            except SafetyLimitViolation as exc:
                return exc.to_rpc()

        return check, True
    except Exception:  # noqa: BLE001 - the demo must run from a copied folder too
        limit = next(item for item in tag_doc["safety_limits"] if item["target"] == TARGET)

        def check(degrees: float) -> dict | None:
            if limit["min"] <= degrees <= limit["max"]:
                return None
            return {
                "code": -32001,
                "message": (
                    f"{TARGET}: {degrees} {limit['unit']} is outside the inclusive bound "
                    f"[{limit['min']}, {limit['max']}] {limit['unit']}"
                ),
                "data": {
                    "device_id": DEVICE_ID, "target": TARGET, "attempted": degrees,
                    "min": limit["min"], "max": limit["max"], "unit": limit["unit"],
                    "enforcement": limit["enforcement"],
                    "on_violation": limit["on_violation"],
                    "rationale": limit.get("rationale"),
                },
            }

        return check, False


def wire(payload: dict) -> None:
    """Print a JSON-RPC envelope the way it goes over the wire."""
    for line in json.dumps(payload, indent=2).splitlines():
        type_text(f"  {line}", GREY, delay=0.002)


# --------------------------------------------------------------------------------------
# Choreography
# --------------------------------------------------------------------------------------


def rule(colour: str = GREY) -> None:
    type_text("─" * 74, colour, delay=0.0)


def phase_1_discovery(real_checker: bool) -> None:
    banner("DISCOVERY", (230, 220, 120), (0.5, 0.85, 0.9))
    rule(CYAN)
    type_text("  OPEN-MHS  ·  the safety envelope travels with the hardware", BOLD + CYAN, 0.02)
    rule(CYAN)
    print()
    hold(0.6)

    type_text("claude> Looking for hardware on the Open-MHS registry...", BLUE, 0.025)
    hold(0.4)
    type_text(f"        Found: {DEVICE_ID}  (KUKA iiwa, simulated)", GREEN, 0.02)
    type_text("        Readable:  joint_0_actual  [deg]", GREEN, 0.02)
    type_text("        Writable:  joint_0         [deg]", GREEN, 0.02)
    type_text("          allowed range: -90.0 to 90.0 deg, inclusive", BOLD + GREEN, 0.025)
    type_text("          on_violation:  reject", BOLD + GREEN, 0.025)
    type_text("          why: past +/-90 the arm sweeps the operator's side of the bench",
              GREEN, 0.015)
    print()
    type_text("        The robot itself can reach +/-170. The tag says 90. The tag wins.",
              DIM + CYAN, 0.02)
    if not real_checker:
        type_text("        [demo running standalone: using a local copy of the bounds check]",
                  DIM + YELLOW, 0.01)
    print()
    hold(1.0)


def phase_2_safe_move(robot: int) -> None:
    banner("SAFE MOVE   joint_0 -> 45 deg", (120, 230, 140), (0.2, 0.9, 0.3))
    type_text("claude> 45 degrees is inside the envelope. Sending it.", BLUE, 0.025)
    wire({"jsonrpc": "2.0", "id": 1, "method": "mhs.write",
          "params": {"device_id": DEVICE_ID, "target": TARGET, "value": 45.0}})
    hold(0.3)
    type_text("open-mhs> ACCEPTED. joint_0 commanded to 45.0 deg.", GREEN, 0.02)
    command_joint(robot, 45.0, 2.0)
    report_position(robot, 45.0)
    print()
    hold(0.8)


def phase_3_hallucination() -> None:
    banner("REQUEST   joint_0 -> 300 deg", (90, 200, 250), (0.95, 0.75, 0.1))
    type_text("claude> Now rotating the base a full turn to reach the far tray...",
              YELLOW, 0.03)
    type_text("        joint_0 -> 300 degrees.", BOLD + YELLOW, 0.04)
    wire({"jsonrpc": "2.0", "id": 2, "method": "mhs.write",
          "params": {"device_id": DEVICE_ID, "target": TARGET, "value": 300.0}})
    hold(0.6)


def phase_4_block(robot: int, error: dict) -> float:
    """Show the refusal. The arm must not move, and must visibly still be simulated."""
    before = read_joint(robot)
    banner("BLOCKED BY OPEN-MHS   ·   -32001", (70, 70, 255), (1.0, 0.15, 0.15))
    set_alarm(True)
    print()
    rule(RED_BOLD)
    type_text("  ██  BLOCKED BY OPEN-MHS  ·  SAFETY LIMIT VIOLATION  ·  -32001  ██",
              RED_BOLD, 0.012)
    rule(RED_BOLD)
    wire({"jsonrpc": "2.0", "id": 2, "error": error})
    print()

    data = error.get("data", {})
    type_text(f"  Attempted:      {data.get('attempted')} deg", RED_BOLD, 0.02)
    type_text(f"  Allowed range:  {data.get('min')} to {data.get('max')} deg, inclusive",
              RED_BOLD, 0.02)
    type_text("  Bytes sent to the hardware:   0", BOLD + RED_BOLD, 0.03)
    type_text("  The arm did not move. It was never asked to.", BOLD + RED_BOLD, 0.03)
    rule(RED_BOLD)
    print()

    # Simulation keeps running, arm stays put. This is the proof shot.
    hold(2.0)
    after = read_joint(robot)
    type_text(
        f"open-mhs> joint_0_actual before: {before:6.1f} deg   after: {after:6.1f} deg   "
        f"(moved {abs(after - before):.2f} deg)", GREY, 0.012,
    )
    print()
    set_alarm(False)
    return abs(after - before)


def phase_5_correction(robot: int) -> None:
    banner("SELF-CORRECTED   joint_0 -> 0 deg", (120, 230, 140), (0.2, 0.9, 0.3))
    type_text("claude> Understood - 300 is outside the declared envelope, and the refusal",
              BLUE, 0.022)
    type_text("        told me the real bound. Returning to 0 degrees.", BLUE, 0.022)
    wire({"jsonrpc": "2.0", "id": 3, "method": "mhs.write",
          "params": {"device_id": DEVICE_ID, "target": TARGET, "value": 0.0}})
    type_text("open-mhs> ACCEPTED. joint_0 commanded to 0.0 deg.", GREEN, 0.02)
    command_joint(robot, 0.0, 2.5)
    report_position(robot, 0.0)
    print()
    hold(0.6)
    banner("OPEN-MHS", (230, 220, 120), (0.5, 0.85, 0.9))
    rule(CYAN)
    type_text("  The limit lived in the hardware's own capability tag - not in the prompt,",
              BOLD + CYAN, 0.02)
    type_text("  not in the model, not in a code review. github.com/Abenor-Labs/Open-MHS",
              BOLD + CYAN, 0.02)
    rule(CYAN)
    hold(2.5)


# --------------------------------------------------------------------------------------


def main() -> int:
    global _fast, _headless, _recorder, _robot, _dry_run

    parser = argparse.ArgumentParser(description="Open-MHS cinematic PyBullet demo.")
    parser.add_argument("--headless", action="store_true",
                        help="no GUI window; frames are rendered offscreen (slower)")
    parser.add_argument("--fast", action="store_true",
                        help="skip typing delays and shorten holds")
    parser.add_argument("--speed", type=float, default=None, metavar="X",
                        help=f"scale typing delays (default {TYPING_SPEED}; lower is faster)")
    parser.add_argument("--no-video", action="store_true",
                        help="run the sequence without writing an MP4")
    parser.add_argument("--dry-run", action="store_true",
                        help="no rendering, no sleeping: reports the duration the exported "
                             "video WILL have, in a couple of seconds")
    parser.add_argument("--out", type=Path, default=VIDEO_PATH, help="output MP4 path")
    parser.add_argument("--resolution", default=f"{FRAME_W}x{FRAME_H}", metavar="WxH",
                        help="frame size, e.g. 1920x1080 or 640x360 for a quick check")
    parser.add_argument("--no-shadows", action="store_true",
                        help="drop shadows: ~3x faster offscreen export, flatter image")
    args = parser.parse_args()

    _fast = args.fast
    _headless = args.headless
    _dry_run = args.dry_run
    if _dry_run:
        args.no_video = True
        args.headless = True
    try:
        width, height = (int(v) for v in args.resolution.lower().split("x"))
    except ValueError:
        parser.error(f"--resolution must look like 1280x720, got {args.resolution!r}")
    globals()["FRAME_W"], globals()["FRAME_H"] = width, height
    globals()["SHADOWS"] = not args.no_shadows
    if args.speed is not None:
        globals()["TYPING_SPEED"] = max(args.speed, 0.0)

    _enable_ansi()
    check, real_checker = load_checker()
    _robot = build_scene(args.headless)

    if not args.no_video:
        _recorder = Recorder(args.out, VIDEO_FPS, (FRAME_W, FRAME_H))

    started = time.monotonic()
    try:
        phase_1_discovery(real_checker)
        phase_2_safe_move(_robot)
        phase_3_hallucination()

        error = check(300.0)
        if error is None:  # pragma: no cover - would mean the envelope is broken
            type_text("DEMO ABORTED: 300 deg was NOT rejected. The capability tag or the "
                      "safety check is wrong, and this demo will not pretend otherwise.",
                      RED_BOLD, 0.0)
            return 2

        moved = phase_4_block(_robot, error)
        phase_5_correction(_robot)

        if moved > 1.0:  # pragma: no cover - the arm must not have drifted
            type_text(f"WARNING: the arm moved {moved:.2f} deg during the blocked command.",
                      RED_BOLD, 0.0)
            return 3
        return 0
    except KeyboardInterrupt:
        type_text("\nInterrupted.", DIM + CYAN, 0.0)
        return 130
    finally:
        wall = time.monotonic() - started
        sim_seconds = _ticks / SIM_HZ
        if _dry_run:
            print(f"{CYAN}  dry run: the exported video would be {sim_seconds:.1f}s "
                  f"({int(_ticks / TICKS_PER_FRAME)} frames at {VIDEO_FPS} fps){RESET}")
        if _recorder is not None:
            duration = _recorder.close()
            print(f"{GREEN}  video: {args.out}{RESET}")
            print(f"{GREY}  {_recorder.frames} frames · {duration:.1f}s at {VIDEO_FPS} fps "
                  f"· {FRAME_W}x{FRAME_H} · rendered in {wall:.1f}s wall clock{RESET}")
        if _client is not None:
            p.disconnect()


if __name__ == "__main__":
    sys.exit(main())
