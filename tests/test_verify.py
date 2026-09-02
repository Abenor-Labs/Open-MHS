"""Verification waits for the hardware, and a desync never hides a clamp.

Both of these were found by driving the robosuite cell live. Every full-span move came
back as a state desync because verification slept a fixed `settle_time_ms` and read the
sensor once, while the servo was still travelling. And when a below-floor command was
clamped and then desynced, the reply reported the clamped value as if it were the one the
caller had asked for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from open_mhs.drivers.base import BaseDevice
from open_mhs.drivers.transport import InMemoryTransport
from open_mhs.server.audit import AuditLog
from open_mhs.server.errors import StateDesync
from open_mhs.server.main import create_app
from open_mhs.server.registry import Registry
from tests.conftest import AUTH_HEADERS, FIXTURES, TEST_TOKEN, _no_sleep, load_tag, rpc_call


class LaggingTransport(InMemoryTransport):
    """Feedback catches up with the command only after `lag` reads, like a real servo."""

    def __init__(self, *args: Any, lag: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lag = lag
        self._pending: dict[str, tuple[Any, int]] = {}

    async def transmit(self, target: str, value: Any) -> None:
        self.writes.append((target, value))
        self.state[target] = value
        feedback = self.feedback_map.get(target)
        if feedback is not None:
            self._pending[feedback] = (value, self.lag)

    async def acquire(self, target: str) -> Any:
        self.reads.append(target)
        if target in self._pending:
            value, remaining = self._pending[target]
            if remaining <= 0:
                self.state[target] = value
                del self._pending[target]
            else:
                self._pending[target] = (value, remaining - 1)
        return self.state[target]


class Heater(BaseDevice):
    pass


def _heater(lag: int, settle_ms: int = 800) -> Heater:
    tag = load_tag(FIXTURES / "clamping_heater.mhs")
    for a in tag["actuators"]:
        a["settle_time_ms"] = settle_ms
    transport = LaggingTransport(
        {"heater_setpoint": 20.0, "block_temp": 20.0},
        feedback_map={"heater_setpoint": "block_temp"}, lag=lag,
    )
    return Heater(tag, transport, sleep=_no_sleep)


# --------------------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_move_that_settles_within_the_budget_is_verified() -> None:
    """Feedback lags five reads; an 800 ms budget at 50 ms polls allows sixteen."""
    heater = _heater(lag=5)
    result = await heater.write("heater_setpoint", 60.0)
    assert result["verified"] is True
    assert result["observed"] == 60.0


@pytest.mark.asyncio
async def test_a_move_that_never_settles_is_still_a_desync() -> None:
    heater = _heater(lag=10_000)
    with pytest.raises(StateDesync) as exc:
        await heater.write("heater_setpoint", 60.0)
    assert exc.value.data["commanded"] == 60.0
    assert exc.value.data["observed"] == 20.0
    assert exc.value.data["settle_time_ms"] == 800


@pytest.mark.asyncio
async def test_verification_returns_as_soon_as_the_sensor_agrees() -> None:
    """A finished move must not sit out the whole budget. Reads stop when it settles."""
    heater = _heater(lag=2)
    await heater.write("heater_setpoint", 60.0)
    feedback_reads = [t for t in heater.transport.reads if t == "block_temp"]
    assert len(feedback_reads) == 3  # first read, then two polls until it agrees


@pytest.mark.asyncio
async def test_no_settle_time_means_exactly_one_read() -> None:
    heater = _heater(lag=0, settle_ms=0)
    await heater.write("heater_setpoint", 60.0)
    assert [t for t in heater.transport.reads if t == "block_temp"] == ["block_temp"]


@pytest.mark.asyncio
async def test_a_stuck_axis_is_still_caught_by_the_shipped_tests(arm_factory) -> None:
    """The pre-existing desync path: a transport that accepts and never moves."""
    arm = arm_factory(ignore_writes={"joint_1"})
    with pytest.raises(StateDesync):
        await arm.write("joint_1", 30.0)


# --------------------------------------------------------------------------------------
# A desync never hides a clamp
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_desync_after_a_clamp_carries_the_clamp() -> None:
    heater = _heater(lag=10_000)
    with pytest.raises(StateDesync) as exc:
        await heater.write("heater_setpoint", 10_000.0)  # clamped to the max, never lands
    data = exc.value.data
    assert data["clamped"] is True
    assert data["requested"] == 10_000.0
    assert data["commanded"] < 10_000.0
    assert "clamp_reason" in data


@pytest.mark.asyncio
async def test_desync_without_a_clamp_does_not_claim_one() -> None:
    heater = _heater(lag=10_000)
    with pytest.raises(StateDesync) as exc:
        await heater.write("heater_setpoint", 60.0)
    assert "clamped" not in exc.value.data


async def _app_client(device: BaseDevice, path: Path) -> httpx.AsyncClient:
    reg = Registry()
    reg.register(device.tag, device)
    app = create_app(reg, load_mocks=False, auth_token=TEST_TOKEN, audit_log=AuditLog(path))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                             headers=AUTH_HEADERS)


@pytest.mark.asyncio
async def test_the_rpc_reply_and_the_audit_both_carry_the_clamp(tmp_path: Path) -> None:
    heater = _heater(lag=10_000)
    path = tmp_path / "a.jsonl"
    async with await _app_client(heater, path) as c:
        body = await rpc_call(c, "mhs.write", {"device_id": heater.device_id,
                                               "target": "heater_setpoint", "value": 10_000})
    error = body["error"]
    assert error["code"] == -32003
    assert error["data"]["clamped"] is True
    assert error["data"]["requested"] == 10_000

    line = json.loads(path.read_text().splitlines()[-1])
    assert line["event"] == "write.desync"
    assert line["outcome"]["transmitted"] == error["data"]["commanded"]
    assert line["outcome"]["requested"] == 10_000
    assert line["outcome"]["clamped"] is True


@pytest.mark.asyncio
async def test_a_desync_is_not_audited_as_a_refusal(tmp_path: Path, arm_factory) -> None:
    """The value reached the machine. 'transmitted: null' would be a lie in the audit."""
    arm = arm_factory(ignore_writes={"joint_1"})
    path = tmp_path / "a.jsonl"
    async with await _app_client(arm, path) as c:
        await rpc_call(c, "mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 30})
    line = json.loads(path.read_text().splitlines()[-1])
    assert line["event"] == "write.desync"
    assert line["outcome"]["transmitted"] == 30.0
    assert arm.transport.writes == [("joint_1", 30.0)]


@pytest.mark.asyncio
async def test_a_transport_failure_is_audited_as_unknown_not_refused(tmp_path: Path, arm_factory) -> None:
    arm = arm_factory(fail_on={"joint_1"})
    path = tmp_path / "a.jsonl"
    async with await _app_client(arm, path) as c:
        await rpc_call(c, "mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 30})
    line = json.loads(path.read_text().splitlines()[-1])
    assert line["event"] == "write.failed"
    assert line["outcome"]["transmitted"] == "unknown"


@pytest.mark.asyncio
async def test_the_mcp_text_tells_the_model_both_facts(tmp_path: Path) -> None:
    from open_mhs.mcp_adapter import server as adapter
    from open_mhs.mcp_adapter.client import OpenMHSClient

    heater = _heater(lag=10_000)
    async with await _app_client(heater, tmp_path / "a.jsonl") as c:
        adapter.set_client(OpenMHSClient(base_url="http://test", client=c))
        try:
            result = await adapter.mcp.call_tool("write_hardware_state", {
                "device_id": heater.device_id, "parameter": "heater_setpoint", "value": 10_000,
            })
        finally:
            adapter.set_client(None)
    blocks = result[0] if isinstance(result, tuple) else result
    text = "\n".join(getattr(b, "text", str(b)) for b in blocks)
    assert text.startswith("STATE DESYNC")
    assert "You requested 10000" in text
    assert "OUTSIDE the envelope and was clamped" in text
