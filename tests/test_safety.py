"""The safety envelope. The most important file in the suite.

Every rejection test asserts **two** things: that the caller got the right error, and that
the transport recorded zero transmissions. A rejected write that still emitted bytes is a
safety failure a return-value assertion would happily pass.

Both enforcement points are covered: the driver called directly, and the same limit reached
through the RPC dispatcher.
"""

from __future__ import annotations

import json

import pytest

from server.errors import (
    HARDWARE_EXECUTION_ERROR,
    INVALID_PARAMS,
    SAFETY_LIMIT_VIOLATION,
    STATE_DESYNC,
    HardwareExecutionError,
    InvalidParams,
    SafetyLimitViolation,
    StateDesync,
)
from tests.conftest import AUTH_HEADERS, TEST_TOKEN, rpc_call, rpc_error, rpc_result


# --------------------------------------------------------------------------------------
# Bounds are inclusive
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target, value",
    [("joint_1", -90.0), ("joint_1", 90.0), ("joint_2", -135.0), ("joint_2", 135.0)],
)
@pytest.mark.asyncio
async def test_writes_exactly_at_a_bound_are_accepted(arm_device, target, value) -> None:
    """Off-by-one on an inclusive bound is the likeliest safety bug in this codebase."""
    result = await arm_device.write(target, value)
    assert result["written"] == value
    assert arm_device.transport.writes == [(target, value)]


@pytest.mark.parametrize(
    "target, value, bound",
    [
        ("joint_1", -90.001, "min"),
        ("joint_1", 90.001, "max"),
        ("joint_2", -135.5, "min"),
        ("joint_2", 135.5, "max"),
    ],
)
@pytest.mark.asyncio
async def test_writes_just_outside_a_bound_are_rejected_with_zero_transmissions(
    arm_device, target, value, bound
) -> None:
    before = arm_device.transport.snapshot()
    with pytest.raises(SafetyLimitViolation) as exc:
        await arm_device.write(target, value)

    assert exc.value.code == SAFETY_LIMIT_VIOLATION
    assert exc.value.data["attempted"] == value
    assert bound in exc.value.data
    # The two assertions that matter: nothing was sent, nothing moved.
    assert arm_device.transport.writes == []
    assert arm_device.transport.snapshot() == before


@pytest.mark.asyncio
async def test_violation_data_carries_everything_needed_to_correct(arm_device) -> None:
    """An agent must be able to retry correctly without a second round trip."""
    with pytest.raises(SafetyLimitViolation) as exc:
        await arm_device.write("joint_1", 200.0)
    data = exc.value.data
    assert data["min"] == -90.0 and data["max"] == 90.0
    assert data["unit"] == "deg"
    assert data["enforcement"] == "firmware"
    assert data["rationale"]


# --------------------------------------------------------------------------------------
# The same limits, reached through the server (enforcement point 2)
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_bounds_write_over_rpc_is_32001_and_transmits_nothing(
    rpc, arm_device
) -> None:
    before = arm_device.transport.snapshot()
    error = rpc_error(
        await rpc("mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 150.0})
    )
    assert error["code"] == SAFETY_LIMIT_VIOLATION
    assert error["data"]["attempted"] == 150.0
    assert error["data"]["max"] == 90.0
    assert arm_device.transport.writes == []
    assert arm_device.transport.snapshot() == before


@pytest.mark.asyncio
async def test_in_bounds_write_over_rpc_reaches_the_transport(rpc, arm_device) -> None:
    rpc_result(await rpc("mhs.write", {"device_id": "arm-01", "target": "joint_2", "value": -60.0}))
    assert arm_device.transport.writes == [("joint_2", -60.0)]
    assert arm_device.transport.state["joint_2_actual"] == -60.0


# --------------------------------------------------------------------------------------
# Discrete limits
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_state_the_hardware_supports_but_policy_forbids_is_32001(
    rpc, gripper_device
) -> None:
    """'vent' is in enum_values and absent from allowed_values: a real safety violation."""
    error = rpc_error(
        await rpc("mhs.write", {"device_id": "gripper-01", "target": "gripper", "value": "vent"})
    )
    assert error["code"] == SAFETY_LIMIT_VIOLATION
    assert error["data"]["allowed_values"] == ["open", "closed"]
    assert gripper_device.transport.writes == []


@pytest.mark.asyncio
async def test_a_state_the_hardware_cannot_accept_at_all_is_32602(rpc, gripper_device) -> None:
    """'ajar' is not in enum_values: never a candidate for the envelope, so it is a params error."""
    error = rpc_error(
        await rpc("mhs.write", {"device_id": "gripper-01", "target": "gripper", "value": "ajar"})
    )
    assert error["code"] == INVALID_PARAMS
    assert gripper_device.transport.writes == []


@pytest.mark.asyncio
async def test_allowed_discrete_value_is_accepted(gripper_device) -> None:
    await gripper_device.write("gripper", "closed")
    assert gripper_device.transport.writes == [("gripper", "closed")]


# --------------------------------------------------------------------------------------
# Type discipline
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, "45", None])
@pytest.mark.asyncio
async def test_non_numeric_values_never_reach_a_numeric_actuator(arm_device, value) -> None:
    """`bool` is a subclass of `int` in Python. A boolean is not a number here."""
    with pytest.raises(InvalidParams):
        await arm_device.write("joint_1", value)
    assert arm_device.transport.writes == []


