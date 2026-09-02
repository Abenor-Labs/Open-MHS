"""Serial transport and the G-code arm.

Same principle as the rest of the suite: the driver, the protocol encoding and the safety
checks are all real, and only the port underneath is fake. `FakeSerial` records every byte,
so "the write was refused and nothing went down the wire" stays an assertable claim on a
transport that, in production, is a physical UART.

One test runs against genuine pyserial over its `loop://` URL, and skips when pyserial is
not installed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from open_mhs.drivers.serial_robotic_arm import SerialRoboticArm
from open_mhs.drivers.serial_transport import SerialTransport
from open_mhs.drivers.transport import TransportError
from open_mhs.server.errors import HardwareExecutionError, InvalidParams, SafetyLimitViolation
from tests.conftest import EXAMPLES, _no_sleep, load_tag


class FakeSerial:
    """Records what was written; replays scripted responses. Mimics pyserial's `Serial`."""

    def __init__(self, responses: list[str] | None = None, *, fail_writes: bool = False) -> None:
        self.written: list[bytes] = []
        self.responses = list(responses or [])
        self.is_open = True
        self.flushes = 0
        self.closed = False
        self.fail_writes = fail_writes

    @property
    def lines(self) -> list[str]:
        """Everything written, as stripped text — the actual G-code that went out."""
        return [b.decode("ascii").strip() for b in self.written]

    def write(self, data: bytes) -> int:
        if self.fail_writes:
            raise OSError("device disconnected")
        self.written.append(data)
        return len(data)

    def readline(self) -> bytes:
        if not self.responses:
            return b""  # a read timeout, exactly as pyserial reports one
        return (self.responses.pop(0) + "\n").encode("ascii")

    def reset_input_buffer(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        self.is_open = False
        self.closed = True


def make_transport(responses: list[str] | None = None, **kwargs: Any) -> SerialTransport:
    fake = FakeSerial(responses)
    transport = SerialTransport("COM-TEST", connection_factory=lambda: fake, **kwargs)
    transport.fake = fake  # type: ignore[attr-defined]
    return transport


@pytest.fixture
def serial_tag() -> dict[str, Any]:
    return load_tag(EXAMPLES / "serial_arm.mhs")


def make_arm(responses: list[str] | None = None, **kwargs: Any) -> SerialRoboticArm:
    transport = make_transport(responses, **kwargs)
    return SerialRoboticArm(
        load_tag(EXAMPLES / "serial_arm.mhs"), transport, sleep=_no_sleep
    )


# --------------------------------------------------------------------------------------
# Transport mechanics
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_port_opens_lazily_and_closes_cleanly() -> None:
    transport = make_transport(["ok"])
    assert transport.is_open is False

    await transport.write_line("M115")
    assert transport.is_open is True

    await transport.close()
    assert transport.is_open is False
    assert transport.fake.closed is True


@pytest.mark.asyncio
async def test_open_is_idempotent() -> None:
    transport = make_transport()
    await transport.open()
    conn = transport._conn
    await transport.open()
    assert transport._conn is conn


@pytest.mark.asyncio
async def test_lines_are_terminated_and_encoded() -> None:
    transport = make_transport(["ok"])
    await transport.command("G1 X10.000 F1800")
    assert transport.fake.written == [b"G1 X10.000 F1800\n"]


@pytest.mark.asyncio
async def test_a_non_ack_reply_is_a_transport_error() -> None:
    """A firmware refusal must not read as success upstream."""
    transport = make_transport(["error:Bad G-code"])
    with pytest.raises(TransportError, match="device rejected"):
        await transport.command("G1 X10")


@pytest.mark.asyncio
async def test_a_silent_port_times_out_rather_than_hanging() -> None:
    transport = make_transport([])  # readline returns b"" like a pyserial timeout
    with pytest.raises(TransportError, match="no response within"):
        await transport.command("M114")


@pytest.mark.asyncio
async def test_expect_ack_none_skips_the_handshake() -> None:
    transport = make_transport([], expect_ack=None)
    await transport.command("G1 X10")
    assert transport.fake.lines == ["G1 X10"]


@pytest.mark.asyncio
async def test_a_write_failure_becomes_a_transport_error() -> None:
    fake = FakeSerial(fail_writes=True)
    transport = SerialTransport("COM-TEST", connection_factory=lambda: fake)
    with pytest.raises(TransportError, match="write failed"):
        await transport.write_line("M114")


@pytest.mark.asyncio
async def test_reading_an_unmapped_sensor_says_so_rather_than_inventing_a_value() -> None:
    transport = make_transport(["ok"], query_map={"joint_1_actual": "M114"})
    with pytest.raises(TransportError, match="no query command is mapped"):
        await transport.acquire("humidity")


@pytest.mark.asyncio
async def test_acquire_flushes_stale_input_before_querying() -> None:
    """Without a flush, the answer to the previous command is read as this one's."""
    transport = make_transport(["X:1.00 Y:2.00"], query_map={"joint_1_actual": "M114"})
    assert await transport.acquire("joint_1_actual") == "X:1.00 Y:2.00"
    assert transport.fake.flushes == 1


@pytest.mark.asyncio
async def test_transmit_rejects_a_non_string_payload() -> None:
    """The device's encode() owns protocol strings; the transport will not guess one."""
    transport = make_transport(["ok"])
    with pytest.raises(TransportError, match="must return"):
        await transport.transmit("joint_1", 45.0)


# --------------------------------------------------------------------------------------
# G-code encoding
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_joint_write_becomes_a_g1_move(serial_tag) -> None:
    arm = make_arm(["ok", "X:45.000 Y:0.000 Z:0.00 E:0.00"])
    result = await arm.write("joint_1", 45.0)

    assert arm.transport.fake.lines[0] == "G1 X45.000 F1800"
    assert result["verified"] is True
    assert result["observed"] == 45.0


@pytest.mark.asyncio
async def test_the_second_axis_maps_to_y() -> None:
    arm = make_arm(["ok", "X:0.000 Y:-30.000 Z:0.00 E:0.00"])
    await arm.write("joint_2", -30.0)
    assert arm.transport.fake.lines[0] == "G1 Y-30.000 F1800"


@pytest.mark.asyncio
async def test_feedrate_is_derived_from_the_declared_max_rate() -> None:
    """30 deg/s is the tightest limit in the tag, so the firmware is asked for 1800 deg/min."""
    arm = make_arm(["ok", "X:1.000 Y:0.000"])
    assert arm._feedrate == 1800.0


@pytest.mark.asyncio
async def test_gripper_states_map_to_tool_codes() -> None:
    arm = make_arm(["ok"])
    await arm.write("gripper", "closed", confirmed=True)
    assert arm.transport.fake.lines == ["M3"]


@pytest.mark.asyncio
async def test_position_is_parsed_out_of_a_shared_m114_reply() -> None:
    """One reply, two sensors. Each must pick its own axis out of it."""
    arm = make_arm(["X:12.500 Y:-7.250 Z:0.00 E:0.00 Count X:1000 Y:-580"])
    assert await arm.read("joint_1_actual") == 12.5

    arm2 = make_arm(["X:12.500 Y:-7.250 Z:0.00 E:0.00"])
    assert await arm2.read("joint_2_actual") == -7.25


@pytest.mark.asyncio
async def test_temperature_is_parsed_out_of_an_m105_reply() -> None:
    arm = make_arm(["ok T:31.4 /0.0 B:0.0 /0.0"])
    assert await arm.read("motor_temp") == 31.4


@pytest.mark.asyncio
async def test_an_unparseable_reply_is_an_error_not_a_guess() -> None:
    arm = make_arm(["garbage from a noisy line"])
    with pytest.raises(HardwareExecutionError, match="no X axis in position report"):
        await arm.read("joint_1_actual")


# --------------------------------------------------------------------------------------
# The safety envelope holds over a real transport
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [-91.0, 200.0])
@pytest.mark.asyncio
async def test_an_out_of_bounds_move_puts_no_bytes_on_the_wire(value) -> None:
    """The point of the whole project, on a transport that reaches physical hardware."""
    arm = make_arm(["ok"])
    with pytest.raises(SafetyLimitViolation):
        await arm.write("joint_1", value)
    assert arm.transport.fake.written == []


