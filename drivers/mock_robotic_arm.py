"""Mock two-axis robotic arm — an actuating device with real state tracking.

Exercises everything the temperature sensor cannot: multi-axis limits, a rate cap, a
discrete (enum) limit, a confirmation-gated actuator, closed-loop feedback verification,
and an emergency stop with a declared safe state.

Physical side effects are simulated in `on_transmit` so the device has state that evolves
with use — motor temperature rises with travel and bleeds off between commands, and the
e-stop flag clears the moment a joint moves again.
"""

from __future__ import annotations

from typing import Any

from drivers.base import BaseDevice
from drivers.transport import InMemoryTransport

TAG_PATH = "examples/robotic_arm.mhs"

AMBIENT_C = 24.0
HEATING_C_PER_DEG = 0.08
COOLING_FRACTION = 0.05


class MockRoboticArm(BaseDevice):
    """Simulated arm: two joints with feedback sensors, plus a binary gripper.

    Joint travel is tracked so that `joint_1_actual` and `joint_2_actual` genuinely follow
    what was commanded. A test can therefore distinguish "the write was accepted" from
    "the hardware moved", which is the whole point of the state-desync error.
    """

    def __init__(
        self,
        tag: Any = TAG_PATH,
        transport: InMemoryTransport | None = None,
        **kwargs: Any,
    ) -> None:
        transport = transport or InMemoryTransport(
            {
                "joint_1": 0.0,
                "joint_2": 0.0,
                "gripper": "open",
                "joint_1_actual": 0.0,
                "joint_2_actual": 0.0,
                "motor_temp": AMBIENT_C,
                "estop_engaged": False,
            },
            feedback_map={"joint_1": "joint_1_actual", "joint_2": "joint_2_actual"},
        )
        super().__init__(tag, transport, **kwargs)

    @property
    def _state(self) -> dict[str, Any]:
        """Direct state access, valid only for the simulated link."""
        if not isinstance(self._transport, InMemoryTransport):
            raise TypeError("MockRoboticArm requires an InMemoryTransport")
        return self._transport.state

    async def on_transmit(self, target: str, value: Any) -> None:
        """Simulate the physical consequences of a commanded move."""
        state = self._state
        if target in {"joint_1", "joint_2"}:
            previous, _ = self._last_write.get(target, (0.0, 0.0))
            travel = abs(float(value) - float(previous or 0.0))
            heat = travel * HEATING_C_PER_DEG
            cooled = state["motor_temp"] - (state["motor_temp"] - AMBIENT_C) * COOLING_FRACTION
            state["motor_temp"] = round(cooled + heat, 2)
            state["estop_engaged"] = False

    async def emergency_stop(self) -> dict[str, Any]:
        """Run the declared safe state, then latch the e-stop flag."""
        result = await super().emergency_stop()
        self._state["estop_engaged"] = True
        return {**result, "estop_engaged": True}

    def decode(self, target: str, raw: Any) -> Any:
        if target in {"joint_1_actual", "joint_2_actual"}:
            return round(float(raw), 3)
        return raw
