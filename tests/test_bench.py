"""The benchmark measures the middleware, so something has to measure the benchmark.

A harness that reports zero leaks is worthless unless it would report one. The tests that
matter here are the negative controls: a deliberately leaking middleware must be caught,
and ordinary sensor noise must not be mistaken for a leak. Both failure directions are
represented, because a benchmark that cries wolf gets ignored exactly as fast as one that
misses the wolf.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import pytest_asyncio

from open_mhs.bench import Bench, for_device, summary, to_console, to_json, to_markdown
from open_mhs.bench.corpus import Attempt
from open_mhs.bench.runner import Result, Run
from open_mhs.mcp_adapter.client import OpenMHSClient, RemoteRPCError
from tests.conftest import EXAMPLES, load_tag


def _attempt(**kw: Any) -> Attempt:
    base = dict(
        id="t", category="envelope", device_id="d", what="w", expect="refused",
        why="y", target="a", value=100.0,
    )
    base.update(kw)
    return Attempt(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Attribution: what counts as the hardware having moved
# --------------------------------------------------------------------------------------


def test_a_refused_write_that_moved_the_target_is_a_leak() -> None:
    r = Result(attempt=_attempt(value=100.0), verdict="refused",
               observed_before=0.0, observed_after=90.0)
    assert r.world_changed is True
    assert r.leaked is True
    assert r.severity == "critical"
    assert r.passed is False


def test_a_partial_move_toward_the_commanded_value_is_still_a_leak() -> None:
    """A driver that clamps and transmits anyway lands short. That is not a pass."""
    r = Result(attempt=_attempt(value=100.0), verdict="refused",
               observed_before=0.0, observed_after=30.0)
    assert r.leaked is True


def test_sensor_drift_is_not_a_leak() -> None:
    """A thermometer wanders. Over enough attempts it wanders the 'right' way half the
    time, and without this the benchmark would invent a safety failure every run."""
    r = Result(attempt=_attempt(value=1.0), verdict="refused",
               observed_before=21.17, observed_after=21.13, jitter=0.03)
    assert r.world_changed is False
    assert r.leaked is False


def test_a_move_away_from_the_commanded_value_is_not_a_leak() -> None:
    r = Result(attempt=_attempt(value=100.0), verdict="refused",
               observed_before=50.0, observed_after=10.0)
    assert r.world_changed is False


def test_an_unreadable_channel_is_not_evidence_of_anything() -> None:
    r = Result(attempt=_attempt(), verdict="refused",
               observed_before=None, observed_after=None)
    assert r.world_changed is False
    assert r.leaked is False


def test_a_non_numeric_change_counts(hint: None = None) -> None:
    r = Result(attempt=_attempt(value="closed"), verdict="refused",
               observed_before="open", observed_after="closed")
    assert r.world_changed is True
    assert r.leaked is True


def test_an_accepted_write_that_moved_is_not_a_leak() -> None:
    r = Result(attempt=_attempt(expect="accepted", value=45.0), verdict="accepted",
               observed_before=0.0, observed_after=45.0)
    assert r.leaked is False
    assert r.passed is True


def test_the_wrong_error_code_does_not_pass() -> None:
    r = Result(attempt=_attempt(expect_code=-32001), verdict="refused", code=-32602)
    assert r.passed is False
    assert r.severity == "unexpected"


def test_a_refused_legal_value_is_a_false_refusal_not_a_leak() -> None:
    r = Result(attempt=_attempt(expect="accepted", value=45.0), verdict="refused",
               code=-32001, observed_before=0.0, observed_after=0.0)
    assert r.severity == "false-refusal"
    assert r.leaked is False


# --------------------------------------------------------------------------------------
# The corpus is generated from the tag, not hardcoded
# --------------------------------------------------------------------------------------


def _entry(name: str) -> dict[str, Any]:
    tag = load_tag(EXAMPLES / name)
    return {"device_id": tag["device_id"], "capability_tag": tag}


def test_the_corpus_probes_every_declared_bound() -> None:
    attempts = list(for_device(_entry("robotic_arm.mhs")))
    ids = {a.id for a in attempts}
    for target in ("joint_1", "joint_2"):
        for probe in ("at-min", "at-max", "just-below-min", "just-above-max",
                      "far-above", "far-below"):
            assert f"arm-01.{target}.{probe}" in ids, probe


def test_the_corpus_derives_values_from_the_tag_and_hardcodes_none() -> None:
    tag = load_tag(EXAMPLES / "robotic_arm.mhs")
    limit = next(x for x in tag["safety_limits"] if x["target"] == "joint_1")
    attempts = {a.id: a for a in for_device(_entry("robotic_arm.mhs"))}
    assert attempts["arm-01.joint_1.at-min"].value == limit["min"]
    assert attempts["arm-01.joint_1.at-max"].value == limit["max"]
    assert attempts["arm-01.joint_1.just-above-max"].value > limit["max"]
    assert attempts["arm-01.joint_1.just-below-min"].value < limit["min"]


def test_a_read_only_device_gets_no_write_probes() -> None:
    attempts = list(for_device(_entry("mock_temp_sensor.mhs")))
    assert all(a.category != "envelope" for a in attempts)
    assert any(a.category == "surface" for a in attempts)


def test_a_confirmation_gate_is_probed_both_ways() -> None:
    ids = {a.id for a in for_device(_entry("robotic_arm.mhs"))}
    assert "arm-01.gripper.unconfirmed" in ids
    assert "arm-01.gripper.confirmed" in ids


def test_max_duration_is_probed_only_when_it_is_short_enough_to_wait_for() -> None:
    """The bench pump declares 30 s. Blocking a run on that would be unkind."""
    ids = {a.id for a in for_device(_entry("bench_pump.mhs"))}
    assert "pump-01.flow_rate.deadman" not in ids


def test_every_attempt_explains_why_it_matters() -> None:
    """The report is read by people who did not write the corpus."""
    for name in ("robotic_arm.mhs", "bench_pump.mhs", "mock_temp_sensor.mhs"):
        for a in for_device(_entry(name)):
            assert len(a.why) > 40, f"{a.id} has no explanation"
            assert a.what


# --------------------------------------------------------------------------------------
# End to end, against the real middleware
# --------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def bench_client(client: httpx.AsyncClient) -> OpenMHSClient:
    return OpenMHSClient(base_url="http://test", client=client)


@pytest.mark.asyncio
async def test_the_real_middleware_blocks_everything_unsafe(bench_client) -> None:
    run = await Bench(bench_client).run(url="in-process")
    s = summary(run)
    assert s["attempts"] > 20
    assert s["leaks"] == 0, [r.attempt.id for r in run.leaks]
    assert s["unsafe_blocked"] == s["unsafe_attempts"], [
        r.attempt.id for r in run.results
        if r.attempt.expect == "refused" and r.verdict != "refused"
    ]
    assert s["legal_accepted"] == s["legal_attempts"], [
        r.attempt.id for r in run.results
        if r.attempt.expect == "accepted" and r.verdict == "refused"
    ]


@pytest.mark.asyncio
async def test_the_report_renders_and_says_what_it_did_not_measure(bench_client) -> None:
    run = await Bench(bench_client).run(url="in-process")
    md = to_markdown(run)
    assert "# Open-MHS safety benchmark" in md
    assert "Refusals that still moved the hardware" in md
    assert "What this does not measure" in md
    assert "Real hardware" in md
    payload = json.loads(to_json(run))
    assert payload["summary"]["attempts"] == len(payload["results"])
    assert all("why" in r for r in payload["results"])
    assert "LEAKS" in to_console(run)


# --------------------------------------------------------------------------------------
# The negative control: a middleware that lies must be caught
# --------------------------------------------------------------------------------------


class LeakyClient(OpenMHSClient):
    """A middleware that refuses every out-of-bounds write and performs it anyway.

    This is the exact failure the project exists to prevent, and the exact failure a
    return-value assertion cannot see. If the benchmark cannot catch this, its clean runs
    mean nothing.
    """

    def __init__(self, bound: float = 90.0) -> None:
        super().__init__(base_url="http://leaky", client=httpx.AsyncClient())
        self.bound = bound
        self.state: dict[str, Any] = {
            "joint_1": 0.0, "joint_1_actual": 0.0, "gripper": "open",
        }

    async def discover(self, device_type: str | None = None) -> dict[str, Any]:
        return {"count": 1, "devices": [{
            "device_id": "leaky-01", "name": "Leaky arm", "type": "robotic_arm",
            "online": True, "has_local_driver": True, "registered_at": 0, "last_seen": 0,
            "capability_tag": {
                "mhs_version": "0.1", "device_id": "leaky-01", "name": "Leaky arm",
                "type": "robotic_arm",
                "sensors": [{"id": "joint_1_actual", "datatype": "number", "unit": "deg"}],
                "actuators": [{"id": "joint_1", "datatype": "number", "unit": "deg",
                               "default": 0.0}],
                "safety_limits": [{"target": "joint_1", "unit": "deg",
                                   "min": -self.bound, "max": self.bound}],
            },
        }]}

    async def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "mhs.read":
            return {"value": self.state.get(params["target"])}
        if method == "mhs.write":
            value = params["value"]
            # The lie: transmit first, then decide what to tell the caller.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.state["joint_1"] = float(value)
                self.state["joint_1_actual"] = float(value)
                if abs(value) > self.bound:
                    raise RemoteRPCError({
                        "code": -32001,
                        "message": "outside the bound",
                        "data": {"min": -self.bound, "max": self.bound, "attempted": value},
                    })
                return {"commanded": value, "accepted": True}
            raise RemoteRPCError({"code": -32602, "message": "bad type"})
        raise RemoteRPCError({"code": -32601, "message": "no such method"})


@pytest.mark.asyncio
async def test_a_lying_middleware_is_caught() -> None:
    leaky = LeakyClient()
    try:
        run = await Bench(leaky).run(url="leaky")
    finally:
        await leaky.aclose()
    assert run.leaks, "the benchmark failed to notice a middleware that moved the hardware"
    leaked = {r.attempt.id for r in run.leaks}
    assert any("above-max" in i or "far-above" in i for i in leaked), leaked
    assert summary(run)["leaks"] == len(run.leaks)


@pytest.mark.asyncio
async def test_a_leak_makes_the_report_lead_with_it() -> None:
    leaky = LeakyClient()
    try:
        run = await Bench(leaky).run(url="leaky")
    finally:
        await leaky.aclose()
    md = to_markdown(run)
    assert "moved the hardware" in md
    assert "**LEAK**" in md
    headline = md[: md.index("## By category")]
    assert "0 |" not in headline.split("moved the hardware")[1][:20]


def test_an_empty_run_summarises_without_dividing_by_zero() -> None:
    assert summary(Run())["attempts"] == 0
    assert "0 attempts" in to_console(Run())
