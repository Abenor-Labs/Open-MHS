#!/usr/bin/env python
"""Build a block tower through Open-MHS, with verification at every step.

    python examples/robosuite_demo/stack_blocks.py --order yellow,blue,green,red

Written after a stacking run collapsed at the fourth block. That failure was not a
dynamics problem -- it was geometric, and the geometry was wrong for one specific reason
that this module exists to avoid.

THE RULE THAT MATTERS
--------------------

**Vision verifies. The TCP frame commands.**

The failed run targeted each placement at the *vision* xy of the block beneath it. If the
camera reports ``v = t + b`` for a true position ``t`` and bias ``b``, then commanding the
tool to ``v_below`` lands the new block at ``t_below + b``: the bias becomes drift, and the
drift compounds once per tier. Measured at ~14 mm per tier over three tiers, which put the
centre of mass 21.25 mm off a 21 mm half-width support. It tipped.

So placements here are commanded in the tool frame: whatever tcp xy released tier N also
releases tier N+1. The camera is never in the command path, so its bias -- whatever its
magnitude, and whether it is a measurement artefact or a real offset -- cannot accumulate.
Only the base->tier-2 joint carries a single bias hop, which does not compound.

The camera is still read constantly, but only ever to answer "did that work?".

WHAT ELSE THE FAILED RUN TAUGHT
-------------------------------

* ``grasping`` is a contact check, not a hold check. Verify the grip AFTER the lift.
* Long single-axis moves shed a marginal grip. Step in <= ``MAX_STEP_M`` increments.
* A write verifies against ``sensor.accuracy``, which is +/-20 mm here. That is not
  accurate enough to stack 42 mm cubes. Re-command until within ``CONVERGE_M``.
* The carry offset differs per grasp (measured 41 / 37 / 43 mm). Measure it, never assume.
* A buried block's ``_z`` is corrupted by the occlusion that proves the stack is intact.
  Use ``_pixels`` for structural truth instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ARM = "panda-arm-01"
CAMERA = "cv-camera-01"

#: JSON-RPC error code for "commanded, but the hardware is not there". See server/errors.py.
STATE_DESYNC = -32003

CUBE_M = 0.042              # examples/robosuite_demo/multi_cube_env.py: BLOCK_HALF_SIZE * 2
TABLE_TOP_Z = 0.800         # examples/robosuite_demo/cell.py

#: Largest single move on any axis WHILE CARRYING.
#:
#: Every grasp measures a carry offset of ~40 mm, but the geometry says 30: at the grasp
#: height of 0.830 a seated cube has its underside on the table at 0.800. The extra 10 mm
#: is the block sliding down through the fingers during the lift, leaving it pinched by
#: its top edge. That purchase is weak and sheds under acceleration. The grasp cannot go
#: lower -- 0.830 IS the envelope floor -- so the only lever left is to demand less of it.
#: 40 mm steps shed roughly half of all carries; 20 mm is the conservative setting.
MAX_STEP_M = 0.02

#: Empty-handed there is nothing to shed, so approach moves go in bigger strides. 50 mm
#: lands ~6 mm out at the tag's 800 ms settle, which is irrelevant for a waypoint nobody
#: stops at -- and `_step_axis` converges the final position properly regardless.
EMPTY_STEP_M = 0.05

#: A grasped cube should hang with its underside 30 mm below the tool: at the 0.830 grasp
#: height a seated block rests on the table at 0.800. Anything materially over that is the
#: block having slipped down through the fingers to be pinched by its top edge. Measured:
#: 37 mm carried fine, 47 mm was visibly dangling by a corner and toppled the tower on
#: landing. Over this threshold, put it down and grasp again -- a bad grip fails over open
#: table, which is cheap, instead of over a standing stack, which is not.
GRIP_MAX_OFFSET_M = 0.040

#: ...and anything BELOW this is not a good grip, it is a bad measurement. The tool cannot
#: hold a block whose underside is less than a cube-height beneath it, so a small offset
#: means the silhouette was contaminated -- the gripper itself entering the blob, most
#: likely -- and the centroid rode up. Measured once at 8.1 mm, which would have set the
#: release 27 mm too low and driven the held block straight through the tower.
#: A one-sided gate accepts garbage as eagerly as it accepts a good grasp.
GRIP_MIN_OFFSET_M = 0.028
GRIP_ATTEMPTS = 4

#: Convergence tolerance for a commanded pose. Tighter than the device's declared
#: +/-0.02 m feedback tolerance, which will happily verify a 16 mm error -- but no tighter
#: than the servo can actually deliver.
#:
#: MEASURED: a 20 mm step lands within 2.2 mm empty-handed at the tag's 800 ms settle.
#: Carrying a block it settles nearer 4 mm, and 3 mm was refusing legitimate moves that
#: had simply reached their steady state (tcp_y stuck at 4.2 mm out, four attempts, no
#: improvement). 5 mm covers the loaded case and is still 4x tighter than the device
#: tolerance. Placement precision is not the constraint here: a 5 mm error on a 42 mm
#: cube is well inside the 21 mm half-width that decides whether a tower stands.
CONVERGE_M = 0.005
#: Yaw tolerance in DEGREES. The tag declares 2.0 deg accuracy on
#: tcp_yaw_actual; 1.5 keeps the grasp square without chasing noise.
CONVERGE_DEG = 1.5
CONVERGE_TRIES = 4

#: Vertical gap between a carried block's underside and the surface it is placed on.
#: Deliberately near-zero: the block is SET DOWN, not dropped. A 5 mm release still has
#: the block falling onto a 42 mm target, and if it is hanging even slightly askew it
#: lands on an edge and levers the stack over -- which is how the last tower came apart.
PLACE_CLEARANCE_M = 0.001

#: Height to carry a block at, above the top of the tallest thing on the table.
CARRY_OVER_M = 0.03

#: Occlusion is checked RELATIVELY, not against a constant, so there is no threshold here
#: any more. An absolute one was tried at 400 px and it destroyed a working tower: derived
#: from a THREE-tier stack (149 / 312 / 789 bottom-to-top), it then called a correct
#: FOUR-tier stack broken, because tier 3 sits higher and shows more of its own sides even
#: with a block on top -- red read 431 px while genuinely buried. The routine e-stopped,
#: retried, lifted the top block back off and dropped it.
#:
#: A buried block simply shows fewer pixels than the block resting on it. That holds at
#: any height and any camera angle: 312 < 789 in the three-tier case, 431 < 793 in the
#: four-tier one. A constant tuned on one tower is not a structural test.

GREEN, YELLOW, RED, GREY, BOLD, RESET = (
    "\033[92m", "\033[93m", "\033[91m", "\033[90m", "\033[1m", "\033[0m")


def say(text: str, colour: str = "") -> None:
    print(f"{colour}{text}{RESET}", flush=True)


class Refused(RuntimeError):
    """The middleware refused a command. Carries the bound it reported."""

    def __init__(self, message: str, code: int, data: dict) -> None:
        super().__init__(message)
        self.code, self.data = code, data


class Unstable(RuntimeError):
    """The world is not what the plan assumed. Re-observe before doing anything else."""


class Middleware:
    """Open-MHS JSON-RPC. The only channel to the hardware."""

    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url.rstrip("/"), token

    def _post(self, method: str, params: dict) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": method, "params": params}).encode()
        request = urllib.request.Request(
            f"{self.url}/rpc", data=body,
            headers={"content-type": "application/json",
                     "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(request, timeout=180) as response:
            reply = json.load(response)
        if "error" in reply:
            error = reply["error"]
            raise Refused(error.get("message", ""), error.get("code", 0),
                          error.get("data", {}))
        return reply["result"]

    def read(self, device: str, target: str):
        return self._post("mhs.read", {"device_id": device, "target": target})["value"]

    def write(self, device: str, target: str, value, confirm: bool = False) -> dict:
        return self._post("mhs.write", {"device_id": device, "target": target,
                                        "value": value, "confirm": confirm})

    def discover(self) -> list[dict]:
        return self._post("mhs.discover", {})["devices"]

    def estop(self, device: str) -> dict:
        return self._post("mhs.emergency_stop", {"device_id": device})


# --------------------------------------------------------------------------- motion


class Arm:
    """Pose commands that actually converge, and lifts that do not shed the payload."""

    def __init__(self, mhs: Middleware) -> None:
        self.mhs = mhs
        self.bounds = self._read_bounds()

    def _read_bounds(self) -> dict[str, tuple[float, float]]:
        """Take the envelope from the capability tag. Never hardcode a safety bound.

        Also resolves the CONDITIONAL floor. `tcp_z` declares a tighter minimum while the
        gripper is closed, because a held block hangs below the tool and the height that
        is safe empty would drive the payload through the table. A plan built on the
        unconditional floor computes set-down heights the middleware will clamp -- which
        is not a refusal the agent can act on, it is a target it can never reach.
        """
        tag = next(d["capability_tag"] for d in self.mhs.discover()
                   if d["device_id"] == ARM)
        bounds: dict[str, tuple[float, float]] = {}
        self.held_floor: float | None = None
        for limit in tag["safety_limits"]:
            if limit.get("min") is None or limit.get("max") is None:
                continue
            bounds[limit["target"]] = (float(limit["min"]), float(limit["max"]))
            if limit["target"] == "tcp_z":
                # Strictest conditional minimum. Assumed to be the payload case, which is
                # the conservative reading: taking the strictest can only refuse earlier.
                mins = [float(c["min"]) for c in (limit.get("conditions") or [])
                        if c.get("min") is not None]
                self.held_floor = max(mins) if mins else float(limit["min"])
        return bounds

    def floor(self, *, carrying: bool) -> float:
        """Lowest tcp_z that will actually execute, given what the gripper is holding."""
        if carrying and self.held_floor is not None:
            return self.held_floor
        return self.bounds["tcp_z"][0]

    def reachable(self, x: float, y: float) -> bool:
        low_x, high_x = self.bounds["tcp_x"]
        low_y, high_y = self.bounds["tcp_y"]
        return low_x <= x <= high_x and low_y <= y <= high_y

    def pose(self) -> tuple[float, float, float]:
        return (self.mhs.read(ARM, "tcp_x_actual"),
                self.mhs.read(ARM, "tcp_y_actual"),
                self.mhs.read(ARM, "tcp_z_actual"))

    def align_yaw(self, degrees: float | None) -> None:
        """Square the jaws to a block. A no-op if the camera could not measure the angle.

        Blocks spawn at random yaw and the wrist does not, so an unaligned grasp closes on
        two CORNERS rather than two faces. Measured on the live cell: blue at an 11 degree
        mismatch was carried successfully every time; green at 44.7 degrees -- as close to
        corner-on as a square allows -- was dropped three times running. This is the
        variable behind the carry-offset scatter, not noise.
        """
        if degrees is None or degrees < 0:
            return                      # -1 means the camera declined to guess. Respect it.
        self._converge("tcp_yaw", float(degrees) % 90.0, tol=CONVERGE_DEG)

    def _converge(self, axis: str, value: float, tol: float | None = None) -> float:
        """Command one axis until the feedback agrees to CONVERGE_M, not to the tag's
        far looser declared tolerance. Re-issuing is what fixed a 16 mm residual."""
        feedback = f"{axis}_actual"
        tol = CONVERGE_M if tol is None else tol
        for attempt in range(CONVERGE_TRIES):
            try:
                self.mhs.write(ARM, axis, round(value, 4))
            except Refused as exc:
                # -32003 means "commanded, but it has not got there yet" -- which is the
                # exact condition this loop exists to resolve. A 45 degree wrist turn does
                # not complete inside one 800 ms settle. Re-command; do NOT swallow -32001,
                # which means a bound was violated and nothing was transmitted at all.
                if exc.code != STATE_DESYNC:
                    raise
                say(f"      {axis} still travelling ({exc.data.get('observed')}) - "
                    f"re-commanding", GREY)
            actual = self.mhs.read(ARM, feedback)
            if abs(actual - value) <= tol:
                return actual
            say(f"      {axis} settled {abs(actual - value):.4f} out "
                f"(attempt {attempt + 1}) - re-commanding", GREY)
        raise Unstable(f"{axis} would not converge on {value:.4f}; last read {actual:.4f}")

    def _nudge(self, axis: str, value: float) -> None:
        """Fire one intermediate waypoint. Not arriving is not an error.

        A waypoint is somewhere the tool passes THROUGH, and the next command supersedes
        it before the servo ever settles, so `-32003` here means "still travelling" and is
        the expected case rather than a fault. Only the final position, in `_converge`,
        has to actually arrive.

        `-32001` still propagates: a bound violation means nothing was transmitted, and
        swallowing that would be routing around a safety limit.
        """
        try:
            self.mhs.write(ARM, axis, round(value, 4))
        except Refused as exc:
            if exc.code != STATE_DESYNC:
                raise

    def lower_onto(self, z: float, *, carrying: str) -> None:
        """Descend to a release height, accepting that contact may stop us short.

        Setting a block down MEANS stopping early: the payload lands on the surface and
        the arm cannot travel the last millimetre without pressing it into the tower.
        Demanding convergence here declares a successful placement a failure -- measured
        at 0.9200 against a 0.9159 target, 4.1 mm high, because red was already resting on
        green. The routine then e-stopped and retried a placement that had worked.

        So the last step is fired, not converged. Landing short is the goal.
        """
        self._step_axis("tcp_z", z, MAX_STEP_M, converge=False)
        self.confirm_holding(carrying)

    def _step_axis(self, axis: str, target: float, step: float,
                   *, converge: bool = True) -> None:
        """Walk one axis to the target in increments no larger than `step`.

        Intermediate waypoints are fired and forgotten -- the servo keeps chasing, and a
        few mm of residual mid-approach costs nothing. Only the final position runs the
        convergence loop. Measured: at the tag's 800 ms settle a 20 mm step lands within
        2.2 mm, a 50 mm step within 8 mm, and a 200 mm step does not arrive at all
        (-32003). Checking every waypoint therefore buys precision nobody needs and pays
        a read plus a possible re-command for it.
        """
        current = self.mhs.read(ARM, f"{axis}_actual")
        span = target - current
        steps = max(1, int(abs(span) / step + 0.999))
        for n in range(1, steps):
            self._nudge(axis, current + span * n / steps)
        if converge:
            self._converge(axis, target)
        else:
            self._nudge(axis, target)

    def move_z(self, z: float, *, carrying: str | None = None) -> None:
        self._step_axis("tcp_z", z, MAX_STEP_M if carrying else EMPTY_STEP_M)
        if carrying:
            self.confirm_holding(carrying)

    def move_xy(self, x: float, y: float, *, carrying: str | None = None) -> None:
        """Interleave x and y so the tool tracks a diagonal rather than an L."""
        start_x, start_y, _ = self.pose()
        step = MAX_STEP_M if carrying else EMPTY_STEP_M
        span = max(abs(x - start_x), abs(y - start_y))
        steps = max(1, int(span / step + 0.999))
        for n in range(1, steps):
            self._nudge("tcp_x", start_x + (x - start_x) * n / steps)
            self._nudge("tcp_y", start_y + (y - start_y) * n / steps)
            if carrying:
                self.confirm_holding(carrying)
        self._converge("tcp_x", x)
        self._converge("tcp_y", y)
        if carrying:
            self.confirm_holding(carrying)

    def grip(self, state: str) -> None:
        """gripper_state is gated on human approval; the operator authorised this task."""
        self.mhs.write(ARM, "gripper_state", state, confirm=True)

    def yaw(self) -> float:
        return self.mhs.read(ARM, "tcp_yaw_actual")

    def holding(self) -> str:
        return self.mhs.read(ARM, "grasping")

    def confirm_holding(self, block: str) -> None:
        held = self.holding()
        if held != block:
            raise Unstable(f"lost {block} in transit - grasping reads {held!r}")


# --------------------------------------------------------------------------- perception


class Camera:
    """Reads that answer 'did that work?'. Never 'where should I go?'."""

    def __init__(self, mhs: Middleware) -> None:
        self.mhs = mhs
        self.blocks = self._discover_blocks()

    def _discover_blocks(self) -> list[str]:
        """Derive block names from the tag rather than hardcoding the scene."""
        tag = next(d["capability_tag"] for d in self.mhs.discover()
                   if d["device_id"] == CAMERA)
        return sorted(s["id"][:-2] for s in tag["sensors"] if s["id"].endswith("_x"))

    def observe(self, block: str) -> dict:
        return {
            "name": block,
            "x": self.mhs.read(CAMERA, f"{block}_x"),
            "y": self.mhs.read(CAMERA, f"{block}_y"),
            "z": self.mhs.read(CAMERA, f"{block}_z"),
            "yaw": self.mhs.read(CAMERA, f"{block}_yaw"),
            "source": self.mhs.read(CAMERA, f"{block}_source"),
            "pixels": self.mhs.read(CAMERA, f"{block}_pixels"),
        }

    def world(self) -> dict[str, dict]:
        """Full re-observation. The only legitimate way to recover from a surprise."""
        return {block: self.observe(block) for block in self.blocks}


def tier_of(observation: dict) -> int:
    """Which tier a block sits on, from its reported top face. 1 == on the table.

    Only trustworthy for an unoccluded block: once something is stacked on top, the blob
    centroid falls to a partial side face and the reported z drops. Guard with pixels.
    """
    height = observation["z"] - (TABLE_TOP_Z + CUBE_M)
    return 1 + int(round(height / CUBE_M))


# --------------------------------------------------------------------------- the routine


class Stacker:
    def __init__(self, mhs: Middleware) -> None:
        self.mhs = mhs
        self.arm = Arm(mhs)
        self.camera = Camera(mhs)
        #: (placed, tcp_xy, surface_z, tower_yaw) from the last verified tier, so a retry
        #: extends the tower instead of grasping through it.
        self._progress = None

    # -- pick -------------------------------------------------------------------

    def pick(self, block: str, scene: dict[str, dict]) -> float:
        """Grasp one block off the table. Returns the measured carry offset in metres.

        Carry offset is `tcp_z - block_underside`. It is measured, not assumed, because
        it varied 37-43 mm across three grasps in the failed run: each closure seats the
        cube slightly differently, and a single hardcoded constant mis-places two in three.
        """
        target = scene[block]
        if target["source"] != "vision":
            raise Unstable(f"{block} reports {target['source']} - refusing to grasp on a "
                           "degraded estimate")
        if not self.arm.reachable(target["x"], target["y"]):
            raise Unstable(f"{block} at ({target['x']:.3f}, {target['y']:.3f}) is outside "
                           f"the declared envelope {self.arm.bounds['tcp_x']} - unreachable")

        clear_z = self._clearance_height(scene)
        floor_z = self.arm.bounds["tcp_z"][0]
        # Verify the hold at height, not at the grasp: a contact check at the bottom
        # reported success on grips that then slipped. High enough to clear the table so
        # the camera sees the underside.
        probe_z = floor_z + 3 * MAX_STEP_M
        say(f"  pick {block} at ({target['x']:.4f}, {target['y']:.4f})", BOLD)

        # Where the block truly is, in the frame that matters. On a retry we know exactly
        # where we put it down, which beats a camera reading that may be occluded by the
        # tower standing between the block and the lens.
        grasp_xy = (target["x"], target["y"])

        self.arm.grip("open")
        self.arm.move_z(clear_z)

        # Yaw comes from the scene captured with the arm PARKED CLEAR, never from a
        # reading taken while hovering over the block. Measured: red read 439 px at
        # (-0.0764, +0.0106) with the arm away, and 104 px at (-0.0889, +0.0293) with the
        # arm overhead -- 28 mm of error, more than a cube's half-width, which lands the
        # jaws on the block's top face instead of around it. `source` does not catch this:
        # it only flips to ground_truth below 20 px, so a badly occluded blob still calls
        # itself vision.
        grasp_yaw = target["yaw"]

        for attempt in range(1, GRIP_ATTEMPTS + 1):
            self.arm.move_xy(*grasp_xy)
            # Square the jaws to THIS block before descending. Done at height, so a wrist
            # that has to swing 45 degrees does not sweep through its neighbours.
            self.arm.align_yaw(grasp_yaw)
            self.arm.move_z(floor_z)                   # the floor IS the grasp height
            self.arm.grip("closed")
            self.arm.move_z(probe_z, carrying=block)

            _, _, tcp_z = self.arm.pose()
            carry = tcp_z - (self.camera.observe(block)["z"] - CUBE_M)

            if GRIP_MIN_OFFSET_M <= carry <= GRIP_MAX_OFFSET_M:
                say(f"    holding {block}; carry offset {carry * 1000:.1f} mm", GREEN)
                return carry

            # Either hanging by its top edge, or the reading is not believable. Set it
            # down and try again -- over open table, where failing costs nothing, rather
            # than over the tower.
            why = ("not believable - silhouette contaminated"
                   if carry < GRIP_MIN_OFFSET_M else "too shallow")
            say(f"    grip {why} ({carry * 1000:.1f} mm, want "
                f"{GRIP_MIN_OFFSET_M * 1000:.0f}-{GRIP_MAX_OFFSET_M * 1000:.0f} mm) - "
                f"setting down and re-gripping ({attempt}/{GRIP_ATTEMPTS})", YELLOW)
            if attempt == GRIP_ATTEMPTS:
                raise Unstable(
                    f"{block}: {GRIP_ATTEMPTS} grasps all failed the offset band (last "
                    f"{carry * 1000:.1f} mm); refusing to carry it over the stack")
            # Do not descend using the offset we just refused to believe.
            safe_carry = max(carry, GRIP_MIN_OFFSET_M)
            # Never below the payload floor: the middleware would clamp it, and a target
            # that cannot be reached is not a set-down, it is a stall. The block is
            # released from the floor instead, dropping the difference onto open table.
            release_z = self._clamp_z(max(TABLE_TOP_Z + PLACE_CLEARANCE_M + safe_carry,
                                          self.arm.floor(carrying=True)))
            self.arm.lower_onto(release_z, carrying=block)
            self.arm.grip("open")
            # It is now exactly where the tool released it, and at the yaw the tool was
            # holding it at -- both known without asking a camera that cannot see past
            # the gripper. The tcp frame again: what we did is better evidence than what
            # we can observe from a bad angle.
            grasp_xy = self.arm.pose()[:2]
            grasp_yaw = self.arm.yaw()
            self.arm.move_z(probe_z)

    # -- place ------------------------------------------------------------------

    def place(self, block: str, tcp_xy: tuple[float, float], surface_z: float,
              carry: float, scene: dict[str, dict],
              tower_yaw: float | None = None) -> tuple[float, float]:
        """Release a held block onto `surface_z` at `tcp_xy`. Returns the tcp xy used.

        `tcp_xy` is a TOOL-frame coordinate, deliberately. Feeding a camera reading in
        here is the bug that collapsed the previous run.
        """
        release_z = max(surface_z + PLACE_CLEARANCE_M + carry,
                        self.arm.floor(carrying=True))
        low_z, high_z = self.arm.bounds["tcp_z"]
        if not low_z <= release_z <= high_z:
            raise Unstable(f"release height {release_z:.3f} is outside {low_z}..{high_z}; "
                           "the tower is taller than the declared envelope")

        # Transit height is derived from the MEASURED carry offset, so the payload clears
        # the tallest obstacle by a known margin instead of a guessed one. Less vertical
        # travel than a generic clearance, and every metre not travelled is a metre the
        # marginal grip cannot fail over.
        tallest = max(obs["z"] for obs in scene.values())
        transit_z = self._clamp_z(max(release_z, tallest + PLACE_CLEARANCE_M + carry))
        self.arm.move_z(transit_z, carrying=block)
        # Turn the held block to the tower's yaw before setting it down. The grasp was
        # squared to the BLOCK; the placement is squared to the TOWER, so every tier lands
        # face-aligned with the one beneath and the support overlap is a full 42x42 mm
        # square rather than the intersection of two rotated ones.
        self.arm.align_yaw(tower_yaw)
        self.arm.confirm_holding(block)
        self.arm.move_xy(*tcp_xy, carrying=block)
        self.arm.lower_onto(release_z, carrying=block)
        self.arm.grip("open")
        self.arm.move_z(self._clamp_z(release_z + 3 * MAX_STEP_M))
        return tcp_xy

    def _clamp_z(self, z: float) -> float:
        low, high = self.arm.bounds["tcp_z"]
        return min(max(z, low), high)

    def _clearance_height(self, scene: dict[str, dict]) -> float:
        """Carry height: above the tallest thing on the table, clamped to the envelope."""
        tallest = max(obs["z"] for obs in scene.values())
        return self._clamp_z(tallest + CARRY_OVER_M + CUBE_M)

    # -- verify -----------------------------------------------------------------

    def verify_tier(self, block: str, expected_tier: int, buried: list[str]) -> dict:
        """Confirm a placement structurally, using the signal occlusion does not corrupt.

        A buried block's z is unreliable precisely BECAUSE it is buried. Its pixel count
        is not: a block that should be covered but reads a large blob has nothing on top
        of it, which is the collapse signature.
        """
        self.arm.move_xy(0.0, self.arm.bounds["tcp_y"][0] * 0.6)   # unmask the scene
        observation = self.camera.observe(block)

        actual_tier = tier_of(observation)
        if actual_tier != expected_tier:
            raise Unstable(f"{block} reads tier {actual_tier} (z={observation['z']:.4f}), "
                           f"expected tier {expected_tier}")

        for covered in buried:
            below = self.camera.observe(covered)
            if below["pixels"] >= observation["pixels"]:
                raise Unstable(
                    f"{covered} shows {below['pixels']} px, not fewer than {block} on top "
                    f"of it at {observation['pixels']} px; the stack has come apart")
        say(f"    verified: {block} on tier {expected_tier}, "
            f"{len(buried)} block(s) correctly occluded", GREEN)
        return observation

    # -- the whole job ----------------------------------------------------------

    def build(self, order: list[str]) -> None:
        """Stack `order` bottom-first. Re-observes and re-plans on any surprise.

        Resumable. A retry after a failure must NOT assume a clean table: tiers that are
        already standing would be grasped at the table-height grasp point, which drives
        the gripper straight through the tower it is meant to be extending. So progress is
        remembered, checked against the world, and resumed from -- and only rebuilt from
        scratch when the tower is actually gone.
        """
        scene = self.camera.world()

        unreachable = [b for b in order
                       if not self.arm.reachable(scene[b]["x"], scene[b]["y"])]
        if unreachable:
            raise Unstable(f"outside the arm's envelope before we start: {unreachable}. "
                           "These cannot be recovered without resetting the cell.")

        resumed = self._resume(order, scene)
        if resumed is not None:
            placed, tcp_xy, surface_z, tower_yaw = resumed
            say(f"\nresuming: {' -> '.join(placed)} already standing, "
                f"top face {surface_z:.4f}", GREEN)
        else:
            base = order[0]
            # The base is wherever it already sits. Its vision xy is the one and only
            # camera reading that reaches the command path -- a single bias hop, which does
            # not compound because every later tier reuses the tool coordinate below.
            tcp_xy = (scene[base]["x"], scene[base]["y"])
            surface_z = scene[base]["z"]
            # The base sits at some random yaw; every tier above it is turned to match, so
            # the tower is square all the way up.
            base_yaw = scene[base]["yaw"]
            tower_yaw = base_yaw if base_yaw is not None and base_yaw >= 0 else None
            placed = [base]
            say(f"\nbase {base} at ({tcp_xy[0]:.4f}, {tcp_xy[1]:.4f}), "
                f"top face {surface_z:.4f}, tower yaw {tower_yaw}")

        for tier, block in enumerate(order[len(placed):], start=len(placed) + 1):
            say(f"\n--- tier {tier}: {block} ---", BOLD)
            scene = self.camera.world()
            carry = self.pick(block, scene)
            tcp_xy = self.place(block, tcp_xy, surface_z, carry, scene, tower_yaw)
            # Everything already placed is now underneath this block, including the one
            # it was just set on. `placed` has not had `block` appended yet.
            observation = self.verify_tier(block, tier, buried=placed)
            surface_z = observation["z"]
            placed.append(block)
            self._progress = (list(placed), tcp_xy, surface_z, tower_yaw)

        self.park()
        say(f"\n{'=' * 70}")
        say(f"  tower standing: {' -> '.join(placed)}", GREEN + BOLD)

    def _resume(self, order: list[str], scene: dict[str, dict]):
        """Progress from an earlier attempt, if the tower it describes is still standing.

        Trusts remembered state over re-derivation: a buried block's vision z is corrupted
        by the very occlusion that proves it is buried, so reconstructing the tower from
        the camera alone is exactly the reading that cannot be trusted. What CAN be
        trusted is the top block, which is unoccluded by definition.
        """
        if not self._progress:
            return None
        placed, tcp_xy, surface_z, tower_yaw = self._progress
        if len(placed) < 2 or placed != order[:len(placed)]:
            return None

        top = scene[placed[-1]]
        expected = TABLE_TOP_Z + CUBE_M * len(placed)
        if abs(top["z"] - expected) > CUBE_M / 2:
            say(f"  previous tower is gone ({placed[-1]} reads {top['z']:.4f}, "
                f"expected ~{expected:.4f}) - rebuilding from scratch", YELLOW)
            self._progress = None
            return None
        return placed, tcp_xy, surface_z, tower_yaw

    def park(self) -> None:
        """Retreat clear of the tower so the camera has an unobstructed view."""
        self.arm.move_z(self.arm.bounds["tcp_z"][1] * 0.92)
        self.arm.move_xy(self.arm.bounds["tcp_x"][0] * 0.7, 0.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Stack blocks through Open-MHS.")
    parser.add_argument("--url", default=os.getenv("OPEN_MHS_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.getenv("OPEN_MHS_AUTH_TOKEN", ""))
    parser.add_argument("--order", default="yellow_block,blue_block,green_block,red_block",
                        help="bottom-first, comma separated")
    parser.add_argument("--retries", type=int, default=1,
                        help="full re-observe-and-rebuild attempts after a collapse")
    parser.add_argument("--standing", type=int, default=0, metavar="N",
                        help="the first N blocks of --order are ALREADY stacked; extend "
                             "that tower instead of building one. Verified against the "
                             "scene before anything moves")
    args = parser.parse_args()
    if not args.token:
        print("set OPEN_MHS_AUTH_TOKEN", file=sys.stderr)
        return 2

    mhs = Middleware(args.url, args.token)
    order = [name if name.endswith("_block") else f"{name}_block"
             for name in args.order.split(",")]

    try:
        stacker = Stacker(mhs)
    except (urllib.error.URLError, OSError) as exc:
        say(f"cannot reach the middleware: {exc}", RED)
        return 2

    if args.standing >= 2:
        # Seed the resume state a fresh process cannot know. `build` re-verifies it
        # against the world and rebuilds from scratch if the tower is not actually there,
        # so a wrong --standing is caught rather than acted on.
        scene = stacker.camera.world()
        placed = order[:args.standing]
        top = scene[placed[-1]]
        yaw = top["yaw"] if top["yaw"] is not None and top["yaw"] >= 0 else None
        # One bias hop from vision, unavoidable across a process restart: the tool
        # coordinate that released this tier died with the previous run. A SINGLE hop is
        # stable -- it is compounding, tier on tier, that tips a tower.
        stacker._progress = (list(placed), (top["x"], top["y"]), top["z"], yaw)
        say(f"resuming on a standing tower: {' -> '.join(placed)}", GREEN)

    for attempt in range(args.retries + 1):
        try:
            stacker.build(order)
            return 0
        except Unstable as exc:
            say(f"\n  UNSTABLE: {exc}", RED + BOLD)
            try:
                stacker.arm.grip("open")
                mhs.estop(ARM)
                say("  emergency stop: arm driven to its declared safe pose", YELLOW)
            except Refused as stop_failure:
                say(f"  emergency stop refused: {stop_failure}", RED)
            if attempt >= args.retries:
                say("  out of retries. Re-read the table before commanding anything.", RED)
                return 1
            say(f"  re-observing and rebuilding (attempt {attempt + 2})", YELLOW)
        except Refused as exc:
            # A refusal is the middleware working. Report the bound it gave us.
            say(f"\n  REFUSED ({exc.code}): {exc}", RED + BOLD)
            if exc.data:
                say(f"  bound: {exc.data}", GREY)
            return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