# --------------------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_step_change_faster_than_max_rate_is_rejected(arm_device) -> None:
    """Both endpoints are in range; the transition between them is not survivable."""
    await arm_device.write("joint_1", 80.0)
    with pytest.raises(SafetyLimitViolation) as exc:
        await arm_device.write("joint_1", -80.0)

    assert exc.value.data["max_rate"] == 30.0
    assert exc.value.data["commanded_rate"] > 30.0
    assert arm_device.transport.writes == [("joint_1", 80.0)]  # only the first one landed


@pytest.mark.asyncio
async def test_rate_limit_also_enforced_by_the_server(rpc, arm_device) -> None:
    rpc_result(await rpc("mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 80.0}))
    error = rpc_error(
        await rpc("mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": -80.0})
    )
    assert error["code"] == SAFETY_LIMIT_VIOLATION
    assert "max_rate" in error["data"]


# --------------------------------------------------------------------------------------
# Sensors are never writable
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["joint_1_actual", "motor_temp", "estop_engaged"])
@pytest.mark.asyncio
async def test_writing_to_a_sensor_is_32602(rpc, arm_device, target) -> None:
    error = rpc_error(
        await rpc("mhs.write", {"device_id": "arm-01", "target": target, "value": 1.0})
    )
    assert error["code"] == INVALID_PARAMS
    assert "never writable" in error["message"]
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_writing_to_an_undeclared_target_is_32602(rpc, arm_device) -> None:
    error = rpc_error(
        await rpc("mhs.write", {"device_id": "arm-01", "target": "joint_9", "value": 1.0})
    )
    assert error["code"] == INVALID_PARAMS
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_reading_an_undeclared_target_raises_rather_than_returning_none(
    arm_device,
) -> None:
    """A silent None is indistinguishable from a genuine zero reading."""
    with pytest.raises(InvalidParams):
        await arm_device.read("joint_9")


@pytest.mark.asyncio
async def test_a_read_only_device_rejects_every_write(rpc, temp_device) -> None:
    error = rpc_error(
        await rpc("mhs.write", {"device_id": "mock-temp-01", "target": "ambient_temp",
                                "value": 20.0})
    )
    assert error["code"] == INVALID_PARAMS
    assert temp_device.transport.writes == []


# --------------------------------------------------------------------------------------
# Human confirmation gate
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmation_gated_actuator_refuses_an_unconfirmed_write(rpc, arm_device) -> None:
    error = rpc_error(
        await rpc("mhs.write", {"device_id": "arm-01", "target": "gripper", "value": "closed"})
    )
    assert error["code"] == INVALID_PARAMS
    assert error["data"]["requires_confirmation"] is True
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_confirmation_gated_actuator_accepts_a_confirmed_write(rpc, arm_device) -> None:
    result = rpc_result(
        await rpc("mhs.write", {"device_id": "arm-01", "target": "gripper",
                                "value": "closed", "confirm": True})
    )
    assert result["accepted"] is True
    assert arm_device.transport.writes == [("gripper", "closed")]


# --------------------------------------------------------------------------------------
# Hardware failure and state desync
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_failure_surfaces_as_32002_not_a_traceback(arm_factory) -> None:
    arm = arm_factory(fail_on={"joint_1"})
    with pytest.raises(HardwareExecutionError) as exc:
        await arm.write("joint_1", 10.0)
    assert exc.value.code == HARDWARE_EXECUTION_ERROR


