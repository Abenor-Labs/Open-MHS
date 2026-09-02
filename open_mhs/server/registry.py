"""In-memory device registry — the Discovery Layer's single source of truth.

The registry owns the authoritative copy of every device's Capability Tag. The RPC
dispatcher evaluates safety limits against *this* copy, never against anything a caller
supplies in a request, so a device cannot widen its own envelope at call time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from open_mhs.server.errors import DeviceNotFound
from open_mhs.server.models import CapabilityTag, DeviceSummary

DEFAULT_HEARTBEAT_S = 30.0
STALE_AFTER_MISSED_BEATS = 2


@runtime_checkable
class DeviceDriver(Protocol):
    """What the middleware requires of a driver.

    Structural, not inherited: `server` never imports `drivers`, so a third-party driver
    package can satisfy this without depending on this repo's base class.
    """

    async def read(self, target: str) -> Any: ...

    async def write(self, target: str, value: Any, *, confirmed: bool = False) -> dict[str, Any]: ...

    async def emergency_stop(self) -> dict[str, Any]: ...


@dataclass
class DeviceRecord:
    tag: CapabilityTag
    driver: DeviceDriver | None = None
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    last_write: dict[str, tuple[Any, float]] = field(default_factory=dict)
    """target -> (last accepted absolute value, monotonic timestamp).

    Held by the registry, not the driver, so the middleware can enforce `max_rate` even for
    a device whose driver lives in another process.
    """

    @property
    def device_id(self) -> str:
        return self.tag.device_id

    @property
    def heartbeat_interval_s(self) -> float:
        return self.tag.discovery.heartbeat_interval_s if self.tag.discovery else DEFAULT_HEARTBEAT_S

    def is_online(self, now: float | None = None) -> bool:
        """A locally-driven device is online by construction; it has no network to miss."""
        if self.driver is not None:
            return True
        now = time.time() if now is None else now
        return (now - self.last_seen) <= self.heartbeat_interval_s * STALE_AFTER_MISSED_BEATS

    def to_summary(self) -> DeviceSummary:
        return DeviceSummary(
            device_id=self.device_id,
            name=self.tag.name,
            type=self.tag.type,
            online=self.is_online(),
            has_local_driver=self.driver is not None,
            registered_at=self.registered_at,
            last_seen=self.last_seen,
            capability_tag=self.tag,
        )


class Registry:
    """Devices known to this middleware instance.

    Not persisted: a restart forgets every device, and every device must re-announce. That
    is deliberate. A registry that survives a restart can hand an agent a tag for hardware
    that is no longer plugged in.
    """

    def __init__(self) -> None:
        self._devices: dict[str, DeviceRecord] = {}

    def __len__(self) -> int:
        return len(self._devices)

    def __contains__(self, device_id: object) -> bool:
        return device_id in self._devices

    def register(self, tag: CapabilityTag, driver: DeviceDriver | None = None) -> DeviceRecord:
        """Add or replace a device. Re-registration keeps the original `registered_at`."""
        existing = self._devices.get(tag.device_id)
        record = DeviceRecord(
            tag=tag,
            driver=driver if driver is not None else (existing.driver if existing else None),
            registered_at=existing.registered_at if existing else time.time(),
        )
        self._devices[tag.device_id] = record
        return record

    def get(self, device_id: str) -> DeviceRecord:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFound(device_id, sorted(self._devices)) from None

    def deregister(self, device_id: str) -> DeviceRecord:
        record = self.get(device_id)
        del self._devices[device_id]
        return record

    def heartbeat(self, device_id: str) -> DeviceRecord:
        record = self.get(device_id)
        record.last_seen = time.time()
        return record

    def list(
        self, *, device_type: str | None = None, online_only: bool = False
    ) -> list[DeviceRecord]:
        records = [
            r for r in self._devices.values()
            if (device_type is None or r.tag.type == device_type)
            and (not online_only or r.is_online())
        ]
        return sorted(records, key=lambda r: r.device_id)

    def clear(self) -> None:
        self._devices.clear()
