"""The CLI is the third gate. Same middleware, same refusals, same text."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli import main as cli
from mcp_adapter.client import OpenMHSClient
from server.audit import AuditLog
from tests.conftest import EXAMPLES


@pytest.fixture
def run(client, capsys):
    """Run the CLI against the in-process app; return (exit_code, stdout)."""

    async def _run(*argv: str) -> tuple[int, str]:
        code = await cli.amain(
            list(argv), client=OpenMHSClient(base_url="http://test", client=client)
        )
        return code, capsys.readouterr().out

    return _run


@pytest.mark.asyncio
async def test_discover_lists_devices(run) -> None:
    code, out = await run("discover")
    assert code == 0
    assert "arm-01" in out and "joint_1" in out and "mock-temp-01" in out


@pytest.mark.asyncio
async def test_read(run) -> None:
    code, out = await run("read", "arm-01", "joint_1_actual")
    assert code == 0
    assert "arm-01.joint_1_actual = 0.0 deg" in out


@pytest.mark.asyncio
async def test_write_inside_the_bound(run, arm_device) -> None:
    code, out = await run("write", "arm-01", "joint_1", "45")
    assert code == 0
    assert "ACCEPTED" in out
    assert arm_device.transport.writes == [("joint_1", 45.0)]


@pytest.mark.asyncio
async def test_write_outside_the_bound_exits_nonzero_and_transmits_nothing(run, arm_device) -> None:
    code, out = await run("write", "arm-01", "joint_1", "500")
    assert code == 1
    assert "90.0" in out
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_confirmation_gate_is_honoured(run, arm_device) -> None:
    code, out = await run("write", "arm-01", "gripper", "closed")
    assert code == 1
    assert "confirm" in out.lower()
    assert arm_device.transport.writes == []
    code, out = await run("write", "arm-01", "gripper", "closed", "--confirm")
    assert code == 0
    assert arm_device.transport.writes == [("gripper", "closed")]


@pytest.mark.asyncio
async def test_snapshot(run) -> None:
    code, out = await run("snapshot")
    assert code == 0
    assert "SNAPSHOT of 3 device(s)" in out
    code, out = await run("snapshot", "arm-01")
    assert code == 0
    assert "SNAPSHOT of 1 device(s)" in out


@pytest.mark.asyncio
async def test_check_from_a_plan_file(run, tmp_path: Path, arm_device) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([{"device_id": "arm-01", "target": "joint_1", "value": 500}]))
    code, out = await run("check", str(plan))
    assert code == 1
    assert "PLAN REJECTED" in out
    assert arm_device.transport.writes == []

    plan.write_text(json.dumps([{"device_id": "arm-01", "target": "joint_1", "value": 5}]))
    code, out = await run("check", str(plan))
    assert code == 0
    assert "PLAN OK" in out
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_estop_one_and_all(run, arm_device) -> None:
    await run("write", "arm-01", "joint_1", "30")
    code, out = await run("estop", "arm-01")
    assert code == 0
    assert arm_device.transport.state["joint_1"] == 0.0
    code, out = await run("estop", "--all")
    assert code == 0
    assert "arm-01: stopped" in out


@pytest.mark.asyncio
async def test_estop_without_a_target_is_a_usage_error(run) -> None:
    code, _ = await run("estop")
    assert code == 2


@pytest.mark.asyncio
async def test_unknown_device_is_a_refusal_not_a_traceback(run) -> None:
    code, out = await run("read", "nope-01", "x")
    assert code == 1
    assert "nope-01" in out


@pytest.mark.asyncio
async def test_describe_renders_a_tag_without_a_server(capsys) -> None:
    code = await cli.amain(["describe", str(EXAMPLES / "robotic_arm.mhs")])
    out = capsys.readouterr().out
    assert code == 0
    assert "joint_1" in out and "-90.0" in out and "REQUIRES HUMAN CONFIRMATION" in out


@pytest.mark.asyncio
async def test_audit_verify(tmp_path: Path, capsys) -> None:
    log = AuditLog(tmp_path / "a.jsonl")
    log.record("write.accepted", device_id="x", target="y", params={}, outcome={})
    code = await cli.amain(["audit", "verify", str(tmp_path / "a.jsonl")])
    assert code == 0
    assert "ok, 1 line(s), chain intact" in capsys.readouterr().out
    (tmp_path / "a.jsonl").write_text('{"seq": 1, "hash": "bad"}\n')
    code = await cli.amain(["audit", "verify", str(tmp_path / "a.jsonl")])
    assert code == 1
    assert "BROKEN at line 1" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_unreachable_middleware_exits_3(capsys) -> None:
    import httpx

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    offline = OpenMHSClient(
        base_url="http://127.0.0.1:9", client=httpx.AsyncClient(transport=httpx.MockTransport(_refuse))
    )
    code = await cli.amain(["discover"], client=offline)
    assert code == 3


def test_parse_value() -> None:
    assert cli.parse_value("45") == 45.0
    assert cli.parse_value("true") is True
    assert cli.parse_value("closed") == "closed"
    assert cli.parse_value("[1, 2, 3]") == [1, 2, 3]