@pytest.mark.asyncio
async def test_transport_failure_over_rpc_is_32002(arm_factory, arm_tag, temp_device,
                                                   gripper_device) -> None:
    import httpx

    from server.main import create_app
    from server.registry import Registry

    arm = arm_factory(fail_on={"joint_1"})
    registry = Registry()
    registry.register(arm.tag, arm)
    app = create_app(registry, load_mocks=False, auth_token=TEST_TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers=AUTH_HEADERS
    ) as client:
        body = (
            await client.post(
                "/rpc",
                json={"jsonrpc": "2.0", "id": 1, "method": "mhs.write",
                      "params": {"device_id": "arm-01", "target": "joint_1", "value": 10.0}},
            )
        ).json()
    assert body["error"]["code"] == HARDWARE_EXECUTION_ERROR


@pytest.mark.asyncio
async def test_an_axis_that_accepts_a_command_but_does_not_move_is_32003(arm_factory) -> None:
    """The write landed on the wire and the hardware stayed put. The agent must be told."""
    arm = arm_factory(ignore_writes={"joint_1"})
    with pytest.raises(StateDesync) as exc:
        await arm.write("joint_1", 45.0)

    assert exc.value.code == STATE_DESYNC
    assert exc.value.data["commanded"] == 45.0
    assert exc.value.data["observed"] == 0.0
    assert exc.value.data["feedback_sensor"] == "joint_1_actual"
    assert arm.transport.writes == [("joint_1", 45.0)]  # it WAS transmitted


@pytest.mark.asyncio
async def test_read_failure_surfaces_as_32002(rpc, arm_factory) -> None:
    arm = arm_factory(fail_on={"motor_temp"})
    with pytest.raises(HardwareExecutionError):
        await arm.read("motor_temp")


# --------------------------------------------------------------------------------------
# Emergency stop
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emergency_stop_drives_every_actuator_to_its_declared_safe_state(
    rpc, arm_device
) -> None:
    await arm_device.write("joint_1", 80.0)
    result = rpc_result(await rpc("mhs.emergency_stop", {"device_id": "arm-01"}))

    assert result["stopped"] is True
    assert result["safe_state"] == {"joint_1": 0.0, "joint_2": 0.0, "gripper": "open"}
    assert arm_device.transport.state["joint_1_actual"] == 0.0
    assert arm_device.transport.state["estop_engaged"] is True
    assert result["elapsed_ms"] <= result["max_stop_time_ms"]


@pytest.mark.asyncio
async def test_emergency_stop_is_not_blocked_by_the_rate_limit(arm_device) -> None:
    """A stop a safety limit could refuse is not a stop."""
    await arm_device.write("joint_1", 90.0)
    result = await arm_device.emergency_stop()
    assert result["stopped"] is True
    assert arm_device.transport.state["joint_1"] == 0.0


@pytest.mark.asyncio
async def test_emergency_stop_clears_rate_history_so_the_next_move_is_unblocked(
    rpc, arm_device
) -> None:
    rpc_result(await rpc("mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 85.0}))
    rpc_result(await rpc("mhs.emergency_stop", {"device_id": "arm-01"}))
    result = rpc_result(
        await rpc("mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": -85.0})
    )
    assert result["accepted"] is True


@pytest.mark.asyncio
async def test_emergency_stop_on_a_device_that_declares_none_is_32002(rpc, temp_device) -> None:
    error = rpc_error(await rpc("mhs.emergency_stop", {"device_id": "mock-temp-01"}))
    assert error["code"] == HARDWARE_EXECUTION_ERROR
    assert error["data"]["supported"] is False


# --------------------------------------------------------------------------------------
# The two enforcement points are genuinely independent
# --------------------------------------------------------------------------------------


async def _naive_write(client, target: str, value) -> dict:
    response = await client.post(
        "/rpc",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "mhs.write",
            "params": {"device_id": "arm-01", "target": target, "value": value,
                       "confirm": True},
        },
    )
    return response.json()


@pytest.mark.asyncio
async def test_middleware_rejects_an_unsafe_write_even_when_the_driver_would_not(
    naive_client, naive_arm
) -> None:
    """`NaiveDriver` enforces nothing. The middleware must stop the command anyway.

    This is the test that makes "two independent enforcement points" a claim rather than a
    comment: delete the check in `server/routers/rpc.py` and only this test fails.
    """
    before = naive_arm.transport.snapshot()
    body = await _naive_write(naive_client, "joint_1", 500.0)

    assert body["error"]["code"] == SAFETY_LIMIT_VIOLATION
    assert naive_arm.transport.writes == []
    assert naive_arm.transport.snapshot() == before


