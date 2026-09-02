"""Serial-port transport — the first link in this repo that touches real hardware.

`InMemoryTransport` proves the safety logic. This proves the safety logic is worth having:
the same `BaseDevice.write` path, the same two limit checks, but the bytes now leave the
machine over a UART.

pyserial is blocking, and every handler above this is async, so every port call is pushed
to a worker thread with `anyio.to_thread.run_sync`. Blocking the event loop inside a driver
would stall every other device the middleware is holding.

The port is opened lazily on first use and can be closed and reopened; a device that is
unplugged and replugged does not require restarting the middleware.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any, Callable, Protocol

import anyio

from open_mhs.drivers.transport import Transport, TransportError

log = logging.getLogger("open_mhs.serial")

DEFAULT_BAUDRATE = 115200
DEFAULT_TIMEOUT_S = 2.0


class SerialLike(Protocol):
    """The slice of pyserial's `Serial` this transport actually uses.

    Declared as a protocol so a test can substitute a recording fake without pyserial
    installed, and so a non-pyserial backend (a socket bridge, a USB HID shim) can be
    dropped in without touching this class.
    """

    is_open: bool

    def write(self, data: bytes) -> int | None: ...

    def readline(self) -> bytes: ...

    def reset_input_buffer(self) -> None: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[], SerialLike]


class SerialTransport(Transport):
    """Line-oriented serial link for text protocols such as G-code.

    Knows nothing about capability tags, limits, or units. It moves lines of text; the
    device class decides what those lines mean. That split is what lets the driver be
    tested against a fake link while the protocol logic stays real.

    Args:
        port: OS port name — `COM3` on Windows, `/dev/ttyUSB0` or `/dev/ttyACM0` elsewhere.
        baudrate: line speed. Must match the firmware.
        timeout_s: per-read timeout. A read that times out raises `TransportError` rather
            than blocking a handler forever.
        eol: line terminator the firmware expects.
        encoding: text encoding for the line protocol.
        expect_ack: reply that means "command accepted", e.g. `ok` for Marlin/GRBL. Set to
            None for firmware that does not acknowledge.
        query_map: sensor id -> the command that reads it. A sensor with no entry cannot be
            read, and says so rather than returning a fabricated value.
        connection_factory: builds the underlying port. Defaults to pyserial. Injected by
            tests, and by anyone bridging a non-serial link.
    """

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        eol: str = "\n",
        encoding: str = "ascii",
        expect_ack: str | None = "ok",
        query_map: dict[str, str] | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self.eol = eol
        self.encoding = encoding
        self.expect_ack = expect_ack
        self.query_map = dict(query_map or {})
        self._factory = connection_factory or self._pyserial_factory
        self._conn: SerialLike | None = None

    # --- connection lifecycle ---

    def _pyserial_factory(self) -> SerialLike:
        """Import pyserial only when a real port is actually wanted."""
        try:
            import serial  # noqa: PLC0415 - optional dependency, imported on demand
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise TransportError(
                "pyserial is not installed; `pip install pyserial` to drive a real port"
            ) from exc
        try:
            return serial.Serial(
                port=self.port, baudrate=self.baudrate, timeout=self.timeout_s
            )
        except Exception as exc:  # noqa: BLE001 - pyserial raises several unrelated types
            raise TransportError(f"cannot open {self.port}: {exc}") from exc

    @property
    def is_open(self) -> bool:
        return self._conn is not None and getattr(self._conn, "is_open", True)

    async def open(self) -> None:
        """Open the port. Idempotent."""
        if self.is_open:
            return
        self._conn = await anyio.to_thread.run_sync(self._factory)
        log.info("opened %s at %s baud", self.port, self.baudrate)

    async def close(self) -> None:
        """Close the port. Safe to call when already closed."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            await anyio.to_thread.run_sync(conn.close)
        except Exception as exc:  # noqa: BLE001 - closing must not raise into a handler
            log.warning("error closing %s: %s", self.port, exc)

    async def __aenter__(self) -> SerialTransport:
        await self.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _connection(self) -> SerialLike:
        if not self.is_open:
            await self.open()
        assert self._conn is not None
        return self._conn

    # --- raw byte level ---

    async def write_bytes(self, data: bytes) -> None:
        conn = await self._connection()
        try:
            await anyio.to_thread.run_sync(partial(conn.write, data))
        except Exception as exc:  # noqa: BLE001 - any port failure is a transport failure
            raise TransportError(f"{self.port}: write failed: {exc}") from exc

    async def read_bytes(self) -> bytes:
        """One line, up to and including `eol`. Empty means the read timed out."""
        conn = await self._connection()
        try:
            return await anyio.to_thread.run_sync(conn.readline)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"{self.port}: read failed: {exc}") from exc

    # --- line level ---

    async def write_line(self, line: str) -> None:
        await self.write_bytes((line + self.eol).encode(self.encoding))

    async def read_line(self) -> str:
        raw = await self.read_bytes()
        if not raw:
            raise TransportError(
                f"{self.port}: no response within {self.timeout_s}s - is the device powered "
                "and is the baud rate correct?"
            )
        return raw.decode(self.encoding, errors="replace").strip()

    async def command(self, line: str) -> str:
        """Send one command and return the first response line.

        When `expect_ack` is set, a reply that is not the ack is treated as a device-side
        error and raised, so a firmware refusal never reads as success upstream.
        """
        await self.write_line(line)
        if self.expect_ack is None:
            return ""
        reply = await self.read_line()
        if reply.lower().startswith(self.expect_ack.lower()):
            return reply
        raise TransportError(f"{self.port}: device rejected {line!r} with {reply!r}")

    # --- Transport interface ---

    async def transmit(self, target: str, value: Any) -> None:
        """Send an already-encoded command line.

        `value` is whatever the device's `encode` produced — for a G-code arm, a string
        like `G1 X45.000 F1800`. This class does not build protocol strings itself.
        """
        if not isinstance(value, str):
            raise TransportError(
                f"{self.port}: SerialTransport transmits text lines, got "
                f"{type(value).__name__} for {target!r}. The device's encode() must return "
                "a command string."
            )
        await self.command(value)

    async def acquire(self, target: str) -> str:
        """Send this target's query command and return the raw response line.

        Parsing belongs to the device: the same `M114` reply serves several sensors, and
        only the device knows which field is which.
        """
        query = self.query_map.get(target)
        if query is None:
            raise TransportError(
                f"{self.port}: no query command is mapped for {target!r}; this link cannot "
                "read it"
            )
        conn = await self._connection()
        try:
            await anyio.to_thread.run_sync(conn.reset_input_buffer)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"{self.port}: cannot flush input: {exc}") from exc
        await self.write_line(query)
        return await self.read_line()
