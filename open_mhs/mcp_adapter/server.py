"""MCP server exposing an Open-MHS middleware instance to any MCP client.

Run over stdio (what Claude Desktop expects):

    python -m open_mhs.mcp_adapter.server

Point it at a middleware other than localhost:8000 with `OPEN_MHS_URL`.

The adapter enforces nothing itself. Every safety decision is made by the middleware and
the driver, exactly as it is for a non-MCP caller; the adapter's contribution is that a
refusal arrives as an explanation the model can act on rather than a bare error code.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from open_mhs.mcp_adapter.client import (
    OpenMHSClient,
    OpenMHSUnreachable,
    RemoteRPCError,
    Unauthorized,
)
from open_mhs.mcp_adapter.formatting import (
    format_check,
    format_discovery,
    format_emergency_stop,
    format_estop_all,
    format_read,
    format_rpc_error,
    format_snapshot,
    format_unauthorized,
    format_unreachable,
    format_write,
)

INSTRUCTIONS = """\
Open-MHS exposes physical hardware — robotic arms, lab instruments, sensors — through two
primitives: read and write.

Always call discover_hardware first. It lists every device with its readable parameters,
its writable parameters, and the exact safety limits each write must respect. Acting
without it means guessing at bounds.

Every write is checked against limits the hardware itself declares, twice, before any byte
reaches the device. A rejected write changes nothing physically. When a write is refused,
the reply states the actual boundary and what a valid retry looks like — use it. Never try
to route around a limit, and never retry an identical command hoping for a different
result.

Some actuators require human confirmation. For those, ask the operator first, then re-send
with confirm=true. If a command reports a state desync, stop and re-read the device: your
model of the world and the world have diverged.

