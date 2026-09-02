"""MCP adapter behaviour.

The adapter is wired to a real in-process Open-MHS app, so these tests exercise the whole
chain — MCP tool -> HTTP -> router -> safety check -> driver -> fake transport — and assert
on the text a language model would actually receive.

The text is the product here. A refusal the model cannot act on is a failed refusal, so
every rejection test asserts the reply names the real boundary, not just that it failed.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio

from open_mhs.mcp_adapter import server as adapter
from open_mhs.mcp_adapter.client import OpenMHSClient
from open_mhs.mcp_adapter.formatting import DEVICE_TEXT_OPEN
from tests.conftest import AUTH_HEADERS, TEST_TOKEN


@pytest_asyncio.fixture
async def mcp_wired(client: httpx.AsyncClient) -> AsyncIterator[None]:
    """Point the adapter at the in-process middleware for the duration of one test."""
    adapter.set_client(OpenMHSClient(base_url="http://test", client=client))
    yield
    adapter.set_client(None)


@pytest_asyncio.fixture
async def mcp_offline() -> AsyncIterator[None]:
    """Point the adapter at a middleware that refuses every connection."""

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    offline = httpx.AsyncClient(transport=httpx.MockTransport(_refuse))
    adapter.set_client(OpenMHSClient(base_url="http://127.0.0.1:8000", client=offline))
    yield
    adapter.set_client(None)
    await offline.aclose()


async def call(name: str, **arguments: Any) -> str:
    """Invoke a tool the way an MCP client does, and return the text it produces."""
    result = await adapter.mcp.call_tool(name, arguments)
    blocks = result[0] if isinstance(result, tuple) else result
    if isinstance(blocks, dict):
        return str(blocks)
    return "\n".join(getattr(b, "text", str(b)) for b in blocks)


# --------------------------------------------------------------------------------------
# Tool registration
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_seven_tools_are_registered() -> None:
    names = {tool.name for tool in await adapter.mcp.list_tools()}
    assert names == {
        "discover_hardware",
        "read_hardware_state",
        "write_hardware_state",
        "emergency_stop_hardware",
        "snapshot_hardware",
        "check_hardware_plan",
        "emergency_stop_all_hardware",
    }


@pytest.mark.asyncio
async def test_mutating_tools_are_annotated_destructive() -> None:
    """An MCP client shows a confirmation prompt based on these hints. They must be right."""
    tools = {tool.name: tool for tool in await adapter.mcp.list_tools()}
    assert tools["write_hardware_state"].annotations.destructiveHint is True
    assert tools["emergency_stop_hardware"].annotations.destructiveHint is True
    assert tools["read_hardware_state"].annotations.readOnlyHint is True
    assert tools["discover_hardware"].annotations.readOnlyHint is True


@pytest.mark.asyncio
async def test_write_tool_schema_exposes_the_confirm_gate() -> None:
    tools = {tool.name: tool for tool in await adapter.mcp.list_tools()}
    schema = tools["write_hardware_state"].inputSchema
    assert set(schema["required"]) == {"device_id", "parameter", "value"}
    assert "confirm" in schema["properties"]


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_lists_devices_with_their_bounds(mcp_wired) -> None:
    text = await call("discover_hardware")

    assert "3 device(s) registered." in text
    assert "arm-01" in text and "mock-temp-01" in text
    # The model must learn the envelope from this call, not by trial and error.
    assert "allowed range: -90.0 to 90.0 deg inclusive" in text
    assert "max rate 30.0 deg/s" in text
    assert "REQUIRES HUMAN CONFIRMATION" in text
    # The rationale reaches the model, and arrives marked as text the *tag* wrote
    # rather than text this tool wrote. See tests/test_untrusted_text.py.
    assert "Beyond +/-90 deg the arm collides with the bench mount" in text
    assert "why: " + DEVICE_TEXT_OPEN in text


@pytest.mark.asyncio
async def test_discovery_separates_readable_from_writable(mcp_wired) -> None:
    text = await call("discover_hardware")
    assert "READABLE (read_hardware_state):" in text
    assert "WRITABLE (write_hardware_state):" in text
    assert "this device is read-only" in text  # the temperature sensor
    assert "HAZARD CLASS: mechanical" in text  # the arm
    assert "EMERGENCY STOP: supported" in text


@pytest.mark.asyncio
async def test_discovery_filters_by_type(mcp_wired) -> None:
    text = await call("discover_hardware", device_type="robotic_arm")
    assert "1 device(s) registered." in text
    assert "mock-temp-01" not in text


# --------------------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_returns_value_with_its_unit(mcp_wired) -> None:
    text = await call("read_hardware_state", device_id="arm-01", parameter="joint_1_actual")
    assert text == "arm-01.joint_1_actual = 0.0 deg (datatype: number)"


@pytest.mark.asyncio
async def test_read_of_unknown_parameter_lists_what_is_readable(mcp_wired) -> None:
    text = await call("read_hardware_state", device_id="arm-01", parameter="joint_9")
    assert "REJECTED" in text
    assert "Readable parameters on this device:" in text
    assert "joint_1_actual" in text


@pytest.mark.asyncio
async def test_read_of_unknown_device_lists_registered_devices(mcp_wired) -> None:
    text = await call("read_hardware_state", device_id="ghost-01", parameter="x")
    assert "no such device" in text
    assert "Registered devices: arm-01, gripper-01, mock-temp-01." in text
    assert "discover_hardware" in text


# --------------------------------------------------------------------------------------
# Write - accepted
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accepted_write_reports_feedback_verification(mcp_wired, arm_device) -> None:
    text = await call("write_hardware_state", device_id="arm-01", parameter="joint_1", value=45.0)
    assert "ACCEPTED" in text
    assert "commanded to 45.0 deg" in text
    assert "Verified against feedback sensor joint_1_actual: reads 45.0 deg." in text
    assert arm_device.transport.writes == [("joint_1", 45.0)]


# --------------------------------------------------------------------------------------
# Write - refused, and the refusal must be actionable
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_bounds_write_explains_the_real_boundary(mcp_wired, arm_device) -> None:
    text = await call("write_hardware_state", device_id="arm-01", parameter="joint_1", value=300.0)

    assert "REJECTED - safety limit violation (code -32001)" in text
    assert "Nothing was transmitted to the hardware." in text
    assert "You commanded joint_1 = 300.0 deg." in text
    assert "The allowed range is -90.0 to 90.0 deg, inclusive." in text
    assert "Retry with a value between -90.0 and 90.0." in text
    assert "Beyond +/-90 deg" in text
    assert "Why this limit exists: " + DEVICE_TEXT_OPEN in text
    assert "Do not attempt to work around this limit." in text
    # The claim in the text must be true.
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_rate_violation_is_explained_as_a_rate_not_a_range(mcp_wired, arm_device) -> None:
    """The value is legal; the speed is not. A model told only 'rejected' would retry it."""
    assert "ACCEPTED" in await call(
        "write_hardware_state", device_id="arm-01", parameter="joint_1", value=85.0
    )
    text = await call("write_hardware_state", device_id="arm-01", parameter="joint_1", value=-85.0)

    assert "RATE OF CHANGE" in text
    assert "against a limit of 30.0 deg/s" in text
    assert "Retry with a smaller step, or wait longer" in text
    assert arm_device.transport.writes == [("joint_1", 85.0)]


@pytest.mark.asyncio
async def test_write_to_a_sensor_names_the_writable_parameters(mcp_wired, arm_device) -> None:
    text = await call(
        "write_hardware_state", device_id="arm-01", parameter="motor_temp", value=20.0
    )
    assert "code -32602" in text
    assert "is a sensor and is never writable" in text
    assert "Writable parameters on this device: gripper, joint_1, joint_2." in text
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_forbidden_discrete_state_lists_the_permitted_ones(mcp_wired, gripper_device) -> None:
    text = await call(
        "write_hardware_state", device_id="gripper-01", parameter="gripper", value="vent"
    )
    assert "safety limit violation" in text
    assert "The only permitted values are: 'open', 'closed'." in text
    assert "Retry with one of those values." in text
    assert gripper_device.transport.writes == []


@pytest.mark.asyncio
async def test_confirmation_gate_tells_the_model_to_ask_a_human(mcp_wired, arm_device) -> None:
    text = await call(
        "write_hardware_state", device_id="arm-01", parameter="gripper", value="closed"
    )
    assert "Ask the operator to confirm" in text
    assert "confirm=true" in text
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_confirmed_write_to_a_gated_actuator_succeeds(mcp_wired, arm_device) -> None:
    text = await call(
        "write_hardware_state",
        device_id="arm-01", parameter="gripper", value="closed", confirm=True,
    )
    assert "ACCEPTED" in text
    assert arm_device.transport.writes == [("gripper", "closed")]


@pytest.mark.asyncio
async def test_state_desync_tells_the_model_to_stop_and_re_read(
    arm_factory, gripper_device, temp_device
) -> None:
    from open_mhs.server.main import create_app
    from open_mhs.server.registry import Registry

    arm = arm_factory(ignore_writes={"joint_1"})
    registry = Registry()
    registry.register(arm.tag, arm)
    app = create_app(registry, load_mocks=False, auth_token=TEST_TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers=AUTH_HEADERS
    ) as http:
        adapter.set_client(OpenMHSClient(base_url="http://test", client=http))
        try:
            text = await call(
                "write_hardware_state", device_id="arm-01", parameter="joint_1", value=45.0
            )
        finally:
            adapter.set_client(None)

    assert "STATE DESYNC (code -32003)" in text
    assert "Commanded: 45.0" in text
    assert "reads: 0.0" in text
    assert "Your model of this device is now wrong" in text


# --------------------------------------------------------------------------------------
# Emergency stop
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emergency_stop_reports_the_safe_state_it_reached(mcp_wired, arm_device) -> None:
    await call("write_hardware_state", device_id="arm-01", parameter="joint_1", value=80.0)
    text = await call("emergency_stop_hardware", device_id="arm-01")

    assert "EMERGENCY STOP executed on arm-01" in text
    assert "joint_1=0.0" in text and "gripper=open" in text
    assert arm_device.transport.state["estop_engaged"] is True


@pytest.mark.asyncio
async def test_emergency_stop_on_a_device_without_one_says_so(mcp_wired) -> None:
    text = await call("emergency_stop_hardware", device_id="mock-temp-01")
    assert "HARDWARE FAILURE (code -32002)" in text
    assert "declares no emergency stop" in text


# --------------------------------------------------------------------------------------
# Middleware down
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreachable_middleware_states_that_nothing_was_sent(mcp_offline) -> None:
    """A model must not read a connection failure as 'the command probably went through'."""
    text = await call("write_hardware_state", device_id="arm-01", parameter="joint_1", value=10.0)
    assert "Cannot reach the Open-MHS middleware at http://127.0.0.1:8000" in text
    assert "No command was sent to any hardware." in text
    assert "uvicorn server.main:app" in text


@pytest.mark.asyncio
async def test_discovery_against_a_down_middleware_does_not_raise(mcp_offline) -> None:
    assert "Cannot reach the Open-MHS middleware" in await call("discover_hardware")


# --------------------------------------------------------------------------------------
# on_violation reaches the model as something it can act on
# --------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mcp_wired_to(client_factory) -> AsyncIterator[Any]:
    """Wire the adapter to a server built around specific devices."""

    async def _wire(*devices: Any) -> None:
        http = await client_factory(*devices)
        adapter.set_client(OpenMHSClient(base_url="http://test", client=http))

    yield _wire
    adapter.set_client(None)


@pytest.mark.asyncio
async def test_a_clamped_write_never_reads_like_a_plain_success(
    mcp_wired_to, heater_device
) -> None:
    """The model must not keep planning against 250 degC when the block is at 80."""
    await mcp_wired_to(heater_device)
    text = await call(
        "write_hardware_state", device_id="heater-01", parameter="heater_setpoint", value=250.0
    )

    assert "ACCEPTED BUT MODIFIED" in text
    assert "the value you asked for was NOT used" in text
    assert "You requested heater-01.heater_setpoint = 250.0 degC." in text
    assert "clamped it to 80.0 degC" in text
    assert "The hardware is now at 80.0 degC, NOT 250.0 degC" in text
    assert "The solvent boils at 82 degC" in text
    assert "Why the limit exists: " + DEVICE_TEXT_OPEN in text
    assert heater_device.transport.writes == [("heater_setpoint", 80.0)]


@pytest.mark.asyncio
async def test_an_unclamped_write_still_reads_as_a_plain_success(
    mcp_wired_to, heater_device
) -> None:
    await mcp_wired_to(heater_device)
    text = await call(
        "write_hardware_state", device_id="heater-01", parameter="heater_setpoint", value=45.0
    )
    assert text.startswith("ACCEPTED.")
    assert "MODIFIED" not in text


@pytest.mark.asyncio
async def test_a_rate_clamp_explains_that_distance_remains(mcp_wired_to, heater_device) -> None:
    await mcp_wired_to(heater_device)
    await call("write_hardware_state", device_id="heater-01",
               parameter="heater_setpoint", value=30.0)
    text = await call("write_hardware_state", device_id="heater-01",
                      parameter="heater_setpoint", value=79.0)

    assert "ACCEPTED BUT MODIFIED" in text
    assert "faster than max_rate 5.0 degC/s" in text
    assert "further writes over time" in text


@pytest.mark.asyncio
async def test_an_estop_violation_tells_the_model_the_device_has_stopped(
    mcp_wired_to, pump_device
) -> None:
    await mcp_wired_to(pump_device)
    text = await call(
        "write_hardware_state", device_id="pump-01", parameter="flow_rate", value=400.0
    )

    assert "safety limit violation" in text
    assert "THE DEVICE HAS BEEN STOPPED" in text
    assert "flow_rate=0.0" in text
    assert "Re-read its state before planning any further work." in text
    assert pump_device.transport.state["flow_rate"] == 0.0


# --------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def bare_client(registry) -> AsyncIterator[httpx.AsyncClient]:
    """A client with NO default auth headers, so the adapter must supply the token itself."""
    from open_mhs.server.main import create_app

    app = create_app(registry, load_mocks=False, auth_token=TEST_TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_the_adapter_sends_its_own_token(bare_client) -> None:
    """Proves the plumbing: nothing else on this path is adding an Authorization header."""
    adapter.set_client(
        OpenMHSClient(base_url="http://test", client=bare_client, token=TEST_TOKEN)
    )
    try:
        assert "3 device(s) registered." in await call("discover_hardware")
    finally:
        adapter.set_client(None)


@pytest.mark.asyncio
async def test_without_a_token_the_model_is_told_to_ask_a_human(bare_client) -> None:
    adapter.set_client(OpenMHSClient(base_url="http://test", client=bare_client, token=""))
    try:
        text = await call(
            "write_hardware_state", device_id="arm-01", parameter="joint_1", value=45.0
        )
    finally:
        adapter.set_client(None)

    assert "Not authorised" in text
    assert "No token was sent" in text
    assert "No command was sent to any hardware." in text
    assert "You cannot fix this yourself; ask them." in text


@pytest.mark.asyncio
async def test_a_rejected_token_is_reported_as_rejected_not_as_a_dead_server(
    bare_client, arm_device
) -> None:
    """'Server is down' would send the operator to restart a perfectly healthy service."""
    adapter.set_client(
        OpenMHSClient(base_url="http://test", client=bare_client, token="a-wrong-but-long-token")
    )
    try:
        text = await call(
            "write_hardware_state", device_id="arm-01", parameter="joint_1", value=45.0
        )
    finally:
        adapter.set_client(None)

    assert "A token was sent and the middleware rejected it" in text
    assert "Cannot reach" not in text
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_the_token_never_appears_in_tool_output(bare_client) -> None:
    """Tool output goes into a transcript. The secret must not go with it."""
    adapter.set_client(
        OpenMHSClient(base_url="http://test", client=bare_client, token=TEST_TOKEN)
    )
    try:
        text = await call("discover_hardware")
        text += await call("read_hardware_state", device_id="arm-01", parameter="joint_1_actual")
    finally:
        adapter.set_client(None)
    assert TEST_TOKEN not in text


# --------------------------------------------------------------------------------------
# Multi-device tools
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_device_tools_are_annotated_correctly() -> None:
    tools = {tool.name: tool for tool in await adapter.mcp.list_tools()}
    assert tools["snapshot_hardware"].annotations.readOnlyHint is True
    assert tools["check_hardware_plan"].annotations.readOnlyHint is True
    assert tools["emergency_stop_all_hardware"].annotations.destructiveHint is True


@pytest.mark.asyncio
async def test_snapshot_tool_lists_every_device_and_channel(mcp_wired) -> None:
    text = await call("snapshot_hardware")
    assert text.startswith("SNAPSHOT of 3 device(s)")
    assert "arm-01" in text and "mock-temp-01" in text and "gripper-01" in text
    assert "joint_1_actual = 0.0 deg" in text


@pytest.mark.asyncio
async def test_snapshot_tool_accepts_a_subset(mcp_wired) -> None:
    text = await call("snapshot_hardware", device_ids=["arm-01"])
    assert "SNAPSHOT of 1 device(s)" in text
    assert "mock-temp-01" not in text


@pytest.mark.asyncio
async def test_check_tool_names_the_failing_item_and_the_bound(mcp_wired, arm_device) -> None:
    text = await call("check_hardware_plan", writes=[
        {"device_id": "arm-01", "target": "joint_1", "value": 45.0},
        {"device_id": "arm-01", "target": "joint_1", "value": 500.0},
    ])
    assert text.startswith("PLAN REJECTED: 1 of 2")
    assert "#0 arm-01.joint_1: ok -> would transmit 45.0" in text
    assert "#1 arm-01.joint_1 = 500.0: REFUSED [-32001]" in text
    assert "[-90.0, 90.0]" in text
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_check_tool_reports_a_clean_plan(mcp_wired, arm_device) -> None:
    text = await call("check_hardware_plan", writes=[
        {"device_id": "arm-01", "target": "joint_1", "value": 45.0},
    ])
    assert text.startswith("PLAN OK")
    assert "nothing was transmitted" in text
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_stop_all_tool_reports_each_device(mcp_wired) -> None:
    text = await call("emergency_stop_all_hardware")
    assert text.startswith("EMERGENCY STOP ALL: 1 stopped, 2 skipped, 0 FAILED")
    assert "arm-01: stopped" in text
    assert "mock-temp-01: skipped (declares no emergency stop)" in text
