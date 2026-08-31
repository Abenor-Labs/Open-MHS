"""Two-axis arm driven over a real serial port with Marlin-style G-code.

Same `BaseDevice` as the mock arm, same two safety checks, same capability tag rules — the
only difference is that `encode` produces G-code and the transport puts it on a wire.

    write("joint_1", 45.0)  ->  safety check  ->  "G1 X45.000 F1800"  ->  /dev/ttyUSB0

The safety envelope matters more here, not less. Marlin will accept `G1 X500` and drive the
axis into the frame; the firmware has no opinion about it. The capability tag's bound is
the only thing standing in the way, which is why `enforcement` on those limits honestly
says `software` rather than claiming `firmware`.

Run it against real hardware:

    python -m drivers.serial_robotic_arm --port /dev/ttyUSB0 --registry http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import asyncio
import re
from typing import Any

from drivers.base import BaseDevice
from drivers.serial_transport import SerialTransport
from drivers.transport import TransportError

TAG_PATH = "examples/serial_arm.mhs"

#: capability-tag id -> G-code axis letter
AXIS = {"joint_1": "X", "joint_2": "Y"}
FEEDBACK_AXIS = {"joint_1_actual": "X", "joint_2_actual": "Y"}

#: Which command reads which sensor. M114 reports position, M105 reports temperature.
QUERY_MAP = {
    "joint_1_actual": "M114",
    "joint_2_actual": "M114",
    "motor_temp": "M105",
}

GRIPPER_CODES = {"closed": "M3", "open": "M5"}

_AXIS_RE = re.compile(r"\b([XY]):(-?\d+(?:\.\d+)?)")
_TEMP_RE = re.compile(r"\bT:(-?\d+(?:\.\d+)?)")


class SerialRoboticArm(BaseDevice):
    """G-code arm on a serial link.

    Args:
        port: serial port name. Ignored when an explicit transport is supplied.
        baudrate: must match the firmware.
        feedrate: F word attached to every move, in axis units per minute. Derived from the
            tag's `max_rate` when not given, so the firmware is asked to move no faster
            than the capability tag permits — belt and braces with the software rate check.
    """

    def __init__(
        self,
        tag: Any = TAG_PATH,
        transport: SerialTransport | None = None,
        *,
        port: str | None = None,
        baudrate: int = 115200,
        feedrate: float | None = None,
        **kwargs: Any,
    ) -> None:
        if transport is None:
            resolved = port or self._address_from_tag(tag)
            transport = SerialTransport(resolved, baudrate=baudrate, query_map=QUERY_MAP)
        elif isinstance(transport, SerialTransport) and not transport.query_map:
            # Which command reads which sensor is protocol knowledge, and protocol knowledge
            # belongs to the device. A caller who supplies a bare port should not have to
            # know that positions come from M114.
            transport.query_map.update(QUERY_MAP)
        super().__init__(tag, transport, **kwargs)
        self._feedrate = feedrate if feedrate is not None else self._feedrate_from_tag()

    @staticmethod
    def _address_from_tag(tag: Any) -> str:
        """Fall back to the port the capability tag itself declares."""
        loaded = BaseDevice._load_tag(tag)
        if loaded.driver and loaded.driver.address:
            return loaded.driver.address
        raise ValueError(
            "no serial port given and the capability tag declares no driver.address"
        )

    def _feedrate_from_tag(self) -> float:
        """Slowest declared max_rate, converted from units/second to G-code units/minute."""
        rates = [
            limit.max_rate for limit in self._tag.safety_limits if limit.max_rate is not None
        ]
        return min(rates) * 60.0 if rates else 600.0

    # --- protocol ---

    def encode(self, target: str, value: Any) -> str:
        """Capability-tag value -> one line of G-code."""
        if target in AXIS:
            return f"G1 {AXIS[target]}{float(value):.3f} F{self._feedrate:.0f}"
        if target == "gripper":
            code = GRIPPER_CODES.get(str(value))
            if code is None:
                raise TransportError(f"no G-code mapping for gripper state {value!r}")
            return code
        raise TransportError(f"no G-code mapping for target {target!r}")

    def decode(self, target: str, raw: Any) -> Any:
        """One raw response line -> a value in the unit the tag declares.

        `M114` answers for both axes at once, so the same reply is parsed differently
        depending on which sensor asked.
        """
        text = str(raw)
        if target in FEEDBACK_AXIS:
            # Marlin appends stepper counts after the word "Count": `X:12.5 Y:-7.2 Count
            # X:1000 Y:-580`. Those are steps, not degrees, and parsing them as position
            # reads 1000 deg for an axis sitting at 12.5.
            head = text.split("Count", 1)[0]
            found = dict(_AXIS_RE.findall(head))
            axis = FEEDBACK_AXIS[target]
            if axis not in found:
                raise TransportError(
                    f"no {axis} axis in position report {text!r} (expected M114 output)"
                )
            return round(float(found[axis]), 3)
        if target == "motor_temp":
            match = _TEMP_RE.search(text)
            if match is None:
                raise TransportError(
                    f"no temperature in report {text!r} (expected M105 output)"
                )
            return round(float(match.group(1)), 2)
        return raw

    async def emergency_stop(self) -> dict[str, Any]:
        """Send M112 first, then drive the declared safe state.

        M112 halts motion inside the firmware immediately. Homing and repositioning
        afterwards is what the tag's `safe_state` describes, and that still runs so the
        device is left where the tag says it should be.
        """
        transport = self._transport
        halted = False
        if isinstance(transport, SerialTransport):
            try:
                await transport.write_line("M112")
                halted = True
            except TransportError:
                # A failed halt must not prevent the safe-state run below.
                halted = False
        result = await super().emergency_stop()
        return {**result, "firmware_halt_sent": halted}

    # --- lifecycle ---

    async def connect(self) -> None:
        if isinstance(self._transport, SerialTransport):
            await self._transport.open()

    async def disconnect(self) -> None:
        if isinstance(self._transport, SerialTransport):
            await self._transport.close()


async def _main() -> None:
    import httpx

    parser = argparse.ArgumentParser(description="Run the Open-MHS G-code serial arm driver.")
    parser.add_argument("--port", required=True, help="Serial port, e.g. /dev/ttyUSB0 or COM3")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--registry", default=None, help="Registry base URL")
    args = parser.parse_args()

    device = SerialRoboticArm(port=args.port, baudrate=args.baudrate)
    await device.connect()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            print(f"registered: {await device.register(client, args.registry)}")
            for sensor in ("joint_1_actual", "joint_2_actual", "motor_temp"):
                print(f"{sensor}: {await device.read(sensor)}")
    finally:
        await device.disconnect()


if __name__ == "__main__":
    asyncio.run(_main())