For anything involving more than one write or more than one device: snapshot_hardware
first, then check_hardware_plan with the whole plan, then execute the steps one at a time
with write_hardware_state, then snapshot_hardware again. If anything is wrong and you are
not sure which device, emergency_stop_all_hardware.
"""

mcp = FastMCP(
    "open-mhs-adapter",
    instructions=INSTRUCTIONS,
    dependencies=["httpx"],
)

_client: OpenMHSClient | None = None


def get_client() -> OpenMHSClient:
    """The adapter's connection to the middleware, created on first use.

    Credentials come from `OPEN_MHS_AUTH_TOKEN`, the same variable the middleware reads, so
    an MCP client config only has to set one secret.
    """
    global _client
    if _client is None:
        _client = OpenMHSClient(os.getenv("OPEN_MHS_URL"))
    return _client


def set_client(client: OpenMHSClient | None) -> None:
    """Replace the connection. Tests use this to point the adapter at an in-process app."""
    global _client
    _client = client


@mcp.tool(
    annotations=ToolAnnotations(
        title="Discover hardware",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
async def discover_hardware(device_type: str | None = None) -> str:
    """List every device registered with the Open-MHS middleware.

    Returns each device's readable parameters, its writable parameters, and the safety
    limits that govern every write. Call this before any read or write: it is the only way
    to know what exists and what bounds apply.

    Args:
        device_type: optional filter, e.g. "robotic_arm", "sensor_array",
            "thermal_controller". Omit to list everything.
    """
    try:
        return format_discovery(await get_client().discover(device_type))
    except Unauthorized as exc:
        return format_unauthorized(exc)
    except OpenMHSUnreachable as exc:
        return format_unreachable(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Read hardware state",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
async def read_hardware_state(device_id: str, parameter: str) -> str:
    """Read one sensor or actuator state from a device. Changes nothing.

    Args:
        device_id: device identifier from discover_hardware, e.g. "arm-01".
        parameter: the sensor or actuator id to read, e.g. "joint_1_actual".
    """
    try:
        result = await get_client().rpc(
            "mhs.read", {"device_id": device_id, "target": parameter}
        )
        return format_read(result)
    except RemoteRPCError as exc:
        return format_rpc_error(exc)
    except Unauthorized as exc:
        return format_unauthorized(exc)
    except OpenMHSUnreachable as exc:
        return format_unreachable(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Write hardware state",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def write_hardware_state(
    device_id: str,
    parameter: str,
    value: float | bool | str,
    confirm: bool = False,
) -> str:
    """Command one actuator. THIS MOVES PHYSICAL HARDWARE.

    The value is checked against the device's declared safety limits before anything is
    transmitted. If it falls outside them the command is refused and nothing moves; the
    reply then states the real boundary and what a valid retry looks like.

    Args:
        device_id: device identifier from discover_hardware, e.g. "arm-01".
        parameter: the actuator id to command, e.g. "joint_1". Sensors are never writable.
        value: the setpoint, in the unit discover_hardware reports for this actuator.
        confirm: set true only after a human has approved. Required for actuators marked
            "REQUIRES HUMAN CONFIRMATION"; ignored for all others.
    """
    try:
        result = await get_client().rpc(
            "mhs.write",
            {"device_id": device_id, "target": parameter, "value": value, "confirm": confirm},
        )
        return format_write(result)
    except RemoteRPCError as exc:
        return format_rpc_error(exc)
    except Unauthorized as exc:
        return format_unauthorized(exc)
    except OpenMHSUnreachable as exc:
        return format_unreachable(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Emergency stop",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def emergency_stop_hardware(device_id: str) -> str:
    """Drive a device to the safe state its own capability tag declares.

    Use when a device is behaving unexpectedly, a write reported a state desync, or an
    operator asks you to stop. This bypasses safety limits by design: the safe state is
    trusted, and a stop a limit could refuse would not be a stop.

    Args:
        device_id: device identifier from discover_hardware, e.g. "arm-01".
    """
    try:
        result = await get_client().rpc("mhs.emergency_stop", {"device_id": device_id})
        return format_emergency_stop(result)
    except RemoteRPCError as exc:
        return format_rpc_error(exc)
    except Unauthorized as exc:
        return format_unauthorized(exc)
    except OpenMHSUnreachable as exc:
        return format_unreachable(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Snapshot all hardware",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
async def snapshot_hardware(device_ids: list[str] | None = None) -> str:
    """Read every sensor and actuator of every device in one call. Changes nothing.

    Use this before planning a multi-device action, and again after executing one, so
    your model of the whole cell comes from the cell and not from memory. A channel that
    cannot be read is reported inline; the rest of the snapshot is still valid.

    Args:
        device_ids: optional subset of device ids. Omit for every registered device.
    """
    try:
        params = {"device_ids": device_ids} if device_ids else {}
        return format_snapshot(await get_client().rpc("mhs.snapshot", params))
    except RemoteRPCError as exc:
        return format_rpc_error(exc)
    except Unauthorized as exc:
        return format_unauthorized(exc)
    except OpenMHSUnreachable as exc:
        return format_unreachable(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Check a hardware plan (dry run)",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
async def check_hardware_plan(writes: list[dict]) -> str:
    """Dry-run a list of writes across any devices. NOTHING MOVES.

    Every item is evaluated against its device's current safety envelope exactly as
    write_hardware_state would evaluate it. Use this to validate a multi-step or
    multi-device plan before executing a single step of it. A rejected plan names every
    failing item and the real bound it violated.

    Args:
        writes: list of objects {"device_id": ..., "target": ..., "value": ...,
            "confirm": false}. Up to 100 items.
    """
    try:
        return format_check(await get_client().rpc("mhs.check", {"writes": writes}))
    except RemoteRPCError as exc:
        return format_rpc_error(exc)
    except Unauthorized as exc:
        return format_unauthorized(exc)
    except OpenMHSUnreachable as exc:
        return format_unreachable(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Emergency stop ALL hardware",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def emergency_stop_all_hardware() -> str:
    """Drive EVERY device to its declared safe state.

    Use when anything is wrong and you are not sure which device is responsible. Devices
    that declare no emergency stop are skipped and named. A device that fails to stop is
    reported and the loop continues to the next one.
    """
    try:
        return format_estop_all(await get_client().rpc("mhs.emergency_stop_all", {}))
    except RemoteRPCError as exc:
        return format_rpc_error(exc)
    except Unauthorized as exc:
        return format_unauthorized(exc)
    except OpenMHSUnreachable as exc:
        return format_unreachable(exc)


def main() -> None:
    """Entry point for `python -m open_mhs.mcp_adapter.server` and the `open-mhs-mcp` script."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
