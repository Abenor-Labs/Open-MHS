"""Driver compliance: five checks, no hardware, well under a second.

Run it alone when you want an instant verdict on a driver:

    pytest tests/test_driver_compliance.py

The subject is the real `PandaArmDevice` driving the real `panda_arm.mhs` tag through the
real `/rpc` dispatcher. Nothing in the safety path is stubbed. The only substitution is the
workcell itself: `PandaArmDevice` reaches MuJoCo through a `cell` object it only ever calls
`.command()` and `.read()` on, so a dict-backed stand-in exercises every layer above it
without robosuite, a GL context, or a simulator thread.

The checks:

    1. STATE       mhs.read returns the response schema, and its unit and datatype agree
                   with what the capability tag declares.
    2. EXECUTION   an in-bounds mhs.write is accepted, reaches the hardware, and the
                   driver's coordinates actually move.
    3. BOUNCER     an out-of-bounds mhs.write on a REJECT axis is refused with -32001 AND
                   the driver's coordinates do not move AND nothing is transmitted at all.
    4. CLAMP       an out-of-bounds write on a CLAMP axis succeeds, transmits the nearest
                   legal value rather than the requested one, and says so in `warning`.
    5. CONDITIONAL the tcp_z floor rises from 0.82 to 0.86 while the gripper is closed,
                   and the clamp target moves with it.

Check 3 is the one that matters most. A refusal that still emitted bytes would pass a
return-value assertion and break a machine, so it asserts zero transmissions rather than
merely asserting that an error came back.

Check 4 asserts the inverse and is just as important: a clamp MUST transmit, and must
transmit the corrected value. The failure mode here is a caller that reads `accepted:
true` and believes the hardware went where it asked.

NOT covered here, by design: settle-time behaviour. The tag declares
`settle_time_ms` on the pose axes and this file injects a no-op sleeper to stay
fast, so timing is out of scope. `tests/test_safety.py` is the exhaustive suite; this is
the smoke test.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from open_mhs.server.errors import SAFETY_LIMIT_VIOLATION
from tests.conftest import rpc_call, rpc_error, rpc_result

DEVICE_ID = "panda-arm-01"
#: examples/robosuite_demo/cell.py. The clamp floor must stay above this or clamping
#: would quietly correct a command INTO the table rather than away from it.
TABLE_TOP_Z = 0.800
DEMO = Path(__file__).resolve().parent.parent / "examples" / "robosuite_demo"
TAG_PATH = DEMO / "panda_arm.mhs"


def _load_panda_arm() -> Any:
    """Import the driver by path. `examples/` is not a package, and the runner puts the
    demo directory on sys.path at startup; a test must not rely on that having happened."""
    spec = importlib.util.spec_from_file_location(
        "_compliance_panda_arm", DEMO / "panda_arm.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PandaArmDevice


PandaArmDevice = _load_panda_arm()


async def _no_sleep(_seconds: float) -> None:
    """Never wait on simulated hardware. Keeps the whole file under a second."""
    return None


class FakeCell:
    """Stand-in for the robosuite `Workcell`, duck-typed to what the driver actually uses.

    `PandaArmDevice` talks to the cell through exactly two methods, so this needs no
    MuJoCo and no thread. `channels` IS the driver's internal coordinate state: the
    bouncer check asserts against it directly.

    An unknown channel raises `KeyError` here, matching the real `Workcell.read`, which
    `PandaArmTransport` turns into a `TransportError`.
    """

    #: Home pose. tcp_z starts at the tag's declared default for the axis.
    HOME = {
        "tcp_x_actual": 0.0,
        "tcp_y_actual": 0.0,
        "tcp_z_actual": 1.05,
        "gripper_actual": "open",
        "grasping": "nothing",
    }

    def __init__(self) -> None:
        self.channels: dict[str, Any] = dict(self.HOME)
        #: Every command that reached the cell. Empty is the assertion that matters.
        self.commands: list[tuple[str, Any]] = []

    def command(self, channel: str, value: Any) -> None:
        self.commands.append((channel, value))
        if channel in ("tcp_x", "tcp_y", "tcp_z"):
            self.channels[f"{channel}_actual"] = float(value)
        elif channel == "gripper_state":
            self.channels["gripper_actual"] = str(value)

    def read(self, channel: str) -> Any:
        return self.channels[channel]


@pytest.fixture
def tag() -> dict[str, Any]:
    """The shipped capability tag, from disk. Never inline a tag in a test."""
    return json.loads(TAG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def cell() -> FakeCell:
    return FakeCell()


@pytest.fixture
def panda(cell: FakeCell) -> Any:
    """The real driver, the real tag, a fake cell."""
    return PandaArmDevice(cell, sleep=_no_sleep)


def _limit(tag: dict[str, Any], target: str) -> dict[str, Any]:
    return next(limit for limit in tag["safety_limits"] if limit["target"] == target)


def _sensor(tag: dict[str, Any], sensor_id: str) -> dict[str, Any]:
    return next(sensor for sensor in tag["sensors"] if sensor["id"] == sensor_id)


# --------------------------------------------------------------------------------------
# 1. State
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_returns_the_schema_the_tag_declares(client_factory, panda, cell, tag):
    """mhs.read answers with the full response schema, consistent with the tag."""
    client = await client_factory(panda)

    result = rpc_result(await rpc_call(
        client, "mhs.read", {"device_id": DEVICE_ID, "target": "tcp_z_actual"},
    ))

    assert set(result) == {
        "device_id", "target", "value", "unit", "datatype", "timestamp",
    }
    assert result["device_id"] == DEVICE_ID
    assert result["target"] == "tcp_z_actual"

    # The response must agree with the capability tag, not merely with itself.
    declared = _sensor(tag, "tcp_z_actual")
    assert result["unit"] == declared["unit"]
    assert result["datatype"] == declared["datatype"]

    assert result["value"] == pytest.approx(cell.channels["tcp_z_actual"])
    assert isinstance(result["timestamp"], float)

    # A read is not a write.
    assert cell.commands == []


# --------------------------------------------------------------------------------------
# 2. Execution
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_in_bounds_is_accepted_and_moves_the_driver(
    client_factory, panda, cell, tag
):
    """An in-bounds command is accepted, verified, and actually reaches the hardware."""
    client = await client_factory(panda)
    limit = _limit(tag, "tcp_z")
    midpoint = round((limit["min"] + limit["max"]) / 2, 4)
    assert cell.channels["tcp_z_actual"] != midpoint, "pick a target the arm is not already at"

    result = rpc_result(await rpc_call(
        client, "mhs.write",
        {"device_id": DEVICE_ID, "target": "tcp_z", "value": midpoint},
    ))

    assert result["accepted"] is True
    assert result["clamped"] is False
    assert result["commanded"] == pytest.approx(midpoint)
    assert result["unit"] == limit["unit"]

    # Closed-loop: the driver checked the feedback sensor, it did not just assume.
    assert result["verified"] is True

    # The command reached the hardware, exactly once, unmodified.
    assert cell.commands == [("tcp_z", midpoint)]
    assert cell.channels["tcp_z_actual"] == pytest.approx(midpoint)


# --------------------------------------------------------------------------------------
# 3. Bouncer -- the one that matters
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_outside_a_reject_bound_is_refused_and_nothing_moves(
    client_factory, panda, cell, tag
):
    """tcp_x is a REJECT axis. Out of bounds means refused, and the arm does not twitch.

    Asserts three separate things, because any one alone is a weaker claim than it looks:
      * the caller is told, with the real bound;
      * the driver's coordinates are byte-identical to before;
      * nothing was transmitted at all.
    """
    client = await client_factory(panda)
    limit = _limit(tag, "tcp_x")
    assert limit["on_violation"] == "reject", "this test is about the reject path"
    outside = limit["max"] + 0.5

    before = dict(cell.channels)

    error = rpc_error(await rpc_call(
        client, "mhs.write",
        {"device_id": DEVICE_ID, "target": "tcp_x", "value": outside},
    ))

    # Told, and told the truth: the refusal carries the bound the tag declares.
    assert error["code"] == SAFETY_LIMIT_VIOLATION      # -32001
    assert error["data"]["min"] == pytest.approx(limit["min"])
    assert error["data"]["max"] == pytest.approx(limit["max"])
    assert error["data"]["target"] == "tcp_x"

    # Did not move a single millimetre -- nor any other axis.
    assert cell.channels == before

    # And nothing was transmitted. A refusal that still emitted bytes is a safety failure
    # that an error-code assertion on its own would happily pass.
    assert cell.commands == []
    assert panda._transport.writes == []


# --------------------------------------------------------------------------------------
# 4. Clamp
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_below_the_floor_is_clamped_transmitted_and_flagged(
    client_factory, panda, cell, tag
):
    """tcp_z = 0.50 is under the table. Clamped to the floor, transmitted, and flagged.

    The inverse of the bouncer, and the assertion that matters has the same shape: what
    actually reached the hardware. A clamp that returned success without transmitting
    would be as wrong as a refusal that transmitted.
    """
    client = await client_factory(panda)
    limit = _limit(tag, "tcp_z")
    assert limit["on_violation"] == "clamp", "this test is about the clamp path"
    floor = limit["min"]
    assert floor > TABLE_TOP_Z, "a clamp floor at or below the table would be unsafe"

    result = rpc_result(await rpc_call(
        client, "mhs.write", {"device_id": DEVICE_ID, "target": "tcp_z", "value": 0.50},
    ))

    # Success -- but success that is honest about what happened.
    assert result["accepted"] is True
    assert result["clamped"] is True
    assert result["requested"] == pytest.approx(0.50)
    assert result["commanded"] == pytest.approx(floor)
    assert result["clamp_details"]["bound"] == "min"

    # The one field a caller cannot miss.
    assert "CLAMPED" in result["warning"]

    # The SAFE value reached the hardware: not the requested one, and not nothing.
    assert cell.commands == [("tcp_z", floor)]
    assert cell.channels["tcp_z_actual"] == pytest.approx(floor)


@pytest.mark.asyncio
async def test_in_bounds_write_carries_no_warning(client_factory, panda):
    """`warning` appears only when something actually was clamped."""
    client = await client_factory(panda)
    result = rpc_result(await rpc_call(
        client, "mhs.write", {"device_id": DEVICE_ID, "target": "tcp_z", "value": 1.0},
    ))
    assert result["clamped"] is False
    assert "warning" not in result


# --------------------------------------------------------------------------------------
# 5. Conditional envelope
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_floor_rises_while_the_gripper_holds_a_payload(
    client_factory, panda, cell, tag
):
    """The same command clamps to a different floor depending on the gripper's state.

    This is the whole point of a conditional bound: a height that is legal with an empty
    gripper is illegal while holding a block, because the payload hangs below the tool. A
    static envelope cannot express that without being wrong half the time -- too low when
    loaded, needlessly high when empty.
    """
    client = await client_factory(panda)
    limit = _limit(tag, "tcp_z")
    empty_floor = limit["min"]
    held_floor = limit["conditions"][0]["min"]
    assert held_floor > empty_floor
    between = round((empty_floor + held_floor) / 2, 4)

    # --- gripper open: accepted untouched ---
    result = rpc_result(await rpc_call(
        client, "mhs.write", {"device_id": DEVICE_ID, "target": "tcp_z", "value": between},
    ))
    assert result["clamped"] is False
    assert result["commanded"] == pytest.approx(between)

    # --- close the gripper: the floor moves under the same command ---
    rpc_result(await rpc_call(
        client, "mhs.write",
        {"device_id": DEVICE_ID, "target": "gripper_state", "value": "closed",
         "confirm": True},
    ))
    assert cell.channels["gripper_actual"] == "closed"

    result = rpc_result(await rpc_call(
        client, "mhs.write", {"device_id": DEVICE_ID, "target": "tcp_z", "value": between},
    ))
    assert result["clamped"] is True, "the payload floor did not apply"
    assert result["commanded"] == pytest.approx(held_floor)
    assert cell.channels["tcp_z_actual"] == pytest.approx(held_floor)

    # The payload names the condition that tightened it, and what it observed.
    condition = result["clamp_details"]["condition"]
    assert condition["when_target"] == "gripper_actual"
    assert condition["equals"] == "closed"
    assert condition["observed"] == "closed"
    assert result["clamp_details"]["base_min"] == pytest.approx(empty_floor)


@pytest.mark.asyncio
async def test_condition_reads_the_sensor_not_the_commanded_value(
    client_factory, panda, cell, tag
):
    """The condition consults the MEASURED gripper channel, not the commanded one.

    If it named the actuator, a close that was commanded but did not physically happen
    would leave the middleware applying the payload floor on a false premise. Naming the
    sensor means the envelope follows the world.
    """
    limit = _limit(tag, "tcp_z")
    when = limit["conditions"][0]["when_target"]
    assert when in {s["id"] for s in tag["sensors"]}
    assert when not in {a["id"] for a in tag["actuators"]}

    # Drive the measured channel directly, bypassing the actuator entirely.
    client = await client_factory(panda)
    cell.channels["gripper_actual"] = "closed"

    result = rpc_result(await rpc_call(
        client, "mhs.write",
        {"device_id": DEVICE_ID, "target": "tcp_z", "value": limit["min"]},
    ))
    assert result["clamped"] is True
    assert result["commanded"] == pytest.approx(limit["conditions"][0]["min"])
