"""Discovery Layer and JSON-RPC transport behaviour.

Safety enforcement has its own file — `test_safety.py`. This one covers the protocol: does
the server speak JSON-RPC 2.0 correctly, does discovery return usable capability tags, and
does ingestion validation reject a tag that would be dangerous to accept.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from server.errors import (
    DEVICE_NOT_FOUND,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
)
from server.models import CapabilityTag
from tests.conftest import REPO_ROOT, rpc_error, rpc_result

SCHEMA = json.loads((REPO_ROOT / "schema" / "capability_schema.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------
# Meta / discovery
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_liveness_and_spec_versions_and_nothing_else(client) -> None:
    """The one public endpoint must not hand an anonymous caller a hardware inventory.

    It DOES say which Capability Tag spec versions this reader accepts, because a device
    about to register needs to know whether its tag will be understood.
    """
    body = (await client.get("/health")).json()
    assert body == {
        "status": "ok",
        "mhs_version": "0.2",
        "supported_spec_versions": ["0.1", "0.2"],
    }
    assert "devices" not in body


@pytest.mark.asyncio
async def test_discover_lists_registered_devices(client) -> None:
    body = (await client.get("/discover")).json()
    assert body["count"] == 3
    assert [d["device_id"] for d in body["devices"]] == ["arm-01", "gripper-01", "mock-temp-01"]
    assert all(d["online"] and d["has_local_driver"] for d in body["devices"])


@pytest.mark.asyncio
async def test_discover_inlines_the_full_capability_tag(client) -> None:
    """An agent that must make a second call to learn capabilities decides without them."""
    body = (await client.get("/discover")).json()
    arm = next(d for d in body["devices"] if d["device_id"] == "arm-01")
    tag = arm["capability_tag"]
    assert {a["id"] for a in tag["actuators"]} == {"joint_1", "joint_2", "gripper"}
    assert {limit["target"] for limit in tag["safety_limits"]} == {"joint_1", "joint_2", "gripper"}
    joint_1 = next(limit for limit in tag["safety_limits"] if limit["target"] == "joint_1")
    assert (joint_1["min"], joint_1["max"], joint_1["max_rate"]) == (-90.0, 90.0, 30.0)


@pytest.mark.asyncio
async def test_discover_filters_by_type(client) -> None:
    body = (await client.get("/discover", params={"type": "robotic_arm"})).json()
    assert [d["device_id"] for d in body["devices"]] == ["arm-01"]


@pytest.mark.asyncio
async def test_get_unknown_device_is_404_carrying_the_rpc_error(client) -> None:
    response = await client.get("/devices/nope-01")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == DEVICE_NOT_FOUND
    assert error["data"]["known_devices"] == ["arm-01", "gripper-01", "mock-temp-01"]


@pytest.mark.asyncio
async def test_heartbeat_and_deregister(client) -> None:
    assert (await client.post("/devices/arm-01/heartbeat")).status_code == 200
    assert (await client.delete("/devices/arm-01")).json() == {
        "deregistered": True, "device_id": "arm-01"
    }
    assert (await client.get("/discover")).json()["count"] == 2


# --------------------------------------------------------------------------------------
# Registration = ingestion validation (enforcement point 1)
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_driver_registers_itself_with_the_registry(empty_client, temp_device) -> None:
    result = await temp_device.register(empty_client, registry_url="")
    assert result["registered"] is True
    assert result["device_id"] == "mock-temp-01"
    assert (await empty_client.get("/discover")).json()["count"] == 1


@pytest.mark.asyncio
async def test_registration_rejects_an_actuator_with_no_safety_limit(
    empty_client, unbounded_tag
) -> None:
    """An unbounded actuator is an unbounded machine. 422 before the registry sees it."""
    response = await empty_client.post("/register", json=unbounded_tag)
    assert response.status_code == 422
    assert "heater_setpoint" in json.dumps(response.json())
    assert (await empty_client.get("/discover")).json()["count"] == 0


@pytest.mark.asyncio
async def test_registration_rejects_an_unknown_top_level_key(empty_client, temp_tag) -> None:
    """A typo must not be silently ignored: `actuatorz` would disable safety enforcement."""
    response = await empty_client.post("/register", json={**temp_tag, "actuatorz": []})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_re_registration_replaces_the_tag(empty_client, temp_tag) -> None:
    first = await empty_client.post("/register", json=temp_tag)
    second = await empty_client.post("/register", json={**temp_tag, "name": "Renamed"})
    assert first.json()["registered"] and second.json()["registered"]
    assert "Re-registered" in second.json()["message"]
    assert (await empty_client.get("/discover")).json()["count"] == 1


# --------------------------------------------------------------------------------------
# Pydantic ingestion vs the canonical JSON Schema
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", sorted((REPO_ROOT / "examples").rglob("*.mhs")), ids=lambda p: Path(p).stem
)
def test_shipped_examples_satisfy_both_validators(path: Path) -> None:
    """The two ingestion definitions must never drift apart."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(SCHEMA).iter_errors(doc)) == []
    assert CapabilityTag.model_validate(doc).device_id == doc["device_id"]