@pytest.mark.asyncio
async def test_middleware_rate_limits_a_driver_that_does_not(naive_client, naive_arm) -> None:
    assert "result" in await _naive_write(naive_client, "joint_1", 85.0)
    body = await _naive_write(naive_client, "joint_1", -85.0)

    assert body["error"]["code"] == SAFETY_LIMIT_VIOLATION
    assert body["error"]["data"]["max_rate"] == 30.0
    assert naive_arm.transport.writes == [("joint_1", 85.0)]


@pytest.mark.asyncio
async def test_middleware_blocks_a_forbidden_discrete_state_for_an_unchecked_driver(
    naive_client, naive_arm
) -> None:
    body = await _naive_write(naive_client, "gripper", "sideways")
    assert body["error"]["code"] == INVALID_PARAMS
    assert naive_arm.transport.writes == []


# --------------------------------------------------------------------------------------
# on_violation: the tag decides what a breach means
# --------------------------------------------------------------------------------------


def test_reject_is_the_default_when_a_tag_says_nothing(arm_tag) -> None:
    """Absent `on_violation`, a limit refuses. Nothing else is a safe default."""
    from server.models import CapabilityTag

    tag = CapabilityTag.model_validate(arm_tag)
    assert all(limit.on_violation == "reject" for limit in tag.safety_limits)


@pytest.mark.asyncio
async def test_clamp_substitutes_the_nearest_bound_and_proceeds(heater_device) -> None:
    result = await heater_device.write("heater_setpoint", 250.0)

    assert result["clamped"] is True
    assert result["written"] == 80.0
    assert result["requested"] == 250.0
    assert "clamped to the max bound 80.0" in result["clamp_reason"]
    # The clamped value, not the requested one, is what reached the hardware.
    assert heater_device.transport.writes == [("heater_setpoint", 80.0)]


@pytest.mark.asyncio
async def test_clamp_applies_to_the_lower_bound_too(heater_device) -> None:
    result = await heater_device.write("heater_setpoint", -40.0)
    assert result["written"] == 15.0
    assert "clamped to the min bound 15.0 degC" in result["clamp_reason"]
    assert heater_device.transport.writes == [("heater_setpoint", 15.0)]


@pytest.mark.asyncio
async def test_a_value_inside_the_bound_is_never_touched(heater_device) -> None:
    result = await heater_device.write("heater_setpoint", 60.0)
    assert result["clamped"] is False
    assert "requested" not in result
    assert heater_device.transport.writes == [("heater_setpoint", 60.0)]


@pytest.mark.parametrize("value", [15.0, 80.0])
@pytest.mark.asyncio
async def test_clamping_does_not_shrink_an_inclusive_bound(heater_device, value) -> None:
    """A clamp policy must not turn [15, 80] into an exclusive range."""
    result = await heater_device.write("heater_setpoint", value)
    assert result["clamped"] is False
    assert result["written"] == value


@pytest.mark.asyncio
async def test_clamp_also_rate_limits_rather_than_refusing(heater_device) -> None:
    """Under clamp a too-fast move travels as far as max_rate allows, in the right direction."""
    await heater_device.write("heater_setpoint", 30.0)
    result = await heater_device.write("heater_setpoint", 79.0)

    assert result["clamped"] is True
    assert result["requested"] == 79.0
    assert 30.0 < result["written"] < 79.0
    assert "rate-limited to" in result["clamp_reason"]
    assert heater_device.transport.writes[-1] == ("heater_setpoint", result["written"])


