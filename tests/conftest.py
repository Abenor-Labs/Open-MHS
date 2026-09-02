"""Shared fixtures: mock hardware with nothing plugged in.

The suite substitutes the **transport** and nothing above it. Every test drives the real
router, the real driver class, the real safety evaluation and the real registry — only the
byte-level link is fake:

    test -> real /rpc route -> real driver class -> FAKE transport

Fixtures are function-scoped and build fresh devices each time. A mock that accumulated
state between tests would produce order-dependent passes, which is the worst kind of green.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx
import pytest
import pytest_asyncio

from drivers.base import BaseDevice
from drivers.mock_robotic_arm import MockRoboticArm
from drivers.mock_temp_sensor import MockTempSensor
from drivers.transport import InMemoryTransport
from server.main import create_app
from server.registry import Registry

#: Token used by every test app. Long enough to satisfy the production minimum, so the
#: suite exercises the same code path a real deployment does.
TEST_TOKEN = "test-token-not-a-real-secret-0123456789"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}"}

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _audit_log_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test writes an audit file into the working tree unless it asks for one."""
    monkeypatch.setenv("OPEN_MHS_AUDIT_LOG", "off")


def load_tag(path: Path) -> dict[str, Any]:
    """Load a real tag from disk. Never inline a capability tag in a test."""
    return json.loads(path.read_text(encoding="utf-8"))


async def _no_sleep(_seconds: float) -> None:
    """Settle-time waiter for tests. Never wait on simulated hardware."""
    return None


class GripperDevice(BaseDevice):
    """Minimal concrete driver for the restricted-gripper fixture."""


class ClampingHeater(BaseDevice):
    """Concrete driver for the clamping-heater fixture."""


class EstopPump(BaseDevice):
    """Concrete driver for the stop-on-violation pump fixture."""


class NaiveDriver(BaseDevice):
    """A driver that enforces NOTHING - it transmits whatever it is handed.

    Stands in for a careless or malicious third-party driver. The middleware must reject an
    out-of-bounds command before this class ever sees it, which is the entire reason the
    safety check exists at two independent points rather than one.
    """

    async def write(self, target: str, value: Any, *, confirmed: bool = False) -> dict[str, Any]:
        await self._transport.transmit(target, value)
        return {"driver": "NaiveDriver", "written": value, "verified": False}


# --------------------------------------------------------------------------------------
# Capability tags
# --------------------------------------------------------------------------------------


@pytest.fixture
def temp_tag() -> dict[str, Any]:
    return load_tag(EXAMPLES / "mock_temp_sensor.mhs")


@pytest.fixture
def arm_tag() -> dict[str, Any]:
    return load_tag(EXAMPLES / "robotic_arm.mhs")


@pytest.fixture
def unbounded_tag() -> dict[str, Any]:
    """Invalid by design: an actuator with no safety_limits entry."""
    return load_tag(FIXTURES / "bad_no_limits.mhs")


@pytest.fixture
def restricted_gripper_tag() -> dict[str, Any]:
    return load_tag(FIXTURES / "gripper_restricted.mhs")


@pytest.fixture
def clamping_tag() -> dict[str, Any]:
    """A limit that declares on_violation 'clamp'."""
    return load_tag(FIXTURES / "clamping_heater.mhs")


@pytest.fixture
def estop_tag() -> dict[str, Any]:
    """A limit that declares on_violation 'estop'."""
    return load_tag(FIXTURES / "estop_pump.mhs")


@pytest.fixture
def example_tags() -> list[dict[str, Any]]:
    return [load_tag(p) for p in sorted(EXAMPLES.glob("*.mhs"))]


# --------------------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------------------


@pytest.fixture
def temp_device(temp_tag: dict[str, Any]) -> MockTempSensor:
    return MockTempSensor(temp_tag, seed=1234, sleep=_no_sleep)


@pytest.fixture
def arm_factory(arm_tag: dict[str, Any]) -> Callable[..., MockRoboticArm]:
    """Build an arm whose transport can be told to fail or to stick.

    `fail_on` simulates a dead link (-32002). `ignore_writes` simulates an axis that
    accepts a command and does not move (-32003).
    """

    def _build(
        *, fail_on: set[str] | None = None, ignore_writes: set[str] | None = None
    ) -> MockRoboticArm:
        transport = InMemoryTransport(
            {
                "joint_1": 0.0,
                "joint_2": 0.0,
                "gripper": "open",
                "joint_1_actual": 0.0,
                "joint_2_actual": 0.0,
                "motor_temp": 24.0,
                "estop_engaged": False,
            },
            feedback_map={"joint_1": "joint_1_actual", "joint_2": "joint_2_actual"},
            fail_on=fail_on,
            ignore_writes=ignore_writes,
        )
        return MockRoboticArm(arm_tag, transport, sleep=_no_sleep)

    return _build


