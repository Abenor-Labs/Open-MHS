"""`BaseDevice` — the contract every Open-MHS driver implements.

A driver owns three responsibilities and no more:

1. Load and validate its own Capability Tag.
2. Re-check every write against that tag's `safety_limits` before transmitting. This is
   the second of the two independent enforcement points; the middleware already checked,
   and the driver checks again because a driver may also be used without the middleware.
3. Translate values to and from whatever the hardware speaks.

Subclasses override `encode` / `decode` and, if they need it, `on_transmit`. They do not
override `write`: the safety path is not a subclass's business.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from abc import ABC
from pathlib import Path
from typing import Any, Awaitable, Callable

from open_mhs.drivers.transport import InMemoryTransport, Transport, TransportError
from open_mhs.server import safety
from open_mhs.server.errors import HardwareExecutionError, InvalidParams, StateDesync
from open_mhs.server.models import Actuator, CapabilityTag, SafetyLimit, Sensor

log = logging.getLogger("open_mhs.driver")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOLERANCE = 1e-6

#: How often the feedback sensor is sampled while waiting for an actuator to settle.
VERIFY_POLL_S = 0.05

Sleeper = Callable[[float], Awaitable[None]]


class BaseDevice(ABC):
    """Abstract device exposing the two execution primitives plus an emergency stop."""

    def __init__(
        self,
        tag: CapabilityTag | dict[str, Any] | str | Path,
        transport: Transport | None = None,
        *,
        sleep: Sleeper | None = None,
    ) -> None:
        """
        Args:
            tag: a validated tag, a raw dict, or a path to a `.mhs` file.
            transport: the link. Defaults to an in-memory link seeded from the tag.
            sleep: settle-time waiter. Tests inject a no-op so a suite never waits on
                simulated hardware; production leaves it as `asyncio.sleep`.
        """
        self._tag = self._load_tag(tag)
        self._transport = transport if transport is not None else self._default_transport()
        self._sleep: Sleeper = sleep or asyncio.sleep
        self._last_write: dict[str, tuple[Any, float]] = {}

    # --- construction ---

    @staticmethod
    def _load_tag(tag: CapabilityTag | dict[str, Any] | str | Path) -> CapabilityTag:
        """Validate at load. A driver never runs against an unvalidated tag."""
        if isinstance(tag, CapabilityTag):
            return tag
        if isinstance(tag, (str, Path)):
            path = Path(tag)
            if not path.is_absolute():
                path = REPO_ROOT / path
            tag = json.loads(path.read_text(encoding="utf-8"))
        return CapabilityTag.model_validate(tag)

    def _default_transport(self) -> InMemoryTransport:
        """Seed a simulated link from the tag: actuators at their declared defaults."""
        state: dict[str, Any] = {}
        feedback_map: dict[str, str] = {}
        for act in self._tag.actuators:
            if act.default is not None:
                state[act.id] = act.default
            if act.feedback_sensor:
                feedback_map[act.id] = act.feedback_sensor
                if act.default is not None:
                    state[act.feedback_sensor] = act.default
        return InMemoryTransport(state, feedback_map=feedback_map)

    # --- introspection ---

    @property
    def tag(self) -> CapabilityTag:
        return self._tag

    @property
    def device_id(self) -> str:
        return self._tag.device_id

    @property
    def transport(self) -> Transport:
        return self._transport

    def sensor(self, target: str) -> Sensor | None:
        return self._tag.sensor_map.get(target)

    def actuator(self, target: str) -> Actuator | None:
        return self._tag.actuator_map.get(target)

    # --- translation hooks ---

    def encode(self, target: str, value: Any) -> Any:
        """Value -> whatever the hardware accepts. Default: pass through."""
        return value

    def decode(self, target: str, raw: Any) -> Any:
        """Whatever the hardware returned -> a value in the declared unit. Default: pass through."""
        return raw

    async def on_transmit(self, target: str, value: Any) -> None:
        """Hook for side effects a real device would have (heating, wear, counters)."""

    # --- primitives ---

    async def read(self, target: str) -> Any:
        """Read one declared channel.

        An undeclared target raises; it never returns None. A silent None would be
        indistinguishable from a genuine zero reading.
        """
        channel = self.sensor(target) or self.actuator(target)
        if channel is None:
            raise InvalidParams(
                f"{self.device_id}: no sensor or actuator named {target!r}",
                {"device_id": self.device_id, "target": target},
            )
        try:
            raw = await self._transport.acquire(target)
            # decode() is inside the guard on purpose: a reply the driver cannot parse is a
            # hardware-side failure too, and must not escape as a bare TransportError when
            # the driver is used without the middleware.
            return self.decode(target, raw)
        except TransportError as exc:
            raise HardwareExecutionError(
                f"{self.device_id}: {exc}", {"device_id": self.device_id, "target": target}
            ) from exc

    async def write(self, target: str, value: Any, *, confirmed: bool = False) -> dict[str, Any]:
        """Command one actuator. Enforcement point 2 of 2.

        Order matters: validate, check limits, and only then transmit. A rejected write
        emits nothing — `transport.writes` stays empty, which the test suite asserts
        directly rather than inferring from the return value.
        """
        if target in self._tag.sensor_map:
            raise InvalidParams(
                f"{self.device_id}: {target!r} is a sensor and is never writable",
                {"device_id": self.device_id, "target": target},
            )
        actuator = self.actuator(target)
        if actuator is None:
            raise InvalidParams(
                f"{self.device_id}: no actuator named {target!r}",
                {"device_id": self.device_id, "target": target,
                 "writable": sorted(self._tag.actuator_map)},
            )
        if actuator.requires_confirmation and not confirmed:
            raise InvalidParams(
                f"{self.device_id}: {target!r} requires explicit human confirmation",
                {"device_id": self.device_id, "target": target, "requires_confirmation": True},
            )

        limit = self._tag.limit_map[target]
        current, elapsed_s = self._rate_context(target, actuator)
        # Resolved here too, independently of the middleware. The two enforcement points
        # share one evaluator but never share a result: a driver reached directly, with no
        # middleware in front of it, must still honour a state-dependent envelope.
        state = await self._condition_state(limit)
        try:
            decision = safety.check_write(
                actuator, limit, value,
                current=current, elapsed_s=elapsed_s, device_id=self.device_id,
                state=state,
            )
        except safety.EmergencyStopRequired as exc:
            # Honour the tag even when this driver is used without the middleware.
            try:
                exc.data["emergency_stop"] = {"executed": True, **await self.emergency_stop()}
            except Exception as stop_exc:
                exc.data["emergency_stop"] = {
                    "executed": False, "error": f"{type(stop_exc).__name__}: {stop_exc}"
                }
            raise

        absolute = decision.value
        try:
            await self._transport.transmit(target, self.encode(target, absolute))
            await self.on_transmit(target, absolute)
        except TransportError as exc:
            raise HardwareExecutionError(
                f"{self.device_id}: {exc}", {"device_id": self.device_id, "target": target}
            ) from exc

        self._last_write[target] = (absolute, time.monotonic())
        try:
            verification = await self._verify(actuator, absolute)
        except StateDesync as exc:
            # The clamp is the more important fact and the desync must not hide it: a
            # caller told only "commanded 0.83, reads 0.91" believes it asked for 0.83.
            if decision.clamped:
                exc.data["clamped"] = True
                exc.data["requested"] = decision.original
                exc.data["clamp_reason"] = decision.reason
            raise
        result = {
            "driver": type(self).__name__,
            "written": absolute,
            "clamped": decision.clamped,
            **verification,
        }
        if decision.clamped:
            result["requested"] = decision.original
            result["clamp_reason"] = decision.reason
        return result

    async def emergency_stop(self) -> dict[str, Any]:
        """Drive every actuator to the tag's declared safe state.

        Bypasses limit checking on purpose: the safe state is trusted by definition, and a
        stop a limit could refuse is not a stop.
        """
        estop = self._tag.emergency_stop
        if estop is None or not estop.supported:
            raise HardwareExecutionError(
                f"{self.device_id}: device declares no emergency stop",
                {"device_id": self.device_id, "supported": False},
            )
        started = time.monotonic()
        applied: dict[str, Any] = {}
        for target, value in (estop.safe_state or {}).items():
            try:
                await self._transport.transmit(target, self.encode(target, value))
            except TransportError as exc:
                raise HardwareExecutionError(
                    f"{self.device_id}: emergency stop failed on {target!r}: {exc}",
                    {"device_id": self.device_id, "target": target, "partial": applied},
                ) from exc
            applied[target] = value
        self._last_write.clear()
        return {
            "stopped": True,
            "safe_state": applied,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "max_stop_time_ms": estop.max_stop_time_ms,
        }

    # --- internals ---

    async def _condition_state(self, limit: SafetyLimit) -> dict[str, Any] | None:
        """Read the channels a conditional bound depends on. None when it has none.

        An unreadable channel is omitted, never defaulted. The base bound is narrower than
        any condition by construction, so a failed read tightens the envelope rather than
        opening it.
        """
        targets = safety.condition_targets(limit)
        if not targets:
            return None
        state: dict[str, Any] = {}
        for target in targets:
            try:
                state[target] = await self.read(target)
            except Exception:
                log.warning(
                    "%s: could not read %r for the conditional limit on %s; using the "
                    "base bound", self.device_id, target, limit.target,
                )
        return state

    def _rate_context(self, target: str, actuator: Actuator) -> tuple[Any, float | None]:
        previous = self._last_write.get(target)
        if previous is None:
            return (actuator.default, None)
        value, at = previous
        return (value, max(time.monotonic() - at, 1e-9))

    async def _verify(self, actuator: Actuator, commanded: Any) -> dict[str, Any]:
        """Closed-loop check: did the hardware actually go where it was told?

        Without this a driver reports success for a stuck axis, and the agent proceeds on a
        belief about the world that is simply false.
        """
        if not actuator.feedback_sensor:
            return {"verified": False, "reason": "actuator declares no feedback_sensor"}

        sensor = self.sensor(actuator.feedback_sensor)
        tolerance = sensor.accuracy if sensor and sensor.accuracy else DEFAULT_TOLERANCE
        numeric = isinstance(commanded, (int, float)) and not isinstance(commanded, bool)

        def settled(observed: Any) -> bool:
            if numeric:
                return isinstance(observed, (int, float)) and abs(observed - commanded) <= tolerance
            return observed == commanded

        # `settle_time_ms` is the LONGEST the actuator may take, not a fixed wait. The
        # sensor is polled and verification returns the moment it agrees, so a short move
        # is confirmed quickly and a long one is given the whole budget. A single blind
        # read after a fixed sleep did the wrong thing in both directions: it stalled on
        # moves that had already finished, and on a full-span move it sampled the sensor
        # mid-travel and reported a desync for hardware that was doing exactly as told.
        budget_s = (actuator.settle_time_ms or 0) / 1000
        polls = max(1, math.ceil(budget_s / VERIFY_POLL_S)) if budget_s else 1
        interval = budget_s / polls if budget_s else 0.0
        started = time.monotonic()
        observed = await self.read(actuator.feedback_sensor)
        for _ in range(polls):
            if settled(observed):
                break
            if interval:
                await self._sleep(interval)
            observed = await self.read(actuator.feedback_sensor)
        waited_ms = round((time.monotonic() - started) * 1000, 1)

        if not settled(observed):
            raise StateDesync(
                f"{self.device_id}: {actuator.id} was commanded to {commanded!r} but "
                f"{actuator.feedback_sensor} reads {observed!r} after "
                f"{actuator.settle_time_ms or 0} ms",
                {
                    "device_id": self.device_id,
                    "target": actuator.id,
                    "commanded": commanded,
                    "observed": observed,
                    "feedback_sensor": actuator.feedback_sensor,
                    "tolerance": tolerance,
                    "settle_time_ms": actuator.settle_time_ms,
                    "waited_ms": waited_ms,
                },
            )
        return {
            "verified": True,
            "observed": observed,
            "feedback_sensor": actuator.feedback_sensor,
            "settled_ms": waited_ms,
        }

    # --- discovery ---

    async def register(
        self, client: Any, registry_url: str | None = None, token: str | None = None
    ) -> dict[str, Any]:
        """Announce this device to a Discovery Layer registry.

        `client` is any httpx-compatible async client, so a test can register through an
        ASGI transport without opening a socket.

        Args:
            token: middleware API token. Defaults to `$OPEN_MHS_AUTH_TOKEN`. Registration is
                authenticated because a capability tag declares its own safety limits, and
                an anonymous caller must not be able to publish one.
        """
        base = registry_url or (self._tag.discovery.registry_url if self._tag.discovery else None)
        url = f"{base.rstrip('/')}/register" if base else "/register"
        token = token if token is not None else os.getenv("OPEN_MHS_AUTH_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = await client.post(
            url,
            json=self._tag.model_dump(mode="json", exclude_none=True),
            headers=headers,
        )
        response.raise_for_status()
        return response.json()
