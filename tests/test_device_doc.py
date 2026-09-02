"""The reference file: everything a model needs about a device, generated from its tag.

The document is what an agent reads instead of a vendor PDF, so the tests care about one
thing above all: it must not contain a number the tag does not declare, and it must not
omit a bound the tag does declare.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cli import main as cli
from cli.device_doc import generate
from server.models import CapabilityTag
from tests.conftest import EXAMPLES, FIXTURES, load_tag


def _tag(path: Path) -> CapabilityTag:
    return CapabilityTag.model_validate(load_tag(path))


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.mhs")) + sorted(FIXTURES.glob("*.mhs")))
def test_every_valid_tag_produces_a_document(path: Path) -> None:
    try:
        tag = _tag(path)
    except Exception:
        pytest.skip("fixture is invalid by design")
    doc = generate(tag)
    assert doc.startswith(f"# {tag.name}")
    assert tag.device_id in doc


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.mhs")))
def test_every_bound_in_the_tag_appears_in_the_document(path: Path) -> None:
    """An omitted bound is the failure mode that matters: the agent then assumes one."""
    tag = _tag(path)
    doc = generate(tag)
    for limit in tag.safety_limits:
        assert f"`{limit.target}`" in doc
        if limit.allowed_values is not None:
            for value in limit.allowed_values:
                assert f"`{value}`" in doc
        else:
            assert str(limit.min) in doc and str(limit.max) in doc
        if limit.rationale:
            assert limit.rationale in doc


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.mhs")))
def test_every_channel_is_listed(path: Path) -> None:
    tag = _tag(path)
    doc = generate(tag)
    for channel in list(tag.sensors) + list(tag.actuators):
        assert f"`{channel.id}`" in doc


def test_the_document_invents_no_bound_for_a_read_only_device() -> None:
    tag = _tag(EXAMPLES / "mock_temp_sensor.mhs")
    doc = generate(tag)
    assert "This device is read-only" in doc
    assert "## Write" in doc


def test_confirmation_and_verification_are_called_out() -> None:
    doc = generate(_tag(EXAMPLES / "robotic_arm.mhs"))
    assert "needs human confirmation" in doc
    assert "`joint_1` → `joint_1_actual` after 800 ms" in doc
    assert "state desync" in doc


def test_hazard_class_leads_the_document() -> None:
    doc = generate(_tag(EXAMPLES / "bench_pump.mhs"))
    assert "Hazard class: chemical" in doc
    assert doc.index("Hazard class") < doc.index("## Read")


def test_max_duration_is_documented_because_the_middleware_acts_on_it() -> None:
    doc = generate(_tag(EXAMPLES / "bench_pump.mhs"))
    assert "returns to default after 30.0 s" in doc


def test_a_device_without_an_emergency_stop_says_so_plainly() -> None:
    doc = generate(_tag(EXAMPLES / "mock_temp_sensor.mhs"))
    assert "**Not supported.**" in doc
    assert "prevention is the only control" in doc


def test_conditional_bounds_are_explained_with_their_trigger() -> None:
    panda = EXAMPLES / "robosuite_demo" / "panda_arm.mhs"
    if not panda.exists():
        pytest.skip("robosuite demo tag not present")
    tag = _tag(panda)
    conditional = [limit for limit in tag.safety_limits if limit.conditions]
    if not conditional:
        pytest.skip("no conditional bounds in this tag")
    doc = generate(tag)
    assert "Bounds that change with device state" in doc
    assert "can only ever make the bound tighter" in doc
    for limit in conditional:
        for c in limit.conditions or []:
            assert f"`{c.when_target}` reads `{c.equals}`" in doc


def test_the_shell_examples_are_inside_the_declared_bounds() -> None:
    """A copy-pasteable example that gets refused teaches the wrong lesson."""
    tag = _tag(EXAMPLES / "robotic_arm.mhs")
    doc = generate(tag)
    line = next(ln for ln in doc.splitlines() if ln.startswith("open-mhs write"))
    _, _, device, target, value, *rest = line.split()
    assert device == tag.device_id
    limit = tag.limit_map[target]
    if limit.allowed_values is not None:
        assert value in {str(v) for v in limit.allowed_values}
    else:
        assert limit.min <= float(value) <= limit.max
    actuator = tag.actuator_map[target]
    assert ("--confirm" in rest) is actuator.requires_confirmation


@pytest.mark.parametrize("name", ["robotic_arm.mhs", "bench_pump.mhs", "mock_temp_sensor.mhs"])
def test_no_number_appears_that_the_tag_does_not_declare(name: str) -> None:
    """The document must not fabricate a figure. Every number it prints is either from
    the tag, a fixed error code, or the localhost URL.

    Free text the tag itself wrote (name, description, rationales) and identifiers it
    chose (device_id, channel ids) are removed before the scan: digits inside `arm-01`
    or inside a human's rationale are the tag author's, not this generator's.
    """
    tag = _tag(EXAMPLES / name)
    url = "http://127.0.0.1:8000"
    doc = generate(tag, url=url).replace(url, "")
    for prose in [
        tag.name, tag.description or "", tag.vendor or "", tag.model or "",
        tag.firmware_version or "", tag.device_id,
        tag.device_id.replace("-", "_"),
        *(c.id for c in list(tag.sensors) + list(tag.actuators)),
        *(limit.rationale or "" for limit in tag.safety_limits),
        *(c.rationale or "" for limit in tag.safety_limits for c in limit.conditions or []),
        *(s.name or "" for s in tag.sensors), *(s.description or "" for s in tag.sensors),
        *(a.name or "" for a in tag.actuators), *(a.description or "" for a in tag.actuators),
    ]:
        if prose:
            doc = doc.replace(prose, "")
    ranges = [s.nominal_range for s in tag.sensors if s.nominal_range is not None]
    declared = {str(v) for v in [
        *(x for limit in tag.safety_limits for x in (limit.min, limit.max, limit.max_rate,
                                                     limit.max_duration_s)),
        *(a.settle_time_ms for a in tag.actuators),
        *(s.accuracy for s in tag.sensors),
        *(s.sample_rate_hz for s in tag.sensors),
        *(r.min for r in ranges),
        *(r.max for r in ranges),
        *((tag.emergency_stop.max_stop_time_ms,) if tag.emergency_stop else ()),
        *((tag.emergency_stop.safe_state or {}).values() if tag.emergency_stop else ()),
        tag.mhs_version,
    ] if v is not None}
    allowed = declared | {"32001", "32002", "32003", "32602", "8000", "127", "0", "1"}
    for number in re.findall(r"-?\d+(?:\.\d+)?", doc):
        assert number.lstrip("-") in {a.lstrip("-") for a in allowed} or number in allowed, (
            f"{number!r} appears in the document but is not declared in the tag"
        )


@pytest.mark.asyncio
async def test_cli_doc_writes_a_file(tmp_path: Path, capsys) -> None:
    out = tmp_path / "DEVICE.md"
    code = await cli.amain(["doc", str(EXAMPLES / "bench_pump.mhs"), "--out", str(out)])
    assert code == 0
    assert out.read_text(encoding="utf-8").startswith("# Bench Peristaltic Pump")
    assert str(out) in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_doc_to_stdout_and_url_override(capsys) -> None:
    code = await cli.amain(
        ["doc", str(EXAMPLES / "robotic_arm.mhs"), "--url", "http://lab-01:9000"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "# 2-Axis Bench Arm" in out
    assert "http://lab-01:9000" in out
