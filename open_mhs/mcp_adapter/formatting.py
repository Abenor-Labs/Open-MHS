"""Rendering Open-MHS responses as text a language model can act on.

A refusal is only useful if the model can tell *why* it was refused and what to do
instead. Every rejection rendered here answers three questions: what was refused, what the
actual boundary is, and what a correct retry looks like. The numbers come straight from
`error.data`, which is why the middleware puts them there.
"""

from __future__ import annotations

from typing import Any

from open_mhs.mcp_adapter.client import OpenMHSUnreachable, RemoteRPCError, Unauthorized
from open_mhs.server.errors import (
    DEVICE_NOT_FOUND,
    HARDWARE_EXECUTION_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    SAFETY_LIMIT_VIOLATION,
    STATE_DESYNC,
)


def format_discovery(payload: dict[str, Any]) -> str:
    """Full inventory: what exists, what can be read, what can be written and within what."""
    devices = payload.get("devices", [])
    if not devices:
        return (
            "No hardware is registered with the Open-MHS middleware.\n"
            "Devices must announce themselves via POST /register before they can be used."
        )

    blocks: list[str] = [f"{len(devices)} device(s) registered.\n"]
    for entry in devices:
        tag = entry.get("capability_tag", {})
        status = "online" if entry.get("online") else "STALE (missed heartbeats)"
        header = f"=== {entry['device_id']} - {entry.get('name', '')} ==="
        lines = [
            header,
            f"type: {entry.get('type')}    status: {status}",
        ]
        if tag.get("description"):
            lines.append(f"description: {tag['description']}")
        power = tag.get("power") or {}
        if power.get("hazard_class") and power["hazard_class"] != "none":
            lines.append(f"HAZARD CLASS: {power['hazard_class']}")

        lines.append(_render_sensors(tag.get("sensors", [])))
        lines.append(_render_actuators(tag.get("actuators", []), tag.get("safety_limits", [])))
        lines.append(_render_estop(tag.get("emergency_stop")))
        blocks.append("\n".join(part for part in lines if part))
    return "\n\n".join(blocks)


def _render_sensors(sensors: list[dict[str, Any]]) -> str:
    if not sensors:
        return "READABLE (read_hardware_state): none"
    rows = []
    for s in sensors:
        unit = f" {s['unit']}" if s.get("unit") else ""
        extra = []
        if s.get("nominal_range"):
            rng = s["nominal_range"]
            extra.append(f"measures {rng['min']}..{rng['max']}{unit}")
        if s.get("enum_values"):
            extra.append("one of " + "/".join(s["enum_values"]))
        if s.get("accuracy") is not None:
            extra.append(f"+/-{s['accuracy']}{unit}")
        detail = f"  ({'; '.join(extra)})" if extra else ""
        rows.append(f"  - {s['id']} [{s['datatype']}{unit}]{detail}")
    return "READABLE (read_hardware_state):\n" + "\n".join(rows)


def _render_actuators(
    actuators: list[dict[str, Any]], safety_limits: list[dict[str, Any]]
) -> str:
    if not actuators:
        return "WRITABLE (write_hardware_state): none - this device is read-only"
    limits = {limit["target"]: limit for limit in safety_limits}
    rows = []
    for a in actuators:
        unit = f" {a['unit']}" if a.get("unit") else ""
        limit = limits.get(a["id"], {})
        if limit.get("allowed_values") is not None:
            bound = "allowed values: " + "/".join(str(v) for v in limit["allowed_values"])
        else:
            bound = f"allowed range: {limit.get('min')} to {limit.get('max')}{unit} inclusive"
        rows.append(f"  - {a['id']} [{a['datatype']}{unit}] {bound}")
        notes = []
        if limit.get("max_rate"):
            notes.append(f"max rate {limit['max_rate']}{unit}/s")
        if a.get("requires_confirmation"):
            notes.append("REQUIRES HUMAN CONFIRMATION before it will execute")
        if limit.get("enforcement"):
            notes.append(f"enforced in {limit['enforcement']}")
        if limit.get("rationale"):
            notes.append(f"why: {limit['rationale']}")
        for note in notes:
            rows.append(f"      {note}")
    return "WRITABLE (write_hardware_state):\n" + "\n".join(rows)


