"""The public API surface: what `import open_mhs` promises, and what it does not.

Two things are being pinned here. First, the exact set of names: a name that appears in
`__all__` is a promise, and one that quietly disappears breaks a downstream import at
runtime rather than at review time. Second, that the library is usable with no server at
all — a driver plus a transport is a complete safety layer, and that path has to keep
working independently of the HTTP surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import open_mhs
from tests.conftest import EXAMPLES, _no_sleep, load_tag

#: Every name `import open_mhs` is allowed to expose. Adding one is a feature; removing or
#: renaming one is a breaking change and needs a package major/minor bump and a CHANGELOG
#: entry. Update this list deliberately, never to make a failing test pass.
EXPECTED_API = {
    # the specification, as types
    "CapabilityTag", "Sensor", "Actuator", "SafetyLimit", "LimitCondition",
    "EmergencyStop", "LATEST_SPEC_VERSION", "SUPPORTED_SPEC_VERSIONS",
    # writing a driver
    "BaseDevice", "Transport", "InMemoryTransport", "TransportError",
    # the enforcement itself
    "check_write", "effective_bounds", "SafetyDecision",
    # running the middleware
    "create_app", "Registry", "AuditLog", "verify_audit_log",
    # what a refusal looks like
    "MHSError", "SafetyLimitViolation", "StateDesync", "HardwareExecutionError",
    "DeviceNotFound", "InvalidParams",
    "__version__",
}


def test_the_public_api_is_exactly_what_we_promised() -> None:
    assert set(open_mhs.__all__) == EXPECTED_API


def test_every_promised_name_actually_resolves() -> None:
    missing = [name for name in open_mhs.__all__ if not hasattr(open_mhs, name)]
    assert missing == []


def test_the_package_ships_its_type_marker() -> None:
    """Without py.typed, a downstream type checker ignores every annotation we wrote."""
    assert (Path(open_mhs.__file__).parent / "py.typed").is_file()


def test_package_version_is_distinct_from_the_spec_version() -> None:
    """Three numbers, three meanings. Conflating them is how a wire contract breaks."""
    assert open_mhs.__version__ != open_mhs.LATEST_SPEC_VERSION
    assert open_mhs.LATEST_SPEC_VERSION in open_mhs.SUPPORTED_SPEC_VERSIONS


def test_no_top_level_module_squats_a_common_name() -> None:
    """`import server` from a pip-installed package collides with the user's own code.

    Everything lives under `open_mhs`. This is what the 0.3.0 rename was for.
    """
    import importlib

    for squatted in ("server", "drivers", "cli", "mcp_adapter"):
        spec = importlib.util.find_spec(squatted)
        assert spec is None or "Open-MHS" not in str(spec.origin or ""), (
            f"{squatted!r} resolves into this repo; it must live under open_mhs"
        )


# --------------------------------------------------------------------------------------
# The library path: a driver and a transport, with no server anywhere
# --------------------------------------------------------------------------------------


class RecordingTransport(open_mhs.Transport):
    """A transport a third party might write. Records what it was asked to send."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = dict(state)
        self.sent: list[tuple[str, Any]] = []

    async def acquire(self, target: str) -> Any:
        return self.state[target]

    async def transmit(self, target: str, value: Any) -> None:
        self.sent.append((target, value))
        self.state[target] = value
        if target == "flow_rate":
            self.state["flow_actual"] = value


class BenchPump(open_mhs.BaseDevice):
    """What a library user writes: a subclass, and nothing else."""


@pytest.fixture
def pump() -> BenchPump:
    tag = open_mhs.CapabilityTag.model_validate(load_tag(EXAMPLES / "bench_pump.mhs"))
    transport = RecordingTransport({"flow_rate": 0.0, "flow_actual": 0.0, "tray_level": 0.0})
    return BenchPump(tag, transport, sleep=_no_sleep)


@pytest.mark.asyncio
async def test_a_driver_enforces_the_envelope_with_no_server(pump: BenchPump) -> None:
    result = await pump.write("flow_rate", 5.0)
    assert result["written"] == 5.0 and result["verified"] is True
    assert pump.transport.sent == [("flow_rate", 5.0)]


@pytest.mark.asyncio
async def test_a_library_refusal_transmits_nothing(pump: BenchPump) -> None:
    with pytest.raises(open_mhs.SafetyLimitViolation) as exc:
        await pump.write("flow_rate", 500.0)
    assert exc.value.data["max"] == 10.0
    assert exc.value.data["attempted"] == 500.0
    assert pump.transport.sent == []


@pytest.mark.asyncio
async def test_a_library_user_can_evaluate_a_bound_without_writing(pump: BenchPump) -> None:
    """`check_write` is public so a planner can ask 'would this be allowed?' first."""
    actuator = pump.tag.actuator_map["flow_rate"]
    limit = pump.tag.limit_map["flow_rate"]
    decision = open_mhs.check_write(actuator, limit, 5.0)
    assert decision.value == 5.0 and decision.clamped is False
    with pytest.raises(open_mhs.SafetyLimitViolation):
        open_mhs.check_write(actuator, limit, 50.0)
    assert pump.transport.sent == []


def test_effective_bounds_is_public_so_a_planner_can_resolve_a_conditional_envelope() -> None:
    tag = open_mhs.CapabilityTag.model_validate(load_tag(EXAMPLES / "bench_pump.mhs"))
    limit = tag.limit_map["flow_rate"]
    low, high, matched = open_mhs.effective_bounds(limit, state=None)
    assert (low, high, matched) == (0.0, 10.0, None)


@pytest.mark.asyncio
async def test_the_audit_log_is_usable_standalone(tmp_path: Path) -> None:
    log = open_mhs.AuditLog(tmp_path / "a.jsonl")
    log.record("write.accepted", device_id="pump-01", target="flow_rate",
               params={"value": 5.0}, outcome={"transmitted": 5.0})
    assert open_mhs.verify_audit_log(tmp_path / "a.jsonl")["ok"] is True
