"""max_duration_s: an actuator held away from its default is forced back."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from drivers.base import BaseDevice
from drivers.transport import InMemoryTransport
from server.audit import AuditLog
from server.main import create_app
from server.registry import Registry
from tests.conftest import AUTH_HEADERS, FIXTURES, TEST_TOKEN, _no_sleep, load_tag, rpc_call

SETTLE = 0.5  # >3x the fixture's max_duration_s: an ASGI round trip on Windows is ~20 ms
PUMP = "deadman-pump-01"


class DeadmanPump(BaseDevice):
    """Concrete driver for the dead-man fixture."""


def _pump(tag: dict[str, Any]) -> DeadmanPump:
    return DeadmanPump(
        tag,
        InMemoryTransport({"flow_rate": 0.0, "flow_actual": 0.0},
                          feedback_map={"flow_rate": "flow_actual"}),
        sleep=_no_sleep,
    )


@pytest.fixture
def pump() -> DeadmanPump:
    return _pump(load_tag(FIXTURES / "deadman_pump.mhs"))


@pytest_asyncio.fixture
async def pump_client(pump: DeadmanPump, tmp_path: Path):
    reg = Registry()
    reg.register(pump.tag, pump)
    app = create_app(reg, load_mocks=False, auth_token=TEST_TOKEN,
                     audit_log=AuditLog(tmp_path / "audit.jsonl"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=AUTH_HEADERS) as c:
        async with app.router.lifespan_context(app):
            yield c, tmp_path / "audit.jsonl"


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.asyncio
async def test_actuator_is_returned_to_default_after_max_duration(pump_client, pump) -> None:
    c, _ = pump_client
    await rpc_call(c, "mhs.write", {"device_id": PUMP, "target": "flow_rate", "value": 5.0})
    assert pump.transport.writes == [("flow_rate", 5.0)]
    await asyncio.sleep(SETTLE)
    assert pump.transport.writes == [("flow_rate", 5.0), ("flow_rate", 0.0)]
    assert pump.transport.state["flow_actual"] == 0.0


@pytest.mark.asyncio
async def test_expiry_is_audited(pump_client) -> None:
    c, path = pump_client
    await rpc_call(c, "mhs.write", {"device_id": PUMP, "target": "flow_rate", "value": 5.0})
    await asyncio.sleep(SETTLE)
    expired = [e for e in _events(path) if e["event"] == "duration.expired"]
    assert len(expired) == 1
    assert expired[0]["device_id"] == PUMP
    assert expired[0]["target"] == "flow_rate"
    assert expired[0]["outcome"]["returned_to"] == 0.0
    assert expired[0]["outcome"]["held"] == 5.0
    assert expired[0]["outcome"]["max_duration_s"] == 0.15


@pytest.mark.asyncio
async def test_a_newer_write_restarts_the_timer(pump_client, pump) -> None:
    c, _ = pump_client
    await rpc_call(c, "mhs.write", {"device_id": PUMP, "target": "flow_rate", "value": 5.0})
    await rpc_call(c, "mhs.write", {"device_id": PUMP, "target": "flow_rate", "value": 6.0})
    await asyncio.sleep(SETTLE)
    # exactly one forced return, after the SECOND write
    assert pump.transport.writes == [("flow_rate", 5.0), ("flow_rate", 6.0), ("flow_rate", 0.0)]


@pytest.mark.asyncio
async def test_writing_the_default_starts_no_timer(pump_client, pump) -> None:
    c, path = pump_client
    await rpc_call(c, "mhs.write", {"device_id": PUMP, "target": "flow_rate", "value": 0.0})
    await asyncio.sleep(SETTLE)
    assert pump.transport.writes == [("flow_rate", 0.0)]
    assert "duration.expired" not in path.read_text()


@pytest.mark.asyncio
async def test_emergency_stop_cancels_the_timer(pump_client, pump) -> None:
    c, path = pump_client
    await rpc_call(c, "mhs.write", {"device_id": PUMP, "target": "flow_rate", "value": 5.0})
    await rpc_call(c, "mhs.emergency_stop", {"device_id": PUMP})
    await asyncio.sleep(SETTLE)
    # the e-stop wrote 0.0 once; the watchdog must not write it again
    assert pump.transport.writes == [("flow_rate", 5.0), ("flow_rate", 0.0)]
    assert "duration.expired" not in path.read_text()


@pytest.mark.asyncio
async def test_refused_write_starts_no_timer(pump_client, pump) -> None:
    c, path = pump_client
    await rpc_call(c, "mhs.write", {"device_id": PUMP, "target": "flow_rate", "value": 50.0})
    await asyncio.sleep(SETTLE)
    assert pump.transport.writes == []
    assert "duration.expired" not in path.read_text()


@pytest.mark.asyncio
async def test_a_limit_without_max_duration_starts_no_timer(client, arm_device) -> None:
    await rpc_call(client, "mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 30.0})
    await asyncio.sleep(SETTLE)
    assert arm_device.transport.writes == [("joint_1", 30.0)]


@pytest.mark.asyncio
async def test_a_refused_return_falls_back_to_emergency_stop(tmp_path: Path) -> None:
    """If the default cannot be written (here: max_rate forbids the drop), stop instead."""
    tag: dict[str, Any] = load_tag(FIXTURES / "deadman_pump.mhs")
    tag["safety_limits"][0]["max_rate"] = 0.001  # 5 -> 0 in 150 ms breaks this
    pump = _pump(tag)
    reg = Registry()
    reg.register(pump.tag, pump)
    path = tmp_path / "audit.jsonl"
    app = create_app(reg, load_mocks=False, auth_token=TEST_TOKEN, audit_log=AuditLog(path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=AUTH_HEADERS) as c:
        async with app.router.lifespan_context(app):
            # first write from the default: no previous accepted write, so no rate check
            body = await rpc_call(c, "mhs.write", {"device_id": PUMP, "target": "flow_rate", "value": 5.0})
            assert "result" in body, body
            await asyncio.sleep(SETTLE)
    assert pump.transport.state["flow_rate"] == 0.0  # safe state applied by the e-stop
    expired = [e for e in _events(path) if e["event"] == "duration.expired"]
    assert expired[0]["outcome"]["returned_to"] is None
    assert expired[0]["outcome"]["error"]["code"] == -32001
    assert expired[0]["outcome"]["emergency_stop"]["executed"] is True


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_timers(pump) -> None:
    from server.watchdog import Watchdog
    wd = Watchdog(AuditLog(None))
    reg = Registry()
    record = reg.register(pump.tag, pump)
    actuator = pump.tag.actuator_map["flow_rate"]
    wd.arm(record, actuator, pump.tag.limit_map["flow_rate"], 5.0)
    wd.shutdown()
    await asyncio.sleep(SETTLE)
    assert pump.transport.writes == []
