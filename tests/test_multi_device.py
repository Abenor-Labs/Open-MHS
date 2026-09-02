"""Multi-device orchestration: one snapshot, one dry-run, one stop for the whole cell."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from open_mhs.server.errors import DEVICE_NOT_FOUND, INVALID_PARAMS, SAFETY_LIMIT_VIOLATION
from tests.conftest import REPO_ROOT, TEST_TOKEN, rpc_error, rpc_result

# --------------------------------------------------------------------------------------
# mhs.snapshot
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_reads_every_channel_of_every_device(rpc) -> None:
    result = rpc_result(await rpc("mhs.snapshot"))
    assert result["count"] == 3
    assert set(result["devices"]) == {"arm-01", "gripper-01", "mock-temp-01"}
    arm = result["devices"]["arm-01"]
    assert arm["online"] is True
    assert arm["channels"]["joint_1_actual"] == {"value": 0.0, "unit": "deg"}
    assert arm["channels"]["gripper"] == {"value": "open", "unit": None}
    assert set(arm["channels"]) == {
        "joint_1", "joint_2", "gripper", "joint_1_actual", "joint_2_actual",
        "motor_temp", "estop_engaged",
    }
    assert "timestamp" in result


@pytest.mark.asyncio
async def test_snapshot_can_be_narrowed(rpc) -> None:
    result = rpc_result(await rpc("mhs.snapshot", {"device_ids": ["arm-01"]}))
    assert list(result["devices"]) == ["arm-01"]


@pytest.mark.asyncio
async def test_snapshot_of_an_unknown_device_is_32000(rpc) -> None:
    error = rpc_error(await rpc("mhs.snapshot", {"device_ids": ["nope-01"]}))
    assert error["code"] == DEVICE_NOT_FOUND


@pytest.mark.asyncio
async def test_snapshot_reports_a_dead_channel_inline(client_factory, arm_factory) -> None:
    arm = arm_factory(fail_on={"motor_temp"})
    c = await client_factory(arm)
    body = (await c.post("/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "mhs.snapshot"})).json()
    channels = body["result"]["devices"]["arm-01"]["channels"]
    assert channels["joint_1_actual"] == {"value": 0.0, "unit": "deg"}
    assert channels["motor_temp"]["error"]["code"] == -32002
    assert "value" not in channels["motor_temp"]


@pytest.mark.asyncio
async def test_snapshot_transmits_nothing(rpc, arm_device, gripper_device) -> None:
    await rpc("mhs.snapshot")
    assert arm_device.transport.writes == []
    assert gripper_device.transport.writes == []


# --------------------------------------------------------------------------------------
# mhs.check
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_passes_a_valid_multi_device_plan_without_transmitting(
    rpc, arm_device, gripper_device
) -> None:
    result = rpc_result(await rpc("mhs.check", {"writes": [
        {"device_id": "arm-01", "target": "joint_1", "value": 45.0},
        {"device_id": "gripper-01", "target": "gripper", "value": "open"},
    ]}))
    assert result["ok"] is True
    assert result["transmitted"] is False
    assert [r["ok"] for r in result["results"]] == [True, True]
    assert result["results"][0]["would_transmit"] == 45.0
    assert result["results"][0]["clamped"] is False
    assert arm_device.transport.writes == []
    assert gripper_device.transport.writes == []


@pytest.mark.asyncio
async def test_check_reports_every_failing_item_and_transmits_nothing(rpc, arm_device) -> None:
    result = rpc_result(await rpc("mhs.check", {"writes": [
        {"device_id": "arm-01", "target": "joint_1", "value": 45.0},
        {"device_id": "arm-01", "target": "joint_1", "value": 500.0},
        {"device_id": "arm-01", "target": "gripper", "value": "closed"},
        {"device_id": "nope-01", "target": "x", "value": 1},
    ]}))
    assert result["ok"] is False
    codes = [r.get("error", {}).get("code") for r in result["results"]]
    assert codes == [None, SAFETY_LIMIT_VIOLATION, INVALID_PARAMS, DEVICE_NOT_FOUND]
    assert result["results"][1]["error"]["data"]["max"] == 90.0
    assert result["results"][2]["error"]["data"]["requires_confirmation"] is True
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_check_honours_the_confirm_flag(rpc, arm_device) -> None:
    result = rpc_result(await rpc("mhs.check", {"writes": [
        {"device_id": "arm-01", "target": "gripper", "value": "closed", "confirm": True},
    ]}))
    assert result["ok"] is True
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_check_reports_a_clamp_it_would_apply(client_factory, heater_device) -> None:
    c = await client_factory(heater_device)
    body = (await c.post("/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "mhs.check",
            "params": {"writes": [{"device_id": heater_device.device_id,
                                   "target": "heater_setpoint", "value": 10_000}]}})).json()
    item = body["result"]["results"][0]
    assert item["ok"] is True
    assert item["clamped"] is True
    assert item["requested"] == 10_000
    assert item["would_transmit"] < 10_000
    assert heater_device.transport.writes == []


@pytest.mark.asyncio
async def test_check_never_runs_an_emergency_stop(client_factory, pump_device) -> None:
    """A dry run of a write that would trip an estop limit reports it and stops nothing."""
    c = await client_factory(pump_device)
    body = (await c.post("/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "mhs.check",
            "params": {"writes": [{"device_id": pump_device.device_id, "target": "flow_rate",
                                   "value": 1e9}]}})).json()
    item = body["result"]["results"][0]
    assert item["ok"] is False
    assert item["error"]["code"] == SAFETY_LIMIT_VIOLATION
    assert "emergency_stop" not in item["error"].get("data", {})
    assert pump_device.transport.writes == []


@pytest.mark.asyncio
async def test_check_rejects_an_empty_plan(rpc) -> None:
    error = rpc_error(await rpc("mhs.check", {"writes": []}))
    assert error["code"] == INVALID_PARAMS


# --------------------------------------------------------------------------------------
# mhs.emergency_stop_all
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emergency_stop_all_stops_every_device_that_can_stop(
    rpc, arm_device, gripper_device, temp_device
) -> None:
    await rpc("mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 30.0})
    assert arm_device.transport.state["joint_1"] == 30.0
    result = rpc_result(await rpc("mhs.emergency_stop_all"))
    assert result["count"] == 3
    assert result["failed"] == 0
    assert result["devices"]["arm-01"]["stopped"] is True
    assert result["devices"]["mock-temp-01"]["stopped"] is False
    assert result["devices"]["mock-temp-01"]["skipped"] == "declares no emergency stop"
    assert arm_device.transport.state["joint_1"] == 0.0


@pytest.mark.asyncio
async def test_emergency_stop_all_continues_past_a_failure(
    client_factory, arm_factory, arm_tag
) -> None:
    """One device that cannot stop must not prevent the others from stopping."""
    broken = arm_factory(fail_on={"joint_1"})
    healthy = arm_factory()
    healthy._tag = healthy._load_tag({**arm_tag, "device_id": "arm-02"})
    c = await client_factory(broken, healthy)
    body = (await c.post("/rpc", json={"jsonrpc": "2.0", "id": 1,
                                        "method": "mhs.emergency_stop_all"})).json()
    result = body["result"]
    assert result["devices"]["arm-01"]["stopped"] is False
    assert "error" in result["devices"]["arm-01"]
    assert result["devices"]["arm-02"]["stopped"] is True
    assert result["failed"] == 1
    assert healthy.transport.state["joint_1"] == 0.0


@pytest.mark.asyncio
async def test_emergency_stop_all_clears_rate_history(rpc, arm_device) -> None:
    """After a fleet stop the next move must not be refused by the rate limit."""
    await rpc("mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 30.0})
    await rpc("mhs.emergency_stop_all")
    body = await rpc("mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": -30.0})
    assert "result" in body, json.dumps(body)


@pytest.mark.asyncio
async def test_new_methods_are_advertised_on_method_not_found(rpc) -> None:
    error = rpc_error(await rpc("mhs.nope"))
    assert {"mhs.snapshot", "mhs.check", "mhs.emergency_stop_all"} <= set(error["data"]["supported"])


# --------------------------------------------------------------------------------------
# The shipped example, end to end
# --------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_cell_agent_runs_clean_against_the_default_mocks(tmp_path, monkeypatch) -> None:
    """The shipped multi-device example must pass against the shipped devices, no hardware."""
    import uvicorn

    from open_mhs.server.main import create_app

    monkeypatch.setenv("OPEN_MHS_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    port = _free_port()
    app = create_app(auth_token=TEST_TOKEN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn did not come up"
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "examples" / "cell_agent.py"),
             "--url", f"http://127.0.0.1:{port}"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "OPEN_MHS_AUTH_TOKEN": TEST_TOKEN}, cwd=REPO_ROOT,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.rstrip().endswith("OK"), proc.stdout
    assert "discovered 3 device(s)" in proc.stdout
    assert "refused: bound" in proc.stdout
    events = [json.loads(line)["event"] for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert "check" in events and "write.accepted" in events and "estop_all" in events
