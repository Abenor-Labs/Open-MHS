"""Audit log: every command and every refusal leaves a tamper-evident line."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from server.audit import GENESIS, AuditLog, verify
from server.errors import SAFETY_LIMIT_VIOLATION
from server.main import create_app
from server.registry import Registry
from tests.conftest import AUTH_HEADERS, TEST_TOKEN, rpc_call


def _write(log: AuditLog, value: float) -> None:
    log.record("write.accepted", device_id="arm-01", target="joint_1",
               params={"value": value}, outcome={"transmitted": value})


def test_first_line_chains_from_genesis(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl")
    _write(log, 10.0)
    line = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[0])
    assert line["seq"] == 1
    assert line["prev"] == GENESIS
    assert len(line["hash"]) == 64


def test_verify_accepts_an_untouched_log(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(5):
        _write(log, float(i))
    assert verify(tmp_path / "a.jsonl") == {"ok": True, "lines": 5, "first_bad_line": None}


def test_verify_detects_a_modified_value(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    log = AuditLog(path)
    _write(log, 10.0)
    _write(log, 20.0)
    lines = path.read_text().splitlines()
    lines[0] = lines[0].replace("10.0", "90.0")
    path.write_text("\n".join(lines) + "\n")
    report = verify(path)
    assert report["ok"] is False
    assert report["first_bad_line"] == 1


def test_verify_detects_a_deleted_line(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    log = AuditLog(path)
    for i in range(3):
        _write(log, float(i))
    lines = path.read_text().splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n")
    assert verify(path)["first_bad_line"] == 2


def test_reopening_a_log_continues_the_chain(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _write(AuditLog(path), 1.0)
    _write(AuditLog(path), 2.0)  # a fresh writer, as after a restart
    assert verify(path) == {"ok": True, "lines": 2, "first_bad_line": None}
    assert json.loads(path.read_text().splitlines()[1])["seq"] == 2


def test_disabled_log_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPEN_MHS_AUDIT_LOG", "off")
    monkeypatch.chdir(tmp_path)
    log = AuditLog.from_env()
    assert log.enabled is False
    log.record("write.accepted", device_id="x", target="y", params={}, outcome={})
    assert list(tmp_path.iterdir()) == []


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test", headers=AUTH_HEADERS)


@pytest.mark.asyncio
async def test_refused_write_is_logged_with_the_error_and_no_transmission(
    tmp_path: Path, arm_device
) -> None:
    path = tmp_path / "a.jsonl"
    reg = Registry()
    reg.register(arm_device.tag, arm_device)
    app = create_app(reg, load_mocks=False, auth_token=TEST_TOKEN, audit_log=AuditLog(path))
    async with await _client(app) as c:
        await rpc_call(c, "mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 500})
    line = json.loads(path.read_text().splitlines()[-1])
    assert line["event"] == "write.refused"
    assert line["device_id"] == "arm-01"
    assert line["target"] == "joint_1"
    assert line["outcome"]["error"]["code"] == SAFETY_LIMIT_VIOLATION
    assert line["outcome"]["transmitted"] is None
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_accepted_write_and_estop_are_logged(tmp_path: Path, arm_device) -> None:
    path = tmp_path / "a.jsonl"
    reg = Registry()
    reg.register(arm_device.tag, arm_device)
    app = create_app(reg, load_mocks=False, auth_token=TEST_TOKEN, audit_log=AuditLog(path))
    async with await _client(app) as c:
        await rpc_call(c, "mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 10})
        await rpc_call(c, "mhs.emergency_stop", {"device_id": "arm-01"})
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert [e["event"] for e in entries] == ["write.accepted", "estop"]
    assert entries[0]["outcome"]["transmitted"] == 10.0
    assert entries[0]["outcome"]["verified"] is True
    assert entries[1]["outcome"]["stopped"] is True
    assert verify(path)["ok"] is True


@pytest.mark.asyncio
async def test_a_clamped_write_is_logged_as_clamped(tmp_path: Path, heater_device) -> None:
    path = tmp_path / "a.jsonl"
    reg = Registry()
    reg.register(heater_device.tag, heater_device)
    app = create_app(reg, load_mocks=False, auth_token=TEST_TOKEN, audit_log=AuditLog(path))
    async with await _client(app) as c:
        await rpc_call(c, "mhs.write",
                       {"device_id": heater_device.device_id, "target": "heater_setpoint",
                        "value": 10_000})
    line = json.loads(path.read_text().splitlines()[-1])
    assert line["event"] == "write.clamped"
    assert line["outcome"]["requested"] == 10_000
    assert line["outcome"]["transmitted"] < 10_000


@pytest.mark.asyncio
async def test_register_and_deregister_are_logged(tmp_path: Path, arm_tag) -> None:
    path = tmp_path / "a.jsonl"
    app = create_app(Registry(), load_mocks=False, auth_token=TEST_TOKEN, audit_log=AuditLog(path))
    async with await _client(app) as c:
        await c.post("/register", json=arm_tag)
        await c.delete("/devices/arm-01")
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert [e["event"] for e in entries] == ["register", "deregister"]
    assert entries[0]["device_id"] == "arm-01"
    assert entries[0]["outcome"]["mhs_version"] == "0.1"
    assert entries[0]["outcome"]["actuators"] == ["gripper", "joint_1", "joint_2"]


@pytest.mark.asyncio
async def test_the_default_app_logs_to_the_env_path(tmp_path: Path, monkeypatch, arm_device) -> None:
    """`OPEN_MHS_AUDIT_LOG` is the only configuration a deployment needs."""
    path = tmp_path / "from-env.jsonl"
    monkeypatch.setenv("OPEN_MHS_AUDIT_LOG", str(path))
    reg = Registry()
    reg.register(arm_device.tag, arm_device)
    app = create_app(reg, load_mocks=False, auth_token=TEST_TOKEN)
    async with await _client(app) as c:
        await rpc_call(c, "mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 1})
    assert verify(path)["lines"] == 1