@pytest.mark.parametrize("value", [-90.0, 90.0])
@pytest.mark.asyncio
async def test_moves_exactly_at_the_bound_are_still_sent(value) -> None:
    arm = make_arm(["ok", f"X:{value:.3f} Y:0.000"])
    await arm.write("joint_1", value)
    assert arm.transport.fake.lines[0] == f"G1 X{value:.3f} F1800"


@pytest.mark.asyncio
async def test_an_unconfirmed_gripper_command_sends_nothing() -> None:
    arm = make_arm(["ok"])
    with pytest.raises(InvalidParams, match="human confirmation"):
        await arm.write("gripper", "closed")
    assert arm.transport.fake.written == []


@pytest.mark.asyncio
async def test_a_stuck_axis_over_serial_is_a_state_desync() -> None:
    """Firmware acknowledged the move, the axis did not arrive. Not a success."""
    from open_mhs.server.errors import StateDesync

    arm = make_arm(["ok", "X:0.000 Y:0.000 Z:0.00 E:0.00"])
    with pytest.raises(StateDesync) as exc:
        await arm.write("joint_1", 45.0)
    assert exc.value.data["commanded"] == 45.0
    assert exc.value.data["observed"] == 0.0


@pytest.mark.asyncio
async def test_emergency_stop_sends_m112_before_the_safe_state() -> None:
    arm = make_arm(["ok", "ok", "ok"])
    result = await arm.emergency_stop()

    assert result["firmware_halt_sent"] is True
    assert arm.transport.fake.lines[0] == "M112"
    assert "G1 X0.000 F1800" in arm.transport.fake.lines
    assert "M5" in arm.transport.fake.lines


