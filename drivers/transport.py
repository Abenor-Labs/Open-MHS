"""Transport layer: the lowest rung of a driver, and the only thing tests replace.

A test that mocks a driver proves nothing about the driver. Substituting the transport
instead lets the real driver logic, the real safety clamp, and the real registry run
against a fake link:

    test -> real /rpc route -> real driver class -> FAKE transport

`InMemoryTransport` records every transmission, which is what makes "the write was
rejected AND nothing was sent" an assertable claim rather than an assumption.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class TransportError(Exception):
    """The physical link failed. Surfaces to an agent as -32002."""


class Transport(ABC):
    """Byte-level link to a device. Knows nothing about limits or capability tags."""

    @abstractmethod
    async def acquire(self, target: str) -> Any:
        """Fetch the raw value of one channel."""

    @abstractmethod
    async def transmit(self, target: str, value: Any) -> None:
        """Push one encoded value at the hardware."""


class InMemoryTransport(Transport):
    """Simulated link with fault injection, for drivers with no hardware attached.

    Args:
        state: initial channel values, mutated in place as writes land.
        feedback_map: actuator id -> the sensor id that reports its achieved state.
        fail_on: channels whose access raises `TransportError` (simulates a dead link).
        ignore_writes: channels that accept a write but never move (simulates a stuck
            axis, so the state-desync path can be tested).
    """

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        *,
        feedback_map: dict[str, str] | None = None,
        fail_on: set[str] | None = None,
        ignore_writes: set[str] | None = None,
    ) -> None:
        self.state: dict[str, Any] = dict(state or {})
        self.feedback_map: dict[str, str] = dict(feedback_map or {})
        self.fail_on: set[str] = set(fail_on or ())
        self.ignore_writes: set[str] = set(ignore_writes or ())
        self.writes: list[tuple[str, Any]] = []
        self.reads: list[str] = []

    # --- link ---

    async def acquire(self, target: str) -> Any:
        if target in self.fail_on:
            raise TransportError(f"link failure reading {target!r}")
        self.reads.append(target)
        if target not in self.state:
            raise TransportError(f"channel {target!r} is not present on this link")
        return self.state[target]

    async def transmit(self, target: str, value: Any) -> None:
        if target in self.fail_on:
            raise TransportError(f"link failure writing {target!r}")
        self.writes.append((target, value))
        if target in self.ignore_writes:
            return  # accepted on the wire, never actually moved
        self.state[target] = value
        feedback = self.feedback_map.get(target)
        if feedback is not None:
            self.state[feedback] = value

    # --- test affordances ---

    @property
    def written_targets(self) -> list[str]:
        return [t for t, _ in self.writes]

    def snapshot(self) -> dict[str, Any]:
        """Copy of device state, for asserting that a rejected write changed nothing."""
        return dict(self.state)
