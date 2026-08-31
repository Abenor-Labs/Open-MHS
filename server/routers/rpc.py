"""JSON-RPC 2.0 dispatcher — the only surface that mutates hardware.

`POST /rpc` is canonical; `POST /execute` is an alias onto the same dispatcher. Four
methods and no others:

    mhs.discover          read-only   list devices and their capability tags
    mhs.read              read-only   read one sensor or actuator state
    mhs.write             MUTATING    command one actuator, inside its limits
    mhs.emergency_stop    MUTATING    drive a device to its declared safe state

Bare `read` / `write` are accepted as aliases.

This module is enforcement point 2 of 2: **runtime**. Every `mhs.write` is evaluated
against the registry's copy of the device's `safety_limits` *before* the driver is
touched. The driver then evaluates the same limits again before it touches its transport.
Two independent checks, one implementation in `server.safety`.
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, Request, Response
from pydantic import ValidationError

from server import safety
from server.deps import get_registry
from server.errors import (
    InvalidParams,
    InvalidRequest,
    MethodNotFound,
    MHSError,
    ParseError,
    HardwareExecutionError,
)
from server.models import (
    DiscoverParams,
    EmergencyStopParams,
    JsonRpcRequest,
    ReadParams,
    WriteParams,
)
from server.registry import DeviceRecord, Registry

router = APIRouter(tags=["execution"])

Handler = Callable[[Any, Registry], Awaitable[Any]]


# --------------------------------------------------------------------------------------
# Method implementations
# --------------------------------------------------------------------------------------


async def _discover(params: DiscoverParams, registry: Registry) -> dict[str, Any]:
    records = registry.list(device_type=params.type, online_only=params.online_only)
    return {
        "count": len(records),
        "devices": [r.to_summary().model_dump(mode="json") for r in records],
    }


async def _read(params: ReadParams, registry: Registry) -> dict[str, Any]:
    record = registry.get(params.device_id)
    tag = record.tag
    channel = tag.sensor_map.get(params.target) or tag.actuator_map.get(params.target)
    if channel is None:
        raise InvalidParams(
            f"{params.device_id}: no sensor or actuator named {params.target!r}",
            {
                "device_id": params.device_id,
                "target": params.target,
                "readable": sorted(set(tag.sensor_map) | set(tag.actuator_map)),
            },
        )
    driver = _require_driver(record)
    value = await _guard_hardware(driver.read(params.target), params.device_id, params.target)
    return {
        "device_id": params.device_id,
        "target": params.target,
        "value": value,
        "unit": channel.unit,
        "datatype": channel.datatype,
        "timestamp": time.time(),
    }


async def _write(params: WriteParams, registry: Registry) -> dict[str, Any]:
    record = registry.get(params.device_id)
    tag = record.tag

    if params.target in tag.sensor_map:
        raise InvalidParams(
            f"{params.device_id}: {params.target!r} is a sensor and is never writable",
            {
                "device_id": params.device_id,
                "target": params.target,
                "writable": sorted(tag.actuator_map),
            },
        )

    actuator = tag.actuator_map.get(params.target)
    if actuator is None:
        raise InvalidParams(
            f"{params.device_id}: no actuator named {params.target!r}",
            {
                "device_id": params.device_id,
                "target": params.target,
                "writable": sorted(tag.actuator_map),
            },
        )

    if actuator.requires_confirmation and not params.confirm:
        raise InvalidParams(
            f"{params.device_id}: {params.target!r} requires explicit human confirmation; "
            "re-send with confirm=true once a person has approved",
            {
                "device_id": params.device_id,
                "target": params.target,
                "requires_confirmation": True,
            },
        )

    # The limit comes from the registry's tag, never from the request. A caller cannot
    # widen its own envelope by attaching limits to a command.
    limit = tag.limit_map[params.target]
    current, elapsed_s = _rate_context(record, params.target, actuator)
    driver = _require_driver(record)

    try:
        decision = safety.check_write(
            actuator,
            limit,
            params.value,
            current=current,
            elapsed_s=elapsed_s,
            device_id=params.device_id,
        )
    except safety.EmergencyStopRequired as exc:
        # The limit asked for a stop, not just a refusal. Run it before answering.
        exc.data["emergency_stop"] = await _run_estop_for_violation(record, driver)
        raise

    result = await _guard_hardware(
        driver.write(params.target, decision.value, confirmed=params.confirm),
        params.device_id,
        params.target,
    )

    record.last_write[params.target] = (decision.value, time.monotonic())
    # The driver's own fields go in FIRST so the middleware's decision wins on any key they
    # share. The driver was handed an already-clamped value, so its `clamped` is False and
    # would otherwise erase the fact that a clamp happened at all.
    response = {
        **(result if isinstance(result, dict) else {"driver_result": result}),
        "device_id": params.device_id,
        "target": params.target,
        "commanded": decision.value,
        "accepted": True,
        "clamped": decision.clamped,
        "unit": actuator.unit,
        "timestamp": time.time(),
    }
    if decision.clamped:
        # The hardware did NOT go where the caller asked. Say so in the success payload,
        # or the caller proceeds on a false belief about the world.
        response["requested"] = decision.original
        response["clamp_reason"] = decision.reason
        response["clamp_details"] = decision.details
    return response


async def _run_estop_for_violation(record: DeviceRecord, driver: Any) -> dict[str, Any]:
    """Best-effort emergency stop triggered by an `on_violation: estop` limit.

    A failure to stop must not mask the violation that caused it, so this reports the
    failure inside the -32001 rather than replacing it with a -32002.
    """
    try:
        stopped = await driver.emergency_stop()
        record.last_write.clear()
        return {"executed": True, **stopped}
    except Exception as exc:  # noqa: BLE001 - the violation is the headline, not this
        return {"executed": False, "error": f"{type(exc).__name__}: {exc}"}


async def _emergency_stop(params: EmergencyStopParams, registry: Registry) -> dict[str, Any]:
    """Drive the device to the safe state its own tag declares.

    The only mutating path that does not consult `safety_limits`: the safe state is
    trusted by definition, and a stop that a limit could refuse is not a stop.
    """
    record = registry.get(params.device_id)
    driver = _require_driver(record)
    result = await _guard_hardware(driver.emergency_stop(), params.device_id, None)
    record.last_write.clear()
    return {"device_id": params.device_id, **result}


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _require_driver(record: DeviceRecord):
    if record.driver is None:
        raise HardwareExecutionError(
            f"{record.device_id}: registered, but no driver is bound to this middleware "
            "instance, so no command can reach the hardware",
            {"device_id": record.device_id, "registered": True, "driver_bound": False},
        )
    return record.driver


async def _guard_hardware(awaitable: Awaitable[Any], device_id: str, target: str | None) -> Any:
    """Anything a driver raises that is not already an MHSError is a hardware failure.

    Device state after such a failure is unknown, never assumed unchanged.
    """
    try:
        return await awaitable
    except MHSError:
        raise
    except Exception as exc:  # noqa: BLE001 - deliberate boundary
        raise HardwareExecutionError(
            f"{device_id}: driver raised {type(exc).__name__}: {exc}",
            {"device_id": device_id, "target": target, "exception": type(exc).__name__},
        ) from exc


def _rate_context(record: DeviceRecord, target: str, actuator) -> tuple[Any, float | None]:
    """Previous accepted value and how long ago, for `max_rate` and relative writes."""
    previous = record.last_write.get(target)
    if previous is None:
        return (actuator.default, None)
    value, at = previous
    return (value, max(time.monotonic() - at, 1e-9))


METHODS: dict[str, tuple[type, Handler]] = {
    "mhs.discover": (DiscoverParams, _discover),
    "mhs.read": (ReadParams, _read),
    "mhs.write": (WriteParams, _write),
    "mhs.emergency_stop": (EmergencyStopParams, _emergency_stop),
    # Legacy bare aliases.
    "discover": (DiscoverParams, _discover),
    "read": (ReadParams, _read),
    "write": (WriteParams, _write),
    "emergency_stop": (EmergencyStopParams, _emergency_stop),
}


# --------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------


def _error_response(request_id: Any, error: MHSError) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": error.to_rpc()}


async def _dispatch_one(payload: Any, registry: Registry) -> dict[str, Any] | None:
    """Execute one JSON-RPC request object. Returns None for a notification."""
    if not isinstance(payload, dict):
        return _error_response(None, InvalidRequest("A JSON-RPC request must be an object"))

    request_id = payload.get("id")
    try:
        rpc = JsonRpcRequest.model_validate(payload)
    except ValidationError as exc:
        return _error_response(
            request_id, InvalidRequest("Malformed JSON-RPC envelope", {"errors": exc.errors(
                include_url=False, include_input=False)})
        )

    response_id = None if rpc.is_notification else rpc.id

    try:
        entry = METHODS.get(rpc.method)
        if entry is None:
            raise MethodNotFound(
                f"Unknown method {rpc.method!r}",
                {"method": rpc.method, "supported": sorted(m for m in METHODS if "." in m)},
            )
        params_model, handler = entry

        if isinstance(rpc.params, list):
            raise InvalidParams(
                "Positional params are not supported; send params as an object",
                {"method": rpc.method},
            )
        try:
            params = params_model.model_validate(rpc.params or {})
        except ValidationError as exc:
            raise InvalidParams(
                f"Invalid params for {rpc.method!r}",
                {"errors": exc.errors(include_url=False, include_input=False)},
            ) from exc

        result = await handler(params, registry)
    except MHSError as exc:
        return None if rpc.is_notification else _error_response(response_id, exc)
    except Exception as exc:  # noqa: BLE001 - never leak a traceback to an agent
        return (
            None
            if rpc.is_notification
            else _error_response(
                response_id,
                MHSError(f"Internal error handling {rpc.method!r}",
                         {"exception": type(exc).__name__}),
            )
        )

    if rpc.is_notification:
        return None
    return {"jsonrpc": "2.0", "id": response_id, "result": result}


async def _handle(request: Request, registry: Registry) -> Response:
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _json(_error_response(None, ParseError("Request body is not valid JSON")))

    if isinstance(payload, list):
        if not payload:
            return _json(_error_response(None, InvalidRequest("Batch must not be empty")))
        responses = [await _dispatch_one(item, registry) for item in payload]
        kept = [r for r in responses if r is not None]
        if not kept:  # an all-notification batch gets no response body at all
            return Response(status_code=204)
        return _json(kept)

    single = await _dispatch_one(payload, registry)
    if single is None:
        return Response(status_code=204)
    return _json(single)


def _json(body: Any) -> Response:
    return Response(content=json.dumps(body), media_type="application/json")


# `response_model` is deliberately None: a JSON-RPC response is a success object, an error
# object, a batch array, or an empty 204, and no single Pydantic model covers all four
# without lying in the OpenAPI document. Request bodies are still strictly modelled.
@router.post("/rpc", response_model=None, summary="JSON-RPC 2.0 endpoint (canonical)")
async def rpc(request: Request, registry: Registry = Depends(get_registry)) -> Response:
    """Dispatch one JSON-RPC request or a batch.

    Supports notifications (a request with no `id` member): they execute and return no
    response, per the JSON-RPC 2.0 specification.
    """
    return await _handle(request, registry)


@router.post("/execute", response_model=None, summary="JSON-RPC 2.0 endpoint (alias of /rpc)")
async def execute(request: Request, registry: Registry = Depends(get_registry)) -> Response:
    """RESTful alias for `/rpc`. Identical semantics, same dispatcher."""
    return await _handle(request, registry)