def _render_estop(estop: dict[str, Any] | None) -> str:
    if not estop or not estop.get("supported"):
        return "EMERGENCY STOP: not supported by this device"
    safe = estop.get("safe_state") or {}
    state = ", ".join(f"{k}={v}" for k, v in safe.items()) or "device-defined"
    return f"EMERGENCY STOP: supported - drives to {state}"


def format_read(result: dict[str, Any]) -> str:
    unit = f" {result['unit']}" if result.get("unit") else ""
    return (
        f"{result['device_id']}.{result['target']} = {result['value']}{unit} "
        f"(datatype: {result.get('datatype')})"
    )


def format_write(result: dict[str, Any]) -> str:
    unit = f" {result['unit']}" if result.get("unit") else ""
    if result.get("clamped"):
        lines = _clamped_header(result, unit)
    else:
        lines = [
            f"ACCEPTED. {result['device_id']}.{result['target']} commanded to "
            f"{result['commanded']}{unit}."
        ]
    if result.get("verified"):
        lines.append(
            f"Verified against feedback sensor {result.get('feedback_sensor')}: "
            f"reads {result.get('observed')}{unit}."
        )
    elif result.get("reason"):
        lines.append(f"Not independently verified: {result['reason']}.")
    return "\n".join(lines)


def _clamped_header(result: dict[str, Any], unit: str) -> list[str]:
    """A clamp is not a plain success and must never read like one.

    The hardware is sitting at a value the caller did not choose. If that fact is buried,
    the model keeps planning against the number it asked for, which is now fiction.
    """
    details = result.get("clamp_details") or {}
    lines = [
        "ACCEPTED BUT MODIFIED - the value you asked for was NOT used.",
        f"You requested {result['device_id']}.{result['target']} = "
        f"{result.get('requested')}{unit}.",
        f"The device's safety policy clamped it to {result['commanded']}{unit}, and that is "
        "what was sent to the hardware.",
    ]
    if details.get("bound") == "max_rate":
        lines.append(
            f"Reason: the move was faster than max_rate {details.get('max_rate')}{unit}/s. "
            "The remaining distance can be covered by issuing further writes over time."
        )
    else:
        lines.append(
            f"Reason: the allowed range is {details.get('min')} to {details.get('max')}{unit}, "
            "inclusive, and this limit declares on_violation 'clamp' rather than 'reject'."
        )
    condition = details.get("condition")
    if condition:
        # The bound that bit was not the one printed in the tag's headline range. Say which
        # state tightened it, or the agent re-reads the tag, sees the looser number, and
        # concludes the middleware is broken.
        lines.append(
            f"This bound is CONDITIONAL: it tightened to {details.get('min')}"
            f"{unit} because {condition['when_target']} currently reads "
            f"{condition.get('observed')!r}. The unconditional range is "
            f"{details.get('base_min')} to {details.get('base_max')}{unit}, and it will "
            f"apply again once {condition['when_target']} is no longer "
            f"{condition['equals']!r}."
        )
        if condition.get("rationale"):
            lines.append(f"Why this state is stricter: {condition['rationale']}")
    if details.get("rationale"):
        lines.append(f"Why the limit exists: {details['rationale']}")
    lines.append(
        f"The hardware is now at {result['commanded']}{unit}, NOT {result.get('requested')}"
        f"{unit}. Update your plan to match."
    )
    return lines


def format_emergency_stop(result: dict[str, Any]) -> str:
    safe = result.get("safe_state") or {}
    state = ", ".join(f"{k}={v}" for k, v in safe.items())
    return (
        f"EMERGENCY STOP executed on {result['device_id']}. "
        f"Driven to safe state: {state}. Took {result.get('elapsed_ms')} ms."
    )


def format_unreachable(exc: OpenMHSUnreachable) -> str:
    return (
        f"Cannot reach the Open-MHS middleware at {exc.base_url}.\n"
        f"Detail: {exc.detail}\n"
        "No command was sent to any hardware. Start the middleware with "
        "`uvicorn server.main:app`, or set OPEN_MHS_URL to the correct address."
    )


def format_unauthorized(exc: Unauthorized) -> str:
    """The server is healthy and refused us. Do not send the operator to restart it."""
    if exc.had_token:
        cause = (
            "A token was sent and the middleware rejected it. It is wrong, expired, or "
            "meant for a different deployment."
        )
    else:
        cause = "No token was sent. This adapter has no OPEN_MHS_AUTH_TOKEN configured."
    return "\n".join(
        [
            f"Not authorised to talk to the Open-MHS middleware at {exc.base_url}.",
            cause,
            "No command was sent to any hardware. The operator needs to set a matching "
            "OPEN_MHS_AUTH_TOKEN for both the middleware and this adapter. You cannot fix "
            "this yourself; ask them.",
        ]
    )


