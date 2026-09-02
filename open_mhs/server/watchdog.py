"""`max_duration_s` enforcement — the dead-man timer.

A limit may say an actuator can be held away from its default for at most N seconds. The
schema has always said so; until now the middleware parsed it and did nothing. An
unenforced safety field is worse than an absent one, so this module enforces it.

One timer per (device, target). Started on an accepted write of a non-default value,
restarted by a newer write, cancelled by an emergency stop. On expiry the default is
written through the normal safety path; if that is refused, or there is no default, the
device's emergency stop runs. Either way an audit line is written.

Middleware-only. A driver used without the middleware has no watchdog.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from open_mhs.server.audit import AuditLog
from open_mhs.server.errors import MHSError
from open_mhs.server.models import Actuator, SafetyLimit
from open_mhs.server.registry import DeviceRecord

log = logging.getLogger("open_mhs.watchdog")


class Watchdog:
    def __init__(self, audit: AuditLog) -> None:
        self._audit = audit
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    def arm(
        self, record: DeviceRecord, actuator: Actuator, limit: SafetyLimit, value: Any
    ) -> None:
        """Start or restart the timer for one target. No-op when the limit has none."""
        key = (record.device_id, actuator.id)
        self.cancel(record.device_id, actuator.id)
        if limit.max_duration_s is None or value == actuator.default:
            return
        self._tasks[key] = asyncio.create_task(
            self._expire(record, actuator, limit, value), name=f"watchdog:{key[0]}.{key[1]}"
        )

    def cancel(self, device_id: str, target: str | None = None) -> None:
        """Cancel one target's timer, or every timer for a device."""
        for key in list(self._tasks):
            if key[0] == device_id and (target is None or key[1] == target):
                self._tasks.pop(key).cancel()

    def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    async def _expire(
        self, record: DeviceRecord, actuator: Actuator, limit: SafetyLimit, held: Any
    ) -> None:
        assert limit.max_duration_s is not None
        await asyncio.sleep(limit.max_duration_s)
        self._tasks.pop((record.device_id, actuator.id), None)
        driver = record.driver
        outcome: dict[str, Any] = {
            "held": held, "max_duration_s": limit.max_duration_s, "returned_to": None,
        }
        if driver is None:
            outcome["error"] = "no driver bound"
        elif actuator.default is not None:
            try:
                # Returning to the declared default is the tag's own instruction, not an
                # agent's request, so the human-confirmation gate does not apply.
                await driver.write(actuator.id, actuator.default, confirmed=True)
                record.last_write[actuator.id] = (actuator.default, time.monotonic())
                outcome["returned_to"] = actuator.default
            except MHSError as exc:
                outcome["error"] = exc.to_rpc()
            except Exception as exc:  # noqa: BLE001 - must still reach the e-stop below
                outcome["error"] = f"{type(exc).__name__}: {exc}"
        else:
            outcome["error"] = "actuator declares no default"
        if outcome["returned_to"] is None and driver is not None:
            try:
                stopped = await driver.emergency_stop()
                record.last_write.clear()
                outcome["emergency_stop"] = {"executed": True, **stopped}
            except Exception as exc:  # noqa: BLE001 - report, never raise from a task
                outcome["emergency_stop"] = {
                    "executed": False, "error": f"{type(exc).__name__}: {exc}"
                }
        log.warning("DURATION-EXPIRED %s.%s: %s", record.device_id, actuator.id, outcome)
        self._audit.record(
            "duration.expired", device_id=record.device_id, target=actuator.id,
            params={}, outcome=outcome,
        )