def test_pydantic_catches_what_json_schema_structurally_cannot(unbounded_tag) -> None:
    """JSON Schema validates shape; it cannot express 'every actuator needs a limit'."""
    assert list(Draft202012Validator(SCHEMA).iter_errors(unbounded_tag)) == []
    with pytest.raises(ValidationError, match="heater_setpoint"):
        CapabilityTag.model_validate(unbounded_tag)


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ({"device_id": "Arm_01"}, "device_id"),
        ({"type": "teapot"}, "type"),
        ({"mhs_version": "9.9"}, "mhs_version"),
    ],
)
def test_both_validators_reject_the_same_malformed_tags(
    arm_tag: dict[str, Any], mutation: dict[str, Any], expected: str
) -> None:
    doc = {**arm_tag, **mutation}
    assert list(Draft202012Validator(SCHEMA).iter_errors(doc)), "JSON Schema accepted it"
    with pytest.raises(ValidationError, match=expected):
        CapabilityTag.model_validate(doc)


def test_unit_mismatch_between_actuator_and_limit_is_rejected(arm_tag) -> None:
    """`deg` vs `rad` is the classic field failure. Units are never converted."""
    doc = json.loads(json.dumps(arm_tag))
    next(limit for limit in doc["safety_limits"] if limit["target"] == "joint_1")["unit"] = "rad"
    with pytest.raises(ValidationError, match="unit mismatch"):
        CapabilityTag.model_validate(doc)


def test_id_shared_between_a_sensor_and_an_actuator_is_rejected(arm_tag) -> None:
    """read('x') and write('x') must never be ambiguous."""
    doc = json.loads(json.dumps(arm_tag))
    doc["sensors"].append({"id": "joint_1", "datatype": "number", "unit": "deg"})
    with pytest.raises(ValidationError, match="both a sensor and an actuator"):
        CapabilityTag.model_validate(doc)


def test_default_outside_its_own_limit_is_rejected(arm_tag) -> None:
    doc = json.loads(json.dumps(arm_tag))
    next(a for a in doc["actuators"] if a["id"] == "joint_1")["default"] = 120.0
    with pytest.raises(ValidationError, match="outside its safety limit"):
        CapabilityTag.model_validate(doc)


# --------------------------------------------------------------------------------------
# JSON-RPC 2.0 protocol
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_round_trip(rpc) -> None:
    result = rpc_result(await rpc("mhs.read", {"device_id": "arm-01", "target": "joint_1_actual"}))
    assert result["value"] == 0.0
    assert result["unit"] == "deg"
    assert result["datatype"] == "number"


