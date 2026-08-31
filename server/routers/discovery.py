"""Discovery Layer: how hardware announces itself and how agents find it.

Nothing here mutates hardware. Registration accepts a Capability Tag, validates it at
ingestion (enforcement point 1), and stores it. Every physical state change goes through
the RPC dispatcher instead, so there is exactly one surface to audit.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from server.deps import get_registry
from server.models import (
    CapabilityTag,
    DeregisterResponse,
    DeviceSummary,
    DiscoverResponse,
    HeartbeatResponse,
    RegisterResponse,
)
from server.registry import Registry

router = APIRouter(tags=["discovery"])


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    tag: CapabilityTag,
    registry: Registry = Depends(get_registry),
) -> RegisterResponse:
    """Announce a device.

    The body is a full Capability Tag. FastAPI rejects a malformed one with 422 before this
    handler runs — including the cross-field rules (limit coverage, unit agreement, id
    uniqueness) that `CapabilityTag` enforces beyond the JSON Schema shape.
    """
    replaced = tag.device_id in registry
    record = registry.register(tag)
    return RegisterResponse(
        registered=True,
        device_id=record.device_id,
        heartbeat_interval_s=record.heartbeat_interval_s,
        message=(
            f"Re-registered {record.device_id!r}; previous capability tag replaced"
            if replaced
            else f"Registered {record.device_id!r} with "
            f"{len(tag.sensors)} sensor(s), {len(tag.actuators)} actuator(s)"
        ),
    )


@router.get("/discover", response_model=DiscoverResponse)
async def discover(
    type: str | None = None,
    online_only: bool = False,
    registry: Registry = Depends(get_registry),
) -> DiscoverResponse:
    """List connected devices, each with its full Capability Tag.

    The tag ships inline rather than behind a second call: an agent that must fetch
    capabilities separately will make its first decision without them.
    """
    records = registry.list(device_type=type, online_only=online_only)
    return DiscoverResponse(count=len(records), devices=[r.to_summary() for r in records])


@router.get("/devices/{device_id}", response_model=DeviceSummary)
async def get_device(
    device_id: str,
    registry: Registry = Depends(get_registry),
) -> DeviceSummary:
    """Fetch one device. Raises -32000 as a 404 via the app's MHSError handler."""
    return registry.get(device_id).to_summary()


@router.post("/devices/{device_id}/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    device_id: str,
    registry: Registry = Depends(get_registry),
) -> HeartbeatResponse:
    """Keepalive. A device is marked stale after two missed intervals."""
    record = registry.heartbeat(device_id)
    return HeartbeatResponse(device_id=record.device_id, last_seen=record.last_seen)


@router.delete("/devices/{device_id}", response_model=DeregisterResponse)
async def deregister(
    device_id: str,
    registry: Registry = Depends(get_registry),
) -> DeregisterResponse:
    """Remove a device. Its driver is not stopped; unplugging is the driver's business."""
    record = registry.deregister(device_id)
    return DeregisterResponse(deregistered=True, device_id=record.device_id)