@pytest.mark.asyncio
async def test_every_clamp_is_logged_as_a_warning(heater_device, caplog) -> None:
    with caplog.at_level("WARNING", logger="open_mhs.safety"):
        await heater_device.write("heater_setpoint", 500.0)
    assert any("CLAMPED" in record.getMessage() for record in caplog.records)
    assert any("heater-01.heater_setpoint" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_clamp_is_reported_in_the_rpc_success_payload(
    client_factory, heater_device
) -> None:
    """A caller told only 'accepted' would believe the block is at 250 degC. It is at 80."""
    client = await client_factory(heater_device)
    result = rpc_result(await rpc_call(
        client, "mhs.write",
        {"device_id": "heater-01", "target": "heater_setpoint", "value": 250.0},
    ))

    assert result["accepted"] is True
    assert result["clamped"] is True
    assert result["commanded"] == 80.0
    assert result["requested"] == 250.0
    assert result["clamp_details"]["bound"] == "max"
    assert "solvent boils" in result["clamp_details"]["rationale"]


@pytest.mark.asyncio
async def test_clamped_write_is_still_verified_against_feedback(
    client_factory, heater_device
) -> None:
    client = await client_factory(heater_device)
    result = rpc_result(await rpc_call(
        client, "mhs.write",
        {"device_id": "heater-01", "target": "heater_setpoint", "value": 250.0},
    ))
    assert result["verified"] is True
    assert result["observed"] == 80.0


# --------------------------------------------------------------------------------------
# on_violation: estop
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_estop_limit_stops_the_device_and_still_refuses(
    client_factory, pump_device
) -> None:
    client = await client_factory(pump_device)
    await rpc_call(client, "mhs.write",
                   {"device_id": "pump-01", "target": "flow_rate", "value": 60.0})

    error = rpc_error(await rpc_call(
        client, "mhs.write", {"device_id": "pump-01", "target": "flow_rate", "value": 400.0}
    ))

    assert error["code"] == SAFETY_LIMIT_VIOLATION
    assert error["data"]["on_violation"] == "estop"
    assert error["data"]["emergency_stop"]["executed"] is True
    assert error["data"]["emergency_stop"]["safe_state"] == {"flow_rate": 0.0}
    # Refused AND stopped: the out-of-range value never went out, and flow is now zero.
    assert ("flow_rate", 400.0) not in pump_device.transport.writes
    assert pump_device.transport.state["flow_rate"] == 0.0


@pytest.mark.asyncio
async def test_estop_limit_is_honoured_by_the_driver_without_the_middleware(
    pump_device,
) -> None:
    with pytest.raises(SafetyLimitViolation) as exc:
        await pump_device.write("flow_rate", 400.0)
    assert exc.value.data["emergency_stop"]["executed"] is True
    assert pump_device.transport.state["flow_rate"] == 0.0


@pytest.mark.asyncio
async def test_a_failed_emergency_stop_does_not_mask_the_violation(
    client_factory, estop_tag
) -> None:
    """If the stop itself fails, the caller must still learn why the write was refused."""
    from drivers.transport import InMemoryTransport
    from tests.conftest import EstopPump, _no_sleep

    pump = EstopPump(
        estop_tag,
        InMemoryTransport({"flow_rate": 0.0, "flow_actual": 0.0}, fail_on={"flow_rate"}),
        sleep=_no_sleep,
    )
    client = await client_factory(pump)
    error = rpc_error(await rpc_call(
        client, "mhs.write", {"device_id": "pump-01", "target": "flow_rate", "value": 400.0}
    ))

    assert error["code"] == SAFETY_LIMIT_VIOLATION
    assert error["data"]["emergency_stop"]["executed"] is False
    assert "HardwareExecutionError" in error["data"]["emergency_stop"]["error"]


# --------------------------------------------------------------------------------------
# Incoherent on_violation policies are refused at ingestion
# --------------------------------------------------------------------------------------


def test_clamp_on_a_discrete_limit_is_rejected_at_ingestion(restricted_gripper_tag) -> None:
    """There is no nearest member of {open, closed}. The tag must not claim otherwise."""
    from pydantic import ValidationError

    from server.models import CapabilityTag

    doc = json.loads(json.dumps(restricted_gripper_tag))
    doc["safety_limits"][0]["on_violation"] = "clamp"
    with pytest.raises(ValidationError, match="meaningless for a discrete limit"):
        CapabilityTag.model_validate(doc)


def test_estop_without_a_declared_emergency_stop_is_rejected_at_ingestion(estop_tag) -> None:
    from pydantic import ValidationError

    from server.models import CapabilityTag

    doc = json.loads(json.dumps(estop_tag))
    del doc["emergency_stop"]
    with pytest.raises(ValidationError, match="requires the device to declare"):
        CapabilityTag.model_validate(doc)


@pytest.mark.asyncio
async def test_a_discrete_clamp_that_reaches_runtime_refuses_rather_than_guessing(
    restricted_gripper_tag,
) -> None:
    """Defence in depth: ingestion should have caught this, but runtime must not guess."""
    from server import safety
    from server.models import Actuator, SafetyLimit

    actuator = Actuator.model_validate(restricted_gripper_tag["actuators"][0])
    limit = SafetyLimit.model_validate(
        {**restricted_gripper_tag["safety_limits"][0], "on_violation": "clamp"}
    )
    with pytest.raises(SafetyLimitViolation, match="no meaning for a discrete bound"):
        safety.check_write(actuator, limit, "vent", device_id="gripper-01")