@pytest.mark.asyncio
async def test_write_round_trip_verifies_against_feedback(rpc) -> None:
    result = rpc_result(
        await rpc("mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 45.0})
    )
    assert result["accepted"] is True
    assert result["commanded"] == 45.0
    assert result["verified"] is True
    assert result["observed"] == 45.0

    after = rpc_result(await rpc("mhs.read", {"device_id": "arm-01", "target": "joint_1_actual"}))
    assert after["value"] == 45.0


@pytest.mark.asyncio
async def test_sensor_read_from_the_mock_temperature_device(rpc) -> None:
    result = rpc_result(
        await rpc("mhs.read", {"device_id": "mock-temp-01", "target": "ambient_temp"})
    )
    assert result["unit"] == "degC"
    assert 15.0 < result["value"] < 30.0


@pytest.mark.asyncio
async def test_execute_is_an_alias_for_rpc(rpc) -> None:
    via_rpc = rpc_result(await rpc("mhs.read", {"device_id": "arm-01", "target": "motor_temp"}))
    via_execute = rpc_result(
        await rpc("mhs.read", {"device_id": "arm-01", "target": "motor_temp"}, path="/execute")
    )
    assert via_rpc["value"] == via_execute["value"]


@pytest.mark.asyncio
async def test_bare_method_aliases_are_accepted(rpc) -> None:
    result = rpc_result(await rpc("read", {"device_id": "arm-01", "target": "joint_2_actual"}))
    assert result["value"] == 0.0


@pytest.mark.asyncio
async def test_mhs_discover_over_rpc(rpc) -> None:
    result = rpc_result(await rpc("mhs.discover", {"type": "robotic_arm"}))
    assert result["count"] == 1
    assert result["devices"][0]["capability_tag"]["device_id"] == "arm-01"


@pytest.mark.asyncio
async def test_unknown_method_is_32601(rpc) -> None:
    error = rpc_error(await rpc("mhs.selfdestruct", {}))
    assert error["code"] == METHOD_NOT_FOUND
    assert "mhs.write" in error["data"]["supported"]


@pytest.mark.asyncio
async def test_unknown_device_is_32000(rpc) -> None:
    error = rpc_error(await rpc("mhs.read", {"device_id": "ghost-01", "target": "x"}))
    assert error["code"] == DEVICE_NOT_FOUND
    assert error["data"]["device_id"] == "ghost-01"


@pytest.mark.asyncio
async def test_malformed_json_is_32700(client) -> None:
    response = await client.post(
        "/rpc", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.json()["error"]["code"] == PARSE_ERROR


@pytest.mark.asyncio
async def test_missing_jsonrpc_member_is_32600(client) -> None:
    response = await client.post("/rpc", json={"method": "mhs.discover", "id": 1})
    assert response.json()["error"]["code"] == INVALID_REQUEST


@pytest.mark.asyncio
async def test_missing_params_is_32602(rpc) -> None:
    error = rpc_error(await rpc("mhs.read", {"device_id": "arm-01"}))
    assert error["code"] == INVALID_PARAMS


@pytest.mark.asyncio
async def test_positional_params_are_rejected(rpc) -> None:
    error = rpc_error(await rpc("mhs.read", ["arm-01", "joint_1_actual"]))
    assert error["code"] == INVALID_PARAMS


@pytest.mark.asyncio
async def test_a_caller_cannot_smuggle_its_own_limits_into_a_write(rpc) -> None:
    """Limits come from the registry's tag. A request that carries any is rejected."""
    error = rpc_error(
        await rpc(
            "mhs.write",
            {"device_id": "arm-01", "target": "joint_1", "value": 200.0,
             "safety_limits": [{"target": "joint_1", "min": -999, "max": 999}]},
        )
    )
    assert error["code"] == INVALID_PARAMS


@pytest.mark.asyncio
async def test_batch_requests_return_one_response_each(client) -> None:
    response = await client.post(
        "/rpc",
        json=[
            {"jsonrpc": "2.0", "id": "a", "method": "mhs.read",
             "params": {"device_id": "arm-01", "target": "joint_1_actual"}},
            {"jsonrpc": "2.0", "id": "b", "method": "mhs.read",
             "params": {"device_id": "ghost-01", "target": "x"}},
        ],
    )
    body = response.json()
    assert [item["id"] for item in body] == ["a", "b"]
    assert "result" in body[0]
    assert body[1]["error"]["code"] == DEVICE_NOT_FOUND


@pytest.mark.asyncio
async def test_empty_batch_is_32600(client) -> None:
    assert (await client.post("/rpc", json=[])).json()["error"]["code"] == INVALID_REQUEST


@pytest.mark.asyncio
async def test_notification_executes_and_returns_no_body(client, rpc, arm_device) -> None:
    """No `id` member means no response, per JSON-RPC 2.0 - but the write still lands."""
    assert await rpc("mhs.write", {"device_id": "arm-01", "target": "joint_2", "value": 20.0},
                     notification=True) is None
    assert arm_device.transport.writes == [("joint_2", 20.0)]


@pytest.mark.asyncio
async def test_openapi_documents_both_rpc_paths(client) -> None:
    spec = (await client.get("/openapi.json")).json()
    assert "/rpc" in spec["paths"] and "/execute" in spec["paths"]
    assert "/discover" in spec["paths"]