def format_rpc_error(exc: RemoteRPCError) -> str:
    """Turn a JSON-RPC error into an explanation plus a corrective next step."""
    renderer = _ERROR_RENDERERS.get(exc.code, _generic_error)
    return renderer(exc)


def _safety_violation(exc: RemoteRPCError) -> str:
    d = exc.data
    target = d.get("target", "the actuator")
    unit = f" {d['unit']}" if d.get("unit") else ""
    lines = [
        f"REJECTED - safety limit violation (code {exc.code}). "
        "Nothing was transmitted to the hardware.",
        f"You commanded {target} = {d.get('attempted')}{unit}.",
    ]
    if d.get("allowed_values") is not None:
        allowed = ", ".join(repr(v) for v in d["allowed_values"])
        lines.append(f"The only permitted values are: {allowed}.")
        lines.append("Retry with one of those values.")
    elif d.get("commanded_rate") is not None:
        lines.append(
            f"The value is in range, but the RATE OF CHANGE is not: "
            f"{d['commanded_rate']:.2f}{unit}/s against a limit of {d['max_rate']}{unit}/s."
        )
        lines.append(
            "Retry with a smaller step, or wait longer before the next command on this target."
        )
    else:
        lines.append(
            f"The allowed range is {d.get('min')} to {d.get('max')}{unit}, inclusive."
        )
        lines.append(f"Retry with a value between {d.get('min')} and {d.get('max')}.")

    if d.get("rationale"):
        lines.append(f"Why this limit exists: {d['rationale']}")
    if d.get("enforcement"):
        lines.append(f"Enforced in: {d['enforcement']}.")

    estop = d.get("emergency_stop")
    if estop and estop.get("executed"):
        safe = ", ".join(f"{k}={v}" for k, v in (estop.get("safe_state") or {}).items())
        lines.append(
            "This limit declares on_violation 'estop': THE DEVICE HAS BEEN STOPPED and driven "
            f"to its safe state ({safe}). Whatever it was doing has ended. Re-read its state "
            "before planning any further work."
        )
    elif estop is not None:
        lines.append(
            "This limit declares on_violation 'estop', but the stop itself FAILED "
            f"({estop.get('error')}). The device may still be running out of control - "
            "escalate to a human operator now."
        )

    lines.append("Do not attempt to work around this limit.")
    return "\n".join(lines)


def _invalid_params(exc: RemoteRPCError) -> str:
    d = exc.data
    lines = [f"REJECTED - invalid request (code {exc.code}). Nothing was sent to the hardware."]
    lines.append(exc.message)

    if d.get("requires_confirmation"):
        lines.append(
            "This actuator is gated behind human approval. Ask the operator to confirm, "
            "then call write_hardware_state again with confirm=true."
        )
    elif d.get("writable") is not None:
        writable = ", ".join(d["writable"]) or "nothing on this device"
        lines.append(f"Writable parameters on this device: {writable}.")
    elif d.get("readable") is not None:
        lines.append(f"Readable parameters on this device: {', '.join(d['readable'])}.")
    elif d.get("enum_values"):
        lines.append(f"This parameter accepts only: {', '.join(d['enum_values'])}.")
    elif d.get("datatype"):
        lines.append(f"This parameter expects a {d['datatype']}.")
    return "\n".join(lines)


def _device_not_found(exc: RemoteRPCError) -> str:
    known = exc.data.get("known_devices") or []
    listing = ", ".join(known) if known else "none - no hardware is registered"
    return (
        f"REJECTED - no such device (code {exc.code}).\n"
        f"{exc.message}\n"
        f"Registered devices: {listing}.\n"
        "Call discover_hardware to see what is actually available."
    )


def _hardware_error(exc: RemoteRPCError) -> str:
    return (
        f"HARDWARE FAILURE (code {exc.code}).\n"
        f"{exc.message}\n"
        "The device state after this failure is UNKNOWN - do not assume the command did or "
        "did not take effect. Read the relevant sensors before issuing another command."
    )