@pytest.fixture
def arm_device(arm_factory: Callable[..., MockRoboticArm]) -> MockRoboticArm:
    return arm_factory()


@pytest.fixture
def gripper_device(restricted_gripper_tag: dict[str, Any]) -> GripperDevice:
    return GripperDevice(restricted_gripper_tag, sleep=_no_sleep)


@pytest.fixture
def heater_device(clamping_tag: dict[str, Any]) -> ClampingHeater:
    return ClampingHeater(
        clamping_tag,
        InMemoryTransport(
            {"heater_setpoint": 20.0, "block_temp": 20.0},
            feedback_map={"heater_setpoint": "block_temp"},
        ),
        sleep=_no_sleep,
    )


@pytest.fixture
def pump_device(estop_tag: dict[str, Any]) -> EstopPump:
    return EstopPump(
        estop_tag,
        InMemoryTransport(
            {"flow_rate": 0.0, "flow_actual": 0.0},
            feedback_map={"flow_rate": "flow_actual"},
        ),
        sleep=_no_sleep,
    )


# --------------------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------------------


@pytest.fixture
def naive_arm(arm_tag: dict[str, Any]) -> NaiveDriver:
    """The arm's capability tag, driven by a driver with no safety logic at all."""
    transport = InMemoryTransport(
        {"joint_1": 0.0, "joint_2": 0.0, "gripper": "open",
         "joint_1_actual": 0.0, "joint_2_actual": 0.0,
         "motor_temp": 24.0, "estop_engaged": False},
        feedback_map={"joint_1": "joint_1_actual", "joint_2": "joint_2_actual"},
    )
    return NaiveDriver(arm_tag, transport, sleep=_no_sleep)


@pytest_asyncio.fixture
async def naive_client(naive_arm: NaiveDriver) -> AsyncIterator[httpx.AsyncClient]:
    """A server whose only device is driven by the unsafe driver."""
    reg = Registry()
    reg.register(naive_arm.tag, naive_arm)
    app = create_app(reg, load_mocks=False, auth_token=TEST_TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH_HEADERS
    ) as c:
        yield c


@pytest_asyncio.fixture
async def client_factory() -> AsyncIterator[Callable[..., Any]]:
    """Build a client around an arbitrary set of devices, isolated per test."""
    opened: list[httpx.AsyncClient] = []

    async def _make(*devices: BaseDevice) -> httpx.AsyncClient:
        reg = Registry()
        for device in devices:
            reg.register(device.tag, device)
        app = create_app(reg, load_mocks=False, auth_token=TEST_TOKEN)
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers=AUTH_HEADERS,
        )
        opened.append(c)
        return c

    yield _make
    for c in opened:
        await c.aclose()


async def rpc_call(
    client: httpx.AsyncClient, method: str, params: dict[str, Any] | None = None
) -> Any:
    """One JSON-RPC call against any client. Returns the parsed body."""
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    response = await client.post("/rpc", json=body)
    return None if response.status_code == 204 else response.json()


@pytest.fixture
def registry(
    temp_device: MockTempSensor,
    arm_device: MockRoboticArm,
    gripper_device: GripperDevice,
) -> Registry:
    """A registry holding the three mock devices, isolated to this test."""
    reg = Registry()
    for device in (temp_device, arm_device, gripper_device):
        reg.register(device.tag, device)
    return reg


@pytest.fixture
def empty_registry() -> Registry:
    return Registry()


@pytest_asyncio.fixture
async def client(registry: Registry) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(registry, load_mocks=False, auth_token=TEST_TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as c:
        async with app.router.lifespan_context(app):
            yield c


@pytest_asyncio.fixture
async def empty_client(empty_registry: Registry) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(empty_registry, load_mocks=False, auth_token=TEST_TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as c:
        yield c


@pytest.fixture
def rpc(client: httpx.AsyncClient) -> Callable[..., Any]:
    """Send one JSON-RPC request and return the parsed response body."""

    async def _call(
        method: str,
        params: dict[str, Any] | list[Any] | None = None,
        *,
        request_id: Any = 1,
        notification: bool = False,
        path: str = "/rpc",
    ) -> Any:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            body["id"] = request_id
        response = await client.post(path, json=body)
        if response.status_code == 204:
            return None
        return response.json()

    return _call


def rpc_error(payload: Any) -> dict[str, Any]:
    """Assert the payload is a JSON-RPC error object and return it."""
    assert "error" in payload, f"expected an error, got {payload}"
    assert "result" not in payload
    return payload["error"]


def rpc_result(payload: Any) -> dict[str, Any]:
    """Assert the payload is a JSON-RPC success object and return the result."""
    assert "result" in payload, f"expected a result, got {payload}"
    assert "error" not in payload
    return payload["result"]
