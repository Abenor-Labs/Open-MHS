"""JSON-RPC 2.0 dispatcher — the only surface that mutates hardware.

`POST /rpc` is canonical; `POST /execute` is an alias onto the same dispatcher. Methods:

    mhs.discover            read-only   list devices and their capability tags
    mhs.read                read-only   read one sensor or actuator state
    mhs.snapshot            read-only   every channel of every device, in one call
    mhs.check               read-only   dry-run a list of writes; nothing is transmitted
    mhs.write               MUTATING    command one actuator, inside its limits
    mhs.emergency_stop      MUTATING    drive a device to its declared safe state
    mhs.emergency_stop_all  MUTATING    drive every device that can stop to its safe state

Bare `read` / `write` are accepted as aliases.

This module is enforcement point 2 of 2: **runtime**. Every `mhs.write` is evaluated
against the registry's copy of the device's `safety_limits` *before* the driver is
touched. The driver then evaluates the same limits again before it touches its transport.
Two independent checks, one implementation in `server.safety`.

Every mutating call, accepted or refused, is written to the audit log.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, Request, Response
from pydantic import ValidationError

from open_mhs.server import safety
from open_mhs.server.audit import AuditLog
from open_mhs.server.deps import get_audit, get_registry, get_watchdog
from open_mhs.server.errors import (
    HardwareExecutionError,
    InvalidParams,
    InvalidRequest,
    MethodNotFound,
    MHSError,
    ParseError,
    StateDesync,
)
from open_mhs.server.models import (
    Actuator,
    CheckParams,
    DiscoverParams,
    EmergencyStopAllParams,
    EmergencyStopParams,
    JsonRpcRequest,
    ReadParams,
    SafetyLimit,
    SnapshotParams,
    WriteParams,
)
from open_mhs.server.registry import DeviceRecord, Registry
from open_mhs.server.watchdog import Watchdog

log = logging.getLogger("open_mhs.rpc")

router = APIRouter(tags=["execution"])


@dataclass
class Ctx:
    """Per-app services a handler may need besides the registry."""

    audit: AuditLog
    watchdog: Watchdog


Handler = Callable[[Any, Registry, Ctx], Awaitable[Any]]


# --------------------------------------------------------------------------------------
# Method implementations
# --------------------------------------------------------------------------------------


async def _discover(params: DiscoverParams, registry: Registry, ctx: Ctx) -> dict[str, Any]:
    records = registry.list(device_type=params.type, online_only=params.online_only)
    return {
        "count": len(records),
        "devices": [r.to_summary().model_dump(mode="json") for r in records],
    }


async def _read(params: ReadParams, registry: Registry, ctx: Ctx) -> dict[str, Any]:
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


async def _evaluate(
    record: DeviceRecord, params: WriteParams
) -> tuple[Actuator, SafetyLimit, Any, safety.SafetyDecision]:
    """Everything a write needs checked before anything is transmitted.

    Returns (actuator, limit, driver, decision). Raises exactly the errors `mhs.write`
    raises, including `EmergencyStopRequired` — but does NOT run the stop. The caller
    decides whether it is executing (run it) or only checking (report it).
    """
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

    # A conditional bound depends on what the device is doing right now, so the channels
    # it names are read before the envelope is resolved. Reading the sensor rather than
    # trusting the last commanded value is the point: a gripper that was told to close but
    # did not must not unlock the tighter payload bound.
    state = await _condition_state(driver, limit, params.device_id)

    decision = safety.check_write(
        actuator,
        limit,
        params.value,
        current=current,
        elapsed_s=elapsed_s,
        device_id=params.device_id,
        state=state,
    )
    return actuator, limit, driver, decision


async def _write(params: WriteParams, registry: Registry, ctx: Ctx) -> dict[str, Any]:
    record = registry.get(params.device_id)
    logged = {"value": params.value, "confirm": params.confirm}

    def refused(exc: MHSError) -> None:
        ctx.audit.record(
            "write.refused", device_id=params.device_id, target=params.target,
            params=logged, outcome={"transmitted": None, "error": exc.to_rpc()},
        )

    try:
        actuator, limit, driver, decision = await _evaluate(record, params)
    except safety.EmergencyStopRequired as exc:
        # The limit asked for a stop, not just a refusal. Run it before answering.
        exc.data["emergency_stop"] = await _run_estop_for_violation(
            record, _require_driver(record), ctx.watchdog
        )
        refused(exc)
        raise
    except MHSError as exc:
        refused(exc)
        raise

    try:
        result = await _guard_hardware(
            driver.write(params.target, decision.value, confirmed=params.confirm),
            params.device_id,
            params.target,
        )
    except StateDesync as exc:
        # Not a refusal: the value WAS transmitted, then the feedback disagreed. An audit
        # line saying "transmitted: null" here would be false, and false in the direction
        # that matters, because the machine did receive a command.
        #
        # The clamp happened HERE, before the driver ever saw the value, so the driver's
        # desync cannot know about it. Attach it, or the caller is told "commanded 0.83"
        # and never learns that it asked for 0.70.
        if decision.clamped:
            exc.data["clamped"] = True
            exc.data["requested"] = decision.original
            exc.data["clamp_reason"] = decision.reason
        ctx.audit.record(
            "write.desync", device_id=params.device_id, target=params.target, params=logged,
            outcome={
                "transmitted": decision.value,
                "requested": decision.original,
                "clamped": decision.clamped,
                "observed": exc.data.get("observed"),
                "error": exc.to_rpc(),
            },
        )
        raise
    except MHSError as exc:
        # The transport failed somewhere in the act of transmitting. Whether any byte
        # reached the device is unknown, and the record says so rather than guessing.
        ctx.audit.record(
            "write.failed", device_id=params.device_id, target=params.target, params=logged,
            outcome={"transmitted": "unknown", "attempted": decision.value,
                     "error": exc.to_rpc()},
        )
        raise

    record.last_write[params.target] = (decision.value, time.monotonic())
    ctx.watchdog.arm(record, actuator, limit, decision.value)
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
        # A single obvious field, so a caller that reads nothing else still cannot miss
        # that the value it asked for is not the value the hardware got.
        response["warning"] = (
            f"CLAMPED: {params.target} was commanded to {decision.original} but "
            f"{decision.value} was transmitted. {decision.reason}"
        )
    ctx.audit.record(
        "write.clamped" if decision.clamped else "write.accepted",
        device_id=params.device_id, target=params.target, params=logged,
        outcome={
            "transmitted": decision.value,
            "requested": decision.original,
            "clamped": decision.clamped,
            "verified": bool(response.get("verified")),
        },
    )
    return response


async def _condition_state(
    driver: Any, limit: Any, device_id: str
) -> dict[str, Any] | None:
    """Read the channels a conditional bound depends on. None when it has no conditions.

    A channel that cannot be read is omitted rather than defaulted. `effective_bounds`
    then falls back to the base bound, which is the stricter of the two by construction —
    so a failed read can only ever make the envelope tighter, never looser.
    """
    targets = safety.condition_targets(limit)
    if not targets:
        return None
    state: dict[str, Any] = {}
    for target in targets:
        try:
            state[target] = await driver.read(target)
        except Exception:
            log.warning(
                "%s: could not read %r for a conditional limit on %s; falling back to the "
                "base bound", device_id, target, limit.target,
            )
    return state


async def _run_estop_for_violation(
    record: DeviceRecord, driver: Any, watchdog: Watchdog
) -> dict[str, Any]:
    """Best-effort emergency stop triggered by an `on_violation: estop` limit.

    A failure to stop must not mask the violation that caused it, so this reports the
    failure inside the -32001 rather than replacing it with a -32002.
    """
    try:
        stopped = await driver.emergency_stop()
        record.last_write.clear()
        watchdog.cancel(record.device_id)
        return {"executed": True, **stopped}
    except Exception as exc:
        return {"executed": False, "error": f"{type(exc).__name__}: {exc}"}


async def _emergency_stop(
    params: EmergencyStopParams, registry: Registry, ctx: Ctx
) -> dict[str, Any]:
    """Drive the device to the safe state its own tag declares.

    The only mutating path that does not consult `safety_limits`: the safe state is
    trusted by definition, and a stop that a limit could refuse is not a stop.
    """
    record = registry.get(params.device_id)
    driver = _require_driver(record)
    try:
        result = await _guard_hardware(driver.emergency_stop(), params.device_id, None)
    except MHSError as exc:
        ctx.audit.record(
            "estop", device_id=params.device_id, target=None, params={},
            outcome={"stopped": False, "error": exc.to_rpc()},
        )
        raise
    record.last_write.clear()
    ctx.watchdog.cancel(params.device_id)
    ctx.audit.record(
        "estop", device_id=params.device_id, target=None, params={},
        outcome={"stopped": True, **{k: v for k, v in result.items() if k != "device_id"}},
    )
    return {"device_id": params.device_id, **result}


# --------------------------------------------------------------------------------------
# Multi-device methods
# --------------------------------------------------------------------------------------


async def _snapshot(params: SnapshotParams, registry: Registry, ctx: Ctx) -> dict[str, Any]:
    """Every readable channel of every device, in one call. Reads never mutate.

    A channel that cannot be read is reported inline with its error; one dead sensor must
    not hide the state of the rest of the cell.
    """
    if params.device_ids:
        records = [registry.get(device_id) for device_id in params.device_ids]
    else:
        records = registry.list()
    devices: dict[str, Any] = {}
    for record in records:
        tag = record.tag
        channels: dict[str, Any] = {}
        for target in sorted(set(tag.sensor_map) | set(tag.actuator_map)):
            channel = tag.sensor_map.get(target) or tag.actuator_map[target]
            if record.driver is None:
                channels[target] = {
                    "error": HardwareExecutionError(
                        f"{record.device_id}: no driver bound",
                        {"device_id": record.device_id, "target": target},
                    ).to_rpc()
                }
                continue
            try:
                value = await _guard_hardware(record.driver.read(target), record.device_id, target)
                channels[target] = {"value": value, "unit": channel.unit}
            except MHSError as exc:
                channels[target] = {"error": exc.to_rpc()}
        devices[record.device_id] = {"online": record.is_online(), "channels": channels}
    return {"count": len(devices), "timestamp": time.time(), "devices": devices}


async def _check(params: CheckParams, registry: Registry, ctx: Ctx) -> dict[str, Any]:
    """Dry-run a plan.

    Each item is evaluated exactly as `mhs.write` would evaluate it: same registry copy of
    the limits, same live conditional state, same rate context. Nothing is transmitted and
    no emergency stop runs. Items are independent: a later write to the same target is
    checked against the CURRENT state, not against an earlier item in the plan.
    """
    results: list[dict[str, Any]] = []
    for index, item in enumerate(params.writes):
        entry: dict[str, Any] = {
            "index": index, "device_id": item.device_id, "target": item.target,
        }
        try:
            record = registry.get(item.device_id)
            _, _, _, decision = await _evaluate(
                record,
                WriteParams(device_id=item.device_id, target=item.target,
                            value=item.value, confirm=item.confirm),
            )
            entry.update(ok=True, would_transmit=decision.value, clamped=decision.clamped)
            if decision.clamped:
                entry["requested"] = decision.original
                entry["clamp_reason"] = decision.reason
        except MHSError as exc:
            entry.update(ok=False, error=exc.to_rpc())
        results.append(entry)
    ok = all(r["ok"] for r in results)
    ctx.audit.record(
        "check", device_id=None, target=None,
        params={"count": len(results)},
        outcome={"ok": ok, "refused": [r["index"] for r in results if not r["ok"]]},
    )
    return {"ok": ok, "count": len(results), "transmitted": False, "results": results}


async def _emergency_stop_all(
    params: EmergencyStopAllParams, registry: Registry, ctx: Ctx
) -> dict[str, Any]:
    """Stop everything that can be stopped. A failure on one device never halts the loop."""
    devices: dict[str, Any] = {}
    failed = 0
    for record in registry.list():
        estop = record.tag.emergency_stop
        if estop is None or not estop.supported:
            devices[record.device_id] = {
                "stopped": False, "skipped": "declares no emergency stop",
            }
            continue
        if record.driver is None:
            devices[record.device_id] = {"stopped": False, "error": "no driver bound"}
            failed += 1
            continue
        try:
            result = await record.driver.emergency_stop()
            record.last_write.clear()
            ctx.watchdog.cancel(record.device_id)
            devices[record.device_id] = {"stopped": True, **result}
        except Exception as exc:
            devices[record.device_id] = {
                "stopped": False, "error": f"{type(exc).__name__}: {exc}",
            }
            failed += 1
    ctx.audit.record(
        "estop_all", device_id=None, target=None, params={},
        outcome={
            "count": len(devices), "failed": failed,
            "stopped": sorted(d for d, r in devices.items() if r["stopped"]),
        },
    )
    return {"count": len(devices), "failed": failed, "devices": devices}


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
    except Exception as exc:
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
    "mhs.snapshot": (SnapshotParams, _snapshot),
    "mhs.check": (CheckParams, _check),
    "mhs.emergency_stop_all": (EmergencyStopAllParams, _emergency_stop_all),
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


async def _dispatch_one(payload: Any, registry: Registry, ctx: Ctx) -> dict[str, Any] | None:
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

        result = await handler(params, registry, ctx)
    except MHSError as exc:
        return None if rpc.is_notification else _error_response(response_id, exc)
    except Exception as exc:
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


async def _handle(request: Request, registry: Registry, ctx: Ctx) -> Response:
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _json(_error_response(None, ParseError("Request body is not valid JSON")))

    if isinstance(payload, list):
        if not payload:
            return _json(_error_response(None, InvalidRequest("Batch must not be empty")))
        responses = [await _dispatch_one(item, registry, ctx) for item in payload]
        kept = [r for r in responses if r is not None]
        if not kept:  # an all-notification batch gets no response body at all
            return Response(status_code=204)
        return _json(kept)

    single = await _dispatch_one(payload, registry, ctx)
    if single is None:
        return Response(status_code=204)
    return _json(single)


def _json(body: Any) -> Response:
    return Response(content=json.dumps(body), media_type="application/json")


def get_ctx(
    audit: AuditLog = Depends(get_audit), watchdog: Watchdog = Depends(get_watchdog)
) -> Ctx:
    return Ctx(audit=audit, watchdog=watchdog)


# `response_model` is deliberately None: a JSON-RPC response is a success object, an error
# object, a batch array, or an empty 204, and no single Pydantic model covers all four
# without lying in the OpenAPI document. Request bodies are still strictly modelled.
@router.post("/rpc", response_model=None, summary="JSON-RPC 2.0 endpoint (canonical)")
async def rpc(
    request: Request,
    registry: Registry = Depends(get_registry),
    ctx: Ctx = Depends(get_ctx),
) -> Response:
    """Dispatch one JSON-RPC request or a batch.

    Supports notifications (a request with no `id` member): they execute and return no
    response, per the JSON-RPC 2.0 specification.
    """
    return await _handle(request, registry, ctx)


@router.post("/execute", response_model=None, summary="JSON-RPC 2.0 endpoint (alias of /rpc)")
async def execute(
    request: Request,
    registry: Registry = Depends(get_registry),
    ctx: Ctx = Depends(get_ctx),
) -> Response:
    """RESTful alias for `/rpc`. Identical semantics, same dispatcher."""
    return await _handle(request, registry, ctx)