def _state_desync(exc: RemoteRPCError) -> str:
    d = exc.data
    return (
        f"STATE DESYNC (code {exc.code}). The command was transmitted, but the hardware did "
        "not end up where it was told to go.\n"
        f"Commanded: {d.get('commanded')}. Feedback sensor {d.get('feedback_sensor')} "
        f"reads: {d.get('observed')}.\n"
        "Your model of this device is now wrong. Stop, re-read the device state, and "
        "consider an emergency stop if the divergence is unsafe."
    )


def _method_not_found(exc: RemoteRPCError) -> str:
    supported = ", ".join(exc.data.get("supported", []))
    return f"REJECTED - unknown method (code {exc.code}). {exc.message}. Supported: {supported}."


def _generic_error(exc: RemoteRPCError) -> str:
    detail = f"\nDetail: {exc.data}" if exc.data else ""
    return f"REJECTED (code {exc.code}). {exc.message}{detail}"


_ERROR_RENDERERS = {
    SAFETY_LIMIT_VIOLATION: _safety_violation,
    INVALID_PARAMS: _invalid_params,
    DEVICE_NOT_FOUND: _device_not_found,
    HARDWARE_EXECUTION_ERROR: _hardware_error,
    STATE_DESYNC: _state_desync,
    METHOD_NOT_FOUND: _method_not_found,
}


# --------------------------------------------------------------------------------------
# Multi-device
# --------------------------------------------------------------------------------------


def format_snapshot(result: dict[str, Any]) -> str:
    lines = [f"SNAPSHOT of {result['count']} device(s):"]
    for device_id, device in result["devices"].items():
        status = "online" if device.get("online") else "OFFLINE"
        lines.append(f"\n{device_id} ({status})")
        for target, reading in device["channels"].items():
            if "value" in reading:
                unit = f" {reading['unit']}" if reading.get("unit") else ""
                lines.append(f"  {target} = {reading['value']}{unit}")
            else:
                err = reading["error"]
                lines.append(f"  {target}: UNREADABLE [{err.get('code')}] {err.get('message')}")
    return "\n".join(lines)


def format_check(result: dict[str, Any]) -> str:
    if result["ok"]:
        head = [
            f"PLAN OK: all {result['count']} write(s) are inside their envelopes; "
            "nothing was transmitted. Execute them with write_hardware_state, one at a time."
        ]
    else:
        bad = sum(1 for r in result["results"] if not r["ok"])
        head = [
            f"PLAN REJECTED: {bad} of {result['count']} write(s) would be refused; "
            "nothing was transmitted. Fix the items below before executing any of them."
        ]
    rows = []
    for r in result["results"]:
        label = f"#{r['index']} {r['device_id']}.{r['target']}"
        if r["ok"]:
            note = f"ok -> would transmit {r['would_transmit']}"
            if r.get("clamped"):
                note += f" (CLAMPED from {r.get('requested')}: {r.get('clamp_reason')})"
            rows.append(f"  {label}: {note}")
        else:
            err = r["error"]
            data = err.get("data") or {}
            bound = ""
            if "min" in data and "max" in data:
                unit = data.get("unit") or ""
                bound = f" - bound is [{data['min']}, {data['max']}] {unit}".rstrip()
            elif "allowed_values" in data:
                bound = f" - allowed: {data['allowed_values']}"
            attempted = data.get("attempted", "")
            rows.append(
                f"  {label} = {attempted}: REFUSED [{err.get('code')}] "
                f"{err.get('message')}{bound}"
            )
    return "\n".join(head + rows)


def format_estop_all(result: dict[str, Any]) -> str:
    stopped = sum(1 for r in result["devices"].values() if r.get("stopped"))
    skipped = sum(1 for r in result["devices"].values() if "skipped" in r)
    lines = [
        f"EMERGENCY STOP ALL: {stopped} stopped, {skipped} skipped, "
        f"{result['failed']} FAILED."
    ]
    for device_id, r in result["devices"].items():
        if r.get("stopped"):
            lines.append(f"  {device_id}: stopped -> {r.get('safe_state')}")
        elif "skipped" in r:
            lines.append(f"  {device_id}: skipped ({r['skipped']})")
        else:
            lines.append(f"  {device_id}: FAILED ({r.get('error')})")
    if result["failed"]:
        lines.append(
            "A device that failed to stop is in an UNKNOWN state. Tell the operator now."
        )
    return "\n".join(lines)