# --------------------------------------------------------------------------------------
# The tag itself
# --------------------------------------------------------------------------------------


def test_the_serial_arm_tag_validates_against_the_schema(serial_tag) -> None:
    from jsonschema import Draft202012Validator

    from open_mhs.server.models import CapabilityTag
    from tests.conftest import REPO_ROOT

    schema = json.loads(
        (REPO_ROOT / "schema" / "capability_schema.json").read_text(encoding="utf-8")
    )
    assert list(Draft202012Validator(schema).iter_errors(serial_tag)) == []
    assert CapabilityTag.model_validate(serial_tag).device_id == "gcode-arm-01"


def test_the_serial_tag_claims_software_enforcement_not_firmware(serial_tag) -> None:
    """Marlin accepts `G1 X500` without complaint, so claiming firmware enforcement would lie."""
    from open_mhs.server.models import CapabilityTag

    tag = CapabilityTag.model_validate(serial_tag)
    assert tag.limit_map["joint_1"].enforcement == "software"


# --------------------------------------------------------------------------------------
# Genuine pyserial, no hardware
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_over_real_pyserial_loopback() -> None:
    """Proves the transport drives actual pyserial, not just our fake of it.

    `loop://` is pyserial's in-memory loopback: whatever is written comes back on read.
    """
    serial = pytest.importorskip("serial", reason="pyserial not installed")

    transport = SerialTransport(
        "loop://",
        expect_ack=None,
        query_map={"echo": "M114"},
        connection_factory=lambda: serial.serial_for_url("loop://", timeout=1),
    )
    async with transport:
        await transport.write_line("G1 X42.000 F1800")
        assert await transport.read_line() == "G1 X42.000 F1800"
        assert await transport.acquire("echo") == "M114"
    assert transport.is_open is False
