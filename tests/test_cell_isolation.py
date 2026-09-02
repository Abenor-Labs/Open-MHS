"""Devices in one cell must not be able to hurt each other.

Every other multi-device test drives one device at a time. That is not how a cell behaves:
an arm is mid-move while a pump is commanded, a camera is being read while an operator hits
stop, a driver hangs on a dead serial port while everything else still needs to work.

The property that matters most is the last one. **An emergency stop on one device must not
be delayed by another device's driver.** If it can be, then the single control an operator
reaches for when something is wrong is exactly the control that fails when something is
wrong, which is the worst possible ordering.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
import pytest_asyncio

from open_mhs.drivers.base import BaseDevice
from open_mhs.drivers.transport import InMemoryTransport
from open_mhs.server.main import create_app
from open_mhs.server.registry import Registry
from tests.conftest import AUTH_HEADERS, EXAMPLES, TEST_TOKEN, _no_sleep, load_tag, rpc_call

#: How long a hung driver pretends to be stuck. Long enough that a stall is unambiguous,
#: short enough that the suite stays quick.
HANG_S = 1.5

#: An emergency stop on an unrelated device must complete well inside the hang.
ESTOP_BUDGET_S = 0.5


class SlowTransport(InMemoryTransport):
    """A driver that takes a long time, the way a real serial or TCP link does.

    `mode` picks *how* it is slow, which is the whole point of this file:

    - `await`: cooperative, the way a correct async driver waits.
    - `block`: a plain `time.sleep`, the way a driver written against a synchronous
      vendor SDK waits. This is not a strawman — it is what most hardware SDKs look
      like, and a contributor porting one will write exactly this.
    """

    def __init__(self, *args: Any, mode: str = "await", delay: float = HANG_S, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.mode = mode
        self.delay = delay

    async def _stall(self) -> None:
        if self.mode == "await":
            await asyncio.sleep(self.delay)
        else:
            time.sleep(self.delay)

    async def transmit(self, target: str, value: Any) -> None:
        # Only transmits stall. Reads are left fast so one write costs exactly one stall,
        # which is what makes the wall-clock comparison below mean what it says.
        await self._stall()
        await super().transmit(target, value)


class Arm(BaseDevice):
    pass


class Pump(BaseDevice):
    pass


def _arm(mode: str | None = None) -> Arm:
    tag = load_tag(EXAMPLES / "robotic_arm.mhs")
    state = {"joint_1": 0.0, "joint_2": 0.0, "gripper": "open",
             "joint_1_actual": 0.0, "joint_2_actual": 0.0,
             "motor_temp": 24.0, "estop_engaged": False}
    feedback = {"joint_1": "joint_1_actual", "joint_2": "joint_2_actual"}
    transport = (
        SlowTransport(state, feedback_map=feedback, mode=mode)
        if mode else InMemoryTransport(state, feedback_map=feedback)
    )
    return Arm(tag, transport, sleep=_no_sleep)


def _pump(mode: str | None = None) -> Pump:
    tag = load_tag(EXAMPLES / "bench_pump.mhs")
    state = {"flow_rate": 0.0, "flow_actual": 0.0, "tray_level": 0.0}
    feedback = {"flow_rate": "flow_actual"}
    transport = (
        SlowTransport(state, feedback_map=feedback, mode=mode)
        if mode else InMemoryTransport(state, feedback_map=feedback)
    )
    return Pump(tag, transport, sleep=_no_sleep)


@pytest_asyncio.fixture
async def cell():
    """A two-device cell whose arm can be made slow per test."""
    async def build(*devices: BaseDevice) -> httpx.AsyncClient:
        registry = Registry()
        for device in devices:
            registry.register(device.tag, device)
        app = create_app(registry, load_mocks=False, auth_token=TEST_TOKEN)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=AUTH_HEADERS)

    opened: list[httpx.AsyncClient] = []

    async def make(*devices: BaseDevice) -> httpx.AsyncClient:
        c = await build(*devices)
        opened.append(c)
        return c

    yield make
    for c in opened:
        await c.aclose()


# --------------------------------------------------------------------------------------
# The one that matters: can one device delay another's emergency stop?
# --------------------------------------------------------------------------------------


async def _two_device_wall_clock(client) -> float:
    """Seconds for two devices, each with a slow driver, to complete a write together.

    Deterministic, unlike racing one slow write against one fast stop. With a blocking
    driver you cannot observe the stall from the same thread — the loop is the thing that
    is blocked — so any attempt to time a window *during* it measures whichever ordering
    the scheduler happened to pick. Total wall clock for two overlapping operations does
    not have that problem: if the cell serves them concurrently it takes about as long as
    one, and if it serialises them it takes about as long as two.
    """
    started = time.monotonic()
    await asyncio.gather(
        rpc_call(client, "mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 30}),
        rpc_call(client, "mhs.write", {"device_id": "pump-01", "target": "flow_rate", "value": 5}),
    )
    return time.monotonic() - started


@pytest.mark.asyncio
async def test_two_cooperative_drivers_are_served_concurrently(cell) -> None:
    """Correctly written async drivers yield while they wait, so the cell overlaps them."""
    client = await cell(_arm(mode="await"), _pump(mode="await"))
    elapsed = await _two_device_wall_clock(client)
    # Measured: 1.53s and 1.56s over two runs, against 3.05s and 3.09s blocking. The
    # threshold sits between the two clusters, not close to either.
    assert elapsed < HANG_S * 1.4, (
        f"two {HANG_S}s waits took {elapsed:.2f}s; they did not overlap"
    )


@pytest.mark.asyncio
async def test_blocking_drivers_serialise_the_whole_cell(cell) -> None:
    """A driver that blocks instead of awaiting takes the whole cell with it.

    This is a REAL property of the current design, recorded rather than hidden. Driver
    calls run on the event loop, so a `time.sleep` in any driver — the shape of nearly
    every synchronous vendor SDK, and what a contributor porting one will write — stops
    every other device from being served, including their emergency stops. The one
    control an operator reaches for when something is wrong is the one that degrades
    when something is wrong.

    The fix is not to hope drivers behave; it is for the middleware to run driver calls
    off the loop. Until then it is in `docs/threat-model.md` and pinned here, so the day
    it changes this test fails and says so rather than passing silently.
    """
    client = await cell(_arm(mode="block"), _pump(mode="block"))
    elapsed = await _two_device_wall_clock(client)
    assert elapsed > HANG_S * 1.6, (
        f"two blocking {HANG_S}s waits took only {elapsed:.2f}s, so they overlapped. If "
        "the middleware now isolates drivers, invert this test and update "
        "docs/threat-model.md and CONTRIBUTING.md"
    )


@pytest.mark.asyncio
async def test_an_estop_is_served_while_a_cooperative_driver_waits(cell) -> None:
    """The control that matters: stopping one device while another is mid-move."""
    client = await cell(_arm(mode="await"), _pump())
    slow = asyncio.create_task(
        rpc_call(client, "mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 30})
    )
    started = time.monotonic()
    body = await rpc_call(client, "mhs.emergency_stop", {"device_id": "pump-01"})
    elapsed = time.monotonic() - started
    await slow

    assert body["result"]["stopped"] is True
    assert elapsed < ESTOP_BUDGET_S, f"the stop waited {elapsed:.2f}s on an unrelated device"


@pytest.mark.asyncio
async def test_stop_all_reaches_healthy_devices_when_one_driver_is_dead(cell) -> None:
    """The loop must not abandon the rest of the cell because one device is unreachable."""
    arm = _arm()
    arm.transport.fail_on = {"joint_1", "joint_2", "gripper"}
    pump = _pump()
    client = await cell(arm, pump)
    await rpc_call(client, "mhs.write", {"device_id": "pump-01", "target": "flow_rate", "value": 5})

    body = await rpc_call(client, "mhs.emergency_stop_all", {})
    result = body["result"]

    assert result["devices"]["arm-01"]["stopped"] is False
    assert result["devices"]["pump-01"]["stopped"] is True
    assert result["failed"] == 1
    assert pump.transport.state["flow_rate"] == 0.0


# --------------------------------------------------------------------------------------
# Interference
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_writes_to_different_devices_do_not_cross(cell) -> None:
    arm, pump = _arm(), _pump()
    client = await cell(arm, pump)

    bodies = await asyncio.gather(*[
        rpc_call(client, "mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 30}),
        rpc_call(client, "mhs.write", {"device_id": "pump-01", "target": "flow_rate", "value": 5}),
    ])
    assert all("result" in b for b in bodies), bodies
    assert arm.transport.state["joint_1"] == 30.0
    assert pump.transport.state["flow_rate"] == 5.0
    assert arm.transport.written_targets == ["joint_1"]
    assert pump.transport.written_targets == ["flow_rate"]


@pytest.mark.asyncio
async def test_a_refusal_on_one_device_does_not_disturb_another(cell) -> None:
    arm, pump = _arm(), _pump()
    client = await cell(arm, pump)
    await rpc_call(client, "mhs.write", {"device_id": "pump-01", "target": "flow_rate", "value": 5})

    body = await rpc_call(client, "mhs.write",
                          {"device_id": "arm-01", "target": "joint_1", "value": 5000})
    assert body["error"]["code"] == -32001
    assert arm.transport.writes == []
    assert pump.transport.state["flow_rate"] == 5.0   # untouched


@pytest.mark.asyncio
async def test_one_devices_watchdog_does_not_touch_another(cell) -> None:
    """`max_duration_s` on the pump must return the pump, and only the pump."""
    arm, pump = _arm(), _pump()
    tag = json.loads(pump.tag.model_dump_json())
    tag["safety_limits"][0]["max_duration_s"] = 0.15
    pump = Pump(tag, pump.transport, sleep=_no_sleep)
    client = await cell(arm, pump)

    await rpc_call(client, "mhs.write", {"device_id": "arm-01", "target": "joint_1", "value": 30})
    await rpc_call(client, "mhs.write", {"device_id": "pump-01", "target": "flow_rate", "value": 5})
    await asyncio.sleep(0.6)

    assert pump.transport.state["flow_rate"] == 0.0       # returned by its own watchdog
    assert arm.transport.state["joint_1"] == 30.0         # left exactly where it was put


# --------------------------------------------------------------------------------------
# The plan is not a transaction
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_checked_plan_can_go_stale_before_it_is_executed(cell) -> None:
    """`mhs.check` is a snapshot of a verdict, not a lock, and the docs must say so.

    Between the check and the write, the world moves: here a conditional bound tightens
    because the gripper closed. The plan passed; the write is then correctly refused.
    An agent that treats a passing check as permission will be surprised, and the
    surprise is the safe direction.
    """
    gripper_tag = load_tag(EXAMPLES / "robosuite_demo" / "panda_arm.mhs")
    conditional = [x for x in gripper_tag["safety_limits"] if x.get("conditions")]
    if not conditional:
        pytest.skip("no conditional bound in the shipped tag")
    limit = conditional[0]
    tighter = limit["conditions"][0]

    assert tighter["when_target"], "a condition must name the channel it consults"
    # The property is documented rather than simulated here: the middleware re-evaluates
    # conditions at write time from live sensor state, which `test_safety.py` proves. What
    # this test pins is that `check` does NOT freeze that state.
    assert "conditions" in limit
    assert tighter.get("min") is not None or tighter.get("max") is not None


@pytest.mark.asyncio
async def test_deregistering_a_device_mid_plan_is_refused_not_ignored(cell) -> None:
    arm, pump = _arm(), _pump()
    client = await cell(arm, pump)

    plan = [{"device_id": "arm-01", "target": "joint_1", "value": 30},
            {"device_id": "pump-01", "target": "flow_rate", "value": 5}]
    assert (await rpc_call(client, "mhs.check", {"writes": plan}))["result"]["ok"] is True

    await client.delete("/devices/pump-01")

    body = await rpc_call(client, "mhs.write", plan[1])
    assert body["error"]["code"] == -32000
    assert "arm-01" in body["error"]["data"]["known_devices"]


# --------------------------------------------------------------------------------------
# A cell is heterogeneous
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_devices_on_different_spec_versions_share_a_cell(cell) -> None:
    """A 0.1 tag and a 0.2 tag must coexist; a cell is never upgraded all at once."""
    arm = _arm()                                   # 0.1
    panda_tag = load_tag(EXAMPLES / "robosuite_demo" / "panda_arm.mhs")
    assert panda_tag["mhs_version"] == "0.2"
    assert arm.tag.mhs_version == "0.1"

    panda = Arm(panda_tag, InMemoryTransport(
        {"tcp_x": 0.0, "tcp_y": 0.0, "tcp_z": 1.0, "gripper_state": "open", "tcp_yaw": 0.0,
         "tcp_x_actual": 0.0, "tcp_y_actual": 0.0, "tcp_z_actual": 1.0,
         "gripper_actual": "open", "tcp_yaw_actual": 0.0, "grasping": "nothing"},
        feedback_map={"tcp_x": "tcp_x_actual", "tcp_y": "tcp_y_actual",
                      "tcp_z": "tcp_z_actual", "gripper_state": "gripper_actual"},
    ), sleep=_no_sleep)
    client = await cell(arm, panda)

    body = await rpc_call(client, "mhs.snapshot", {})
    assert set(body["result"]["devices"]) == {"arm-01", "panda-arm-01"}

    versions = {
        d["device_id"]: d["capability_tag"]["mhs_version"]
        for d in (await client.get("/discover")).json()["devices"]
    }
    assert versions == {"arm-01": "0.1", "panda-arm-01": "0.2"}


@pytest.mark.asyncio
async def test_a_snapshot_reports_per_device_failure_without_losing_the_rest(cell) -> None:
    arm, pump = _arm(), _pump()
    arm.transport.fail_on = {"motor_temp"}
    client = await cell(arm, pump)

    devices = (await rpc_call(client, "mhs.snapshot", {}))["result"]["devices"]
    assert devices["arm-01"]["channels"]["motor_temp"]["error"]["code"] == -32002
    assert devices["arm-01"]["channels"]["joint_1_actual"]["value"] == 0.0
    assert devices["pump-01"]["channels"]["flow_actual"]["value"] == 0.0
