"""JSON-RPC 2.0 error codes and typed exceptions for Open-MHS.

Every failure that can reach an agent is expressed here. Handlers raise these; the RPC
dispatcher is the only place that converts them to wire format. Nothing else builds an
error object by hand, so the code table cannot drift.
"""

from __future__ import annotations

from typing import Any

# --- Standard JSON-RPC 2.0 codes ---
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# --- Open-MHS implementation-defined codes (-32000..-32099) ---
DEVICE_NOT_FOUND = -32000
SAFETY_LIMIT_VIOLATION = -32001
HARDWARE_EXECUTION_ERROR = -32002
STATE_DESYNC = -32003

ERROR_NAMES: dict[int, str] = {
    PARSE_ERROR: "Parse error",
    INVALID_REQUEST: "Invalid request",
    METHOD_NOT_FOUND: "Method not found",
    INVALID_PARAMS: "Invalid params",
    INTERNAL_ERROR: "Internal error",
    DEVICE_NOT_FOUND: "Device not found",
    SAFETY_LIMIT_VIOLATION: "Safety limit violation",
    HARDWARE_EXECUTION_ERROR: "Hardware execution error",
    STATE_DESYNC: "State desync",
}


class MHSError(Exception):
    """Base for every error that maps onto a JSON-RPC error object."""

    code: int = INTERNAL_ERROR

    def __init__(self, message: str | None = None, data: dict[str, Any] | None = None) -> None:
        self.message = message or ERROR_NAMES.get(self.code, "Error")
        self.data = data or {}
        super().__init__(self.message)

    def to_rpc(self) -> dict[str, Any]:
        """Render as a JSON-RPC 2.0 `error` member."""
        obj: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            obj["data"] = self.data
        return obj


class ParseError(MHSError):
    code = PARSE_ERROR


class InvalidRequest(MHSError):
    code = INVALID_REQUEST


class MethodNotFound(MHSError):
    code = METHOD_NOT_FOUND


class InvalidParams(MHSError):
    """Malformed params, an unknown target, or a write aimed at a read-only target.

    A write to a sensor id lands here rather than on a custom code: the target exists, it
    is simply not a legal write target, which is a params problem and not a hardware one.
    """

    code = INVALID_PARAMS


class DeviceNotFound(MHSError):
    code = DEVICE_NOT_FOUND

    def __init__(self, device_id: str, known: list[str] | None = None) -> None:
        super().__init__(
            f"No device registered with id {device_id!r}",
            {"device_id": device_id, "known_devices": known or []},
        )


class SafetyLimitViolation(MHSError):
    """A write fell outside the device's declared envelope. Nothing was transmitted.

    `data` always carries the attempted value and the bound it violated so the agent can
    correct itself without a second round trip.
    """

    code = SAFETY_LIMIT_VIOLATION


class HardwareExecutionError(MHSError):
    """The driver or its transport failed. Device state is unknown, not assumed unchanged."""

    code = HARDWARE_EXECUTION_ERROR


class StateDesync(MHSError):
    """The write was accepted but the feedback sensor disagrees with the commanded value.

    `data` carries both the commanded and the observed value: the agent's model of the
    world has diverged from the world, and it cannot recover without seeing both numbers.
    """

    code = STATE_DESYNC
