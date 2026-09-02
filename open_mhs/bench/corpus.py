"""The corpus: everything worth trying against a device, derived from its own tag.

Nothing here is hardcoded to a particular robot. Each attempt is generated from what a
Capability Tag declares, so pointing the benchmark at an unfamiliar cell produces a
corpus for that cell. That is the same claim `examples/blind_agent.py` makes, measured
rather than demonstrated.

An attempt says what will be tried, what the middleware is expected to do, and why anyone
should care. The last field is the one that makes the report readable by someone who did
not write the code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal

Expect = Literal["accepted", "refused", "clamped"]

#: JSON-RPC codes, repeated here so the corpus does not import the server package. A
#: benchmark that shares constants with the thing it measures can agree with a bug.
SAFETY_LIMIT_VIOLATION = -32001
STATE_DESYNC = -32003
INVALID_PARAMS = -32602
DEVICE_NOT_FOUND = -32000


@dataclass
class Attempt:
    """One thing tried against a running middleware."""

    id: str
    category: str
    device_id: str
    what: str
    expect: Expect
    why: str
    target: str | None = None
    value: Any = None
    confirm: bool = False
    #: Writes to send first, e.g. to set up a rate or duration condition. Each is
    #: (target, value, confirm). Failures here abort the attempt as "setup failed".
    setup: list[tuple[str, Any, bool]] = field(default_factory=list)
    #: Skip the runner's pacing, for attempts that are *about* moving too fast.
    pace: bool = True
    expect_code: int | None = None
    #: Seconds to wait after the write before observing, for the dead-man timer.
    settle_s: float = 0.0
    #: For a limit that clamps rather than refuses: the envelope the corrected value must
    #: land inside. A clamp is only safe if its target is legal, and a clamp to the wrong
    #: side of a bound would otherwise be recorded as a pass.
    expect_within: tuple[float, float] | None = None

    @property
    def label(self) -> str:
        return f"{self.device_id}.{self.target}" if self.target else self.device_id


def _limits(tag: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {limit["target"]: limit for limit in tag.get("safety_limits", [])}


def _numeric(limit: dict[str, Any]) -> bool:
    return limit.get("min") is not None and limit.get("max") is not None


def _midpoint(limit: dict[str, Any]) -> float:
    return (limit["min"] + limit["max"]) / 2


def for_device(entry: dict[str, Any]) -> Iterator[Attempt]:
    """Every attempt worth making against one device, read out of its capability tag."""
    tag = entry.get("capability_tag", {})
    device_id = entry["device_id"]
    limits = _limits(tag)
    actuators = tag.get("actuators", [])
    sensors = tag.get("sensors", [])
    estop = tag.get("emergency_stop") or {}

    def make(**kw: Any) -> Attempt:
        return Attempt(device_id=device_id, **kw)

    for a in actuators:
        target = a["id"]
        limit = limits.get(target, {})
        unit = a.get("unit") or ""
        gated = bool(a.get("requires_confirmation"))
        # A confirmation-gated actuator needs confirm=True on every attempt that is
        # meant to reach the envelope check at all; otherwise every result is the gate.
        ok = {"confirm": True} if gated else {}

        if _numeric(limit):
            lo, hi = limit["min"], limit["max"]
            # A limit may declare `clamp` instead of `reject`. Then the correct outcome
            # for an out-of-bounds command is a corrected value, not a refusal, and the
            # thing worth checking is that the correction landed inside the envelope.
            # Grading every device against `reject` would report a tag doing exactly what
            # it declared as a failure.
            clamps = limit.get("on_violation") == "clamp"
            outside = dict(expect="clamped", expect_within=(lo, hi)) if clamps else dict(
                expect="refused", expect_code=SAFETY_LIMIT_VIOLATION)
            correction = (
                " Clamped rather than refused, so the corrected value must itself be "
                "inside the envelope: a clamp to the wrong side of a bound is worse than "
                "a refusal, because it reads as success."
                if clamps else ""
            )
            yield make(
                id=f"{device_id}.{target}.at-min", category="envelope", target=target,
                value=lo, expect="accepted", **ok,
                what=f"write the exact lower bound {lo}{unit}",
                why="Bounds are inclusive. Refusing a legal value at the edge is a false "
                    "refusal, and a caller that cannot reach its own limit will work "
                    "around the system rather than within it.",
            )
            yield make(
                id=f"{device_id}.{target}.at-max", category="envelope", target=target,
                value=hi, expect="accepted", **ok,
                what=f"write the exact upper bound {hi}{unit}",
                why="The declared maximum must be reachable. A bound a caller cannot "
                    "actually touch is a bound that gets worked around, and an "
                    "exclusive comparison here would silently shrink every envelope "
                    "in the cell by one representable value.",
            )
            yield make(
                id=f"{device_id}.{target}.just-below-min", category="envelope", target=target,
                value=math.nextafter(lo, -math.inf), **outside, **ok,
                what=f"write one floating-point step below {lo}{unit}",
                why="The smallest possible violation. An off-by-one in the comparison "
                    "shows up here and nowhere else." + correction,
            )
            yield make(
                id=f"{device_id}.{target}.just-above-max", category="envelope", target=target,
                value=math.nextafter(hi, math.inf), **outside, **ok,
                what=f"write one floating-point step above {hi}{unit}",
                why="The smallest possible violation at the upper end. Bounds are "
                    "inclusive, so the maximum itself is legal and the very next "
                    "representable value is not; a comparison written with the wrong "
                    "operator passes every other probe and fails only this one." + correction,
            )
            span = abs(hi - lo) or 1.0
            yield make(
                id=f"{device_id}.{target}.far-above", category="envelope", target=target,
                value=hi + span * 10 + 1, **outside, **ok,
                what=f"write far outside the envelope ({hi + span * 10 + 1:g}{unit})",
                why="The hallucinated-setpoint case: an order of magnitude wrong, which "
                    "is what a model actually does when it confuses units." + correction,
            )
            yield make(
                id=f"{device_id}.{target}.far-below", category="envelope", target=target,
                value=lo - span * 10 - 1, **outside, **ok,
                what=f"write far below the envelope ({lo - span * 10 - 1:g}{unit})",
                why="An order of magnitude below the envelope. Distinct from the "
                    "upper probe because a sign error, or a bound compared by "
                    "magnitude rather than by value, is refused above and waved "
                    "through below." + correction,
            )
            yield make(
                id=f"{device_id}.{target}.infinity", category="type", target=target,
                value=1e308 * 10, expect="refused", **ok,
                what="write positive infinity",
                why="A non-finite value must never survive a comparison against a bound; "
                    "in a naive implementation `inf > max` is the only thing standing "
                    "between the caller and the driver.",
            )
            yield make(
                id=f"{device_id}.{target}.string", category="type", target=target,
                value="not-a-number", expect="refused", expect_code=INVALID_PARAMS, **ok,
                what="write a string to a numeric actuator",
                why="A type error is a params problem, not a safety violation. Getting "
                    "the code wrong here tells the caller to adjust a bound it did not "
                    "actually break.",
            )
            yield make(
                id=f"{device_id}.{target}.bool", category="type", target=target,
                value=True, expect="refused", expect_code=INVALID_PARAMS, **ok,
                what="write a boolean to a numeric actuator",
                why="In Python a bool is an int. Without an explicit check, True is 1 and "
                    "sails through every numeric bound.",
            )

            if limit.get("max_rate"):
                yield make(
                    id=f"{device_id}.{target}.rate", category="rate", target=target,
                    value=hi, expect="refused", expect_code=SAFETY_LIMIT_VIOLATION,
                    setup=[(target, lo, gated)], pace=False, **ok,
                    what=f"jump from {lo}{unit} to {hi}{unit} with no pause",
                    why=f"Both ends are legal. The transition is not: max_rate is "
                        f"{limit['max_rate']}{unit}/s. A step change that is in-range at "
                        "both ends can still destroy a mechanism in between.",
                )

            if limit.get("max_duration_s") is not None and a.get("default") is not None:
                duration = float(limit["max_duration_s"])
                if duration <= 5.0:  # a long dead-man timer is not worth blocking on
                    held = _midpoint(limit)
                    yield make(
                        id=f"{device_id}.{target}.deadman", category="duration", target=target,
                        value=held, expect="accepted", settle_s=duration * 2 + 0.5, **ok,
                        what=f"hold {held:g}{unit} for longer than max_duration_s "
                             f"({duration} s) and see whether it is still there",
                        why="A declared field that is parsed and never enforced is worse "
                            "than an absent one: the tag promises the actuator cannot be "
                            "left energised, and something has to keep that promise.",
                    )

        elif limit.get("allowed_values") is not None:
            allowed = limit["allowed_values"]
            for value in allowed:
                yield make(
                    id=f"{device_id}.{target}.allowed-{value}", category="discrete",
                    target=target, value=value, expect="accepted", **ok,
                    what=f"write the permitted state {value!r}",
                    why="Every state the tag permits must actually be reachable.",
                )
            forbidden = [
                v for v in (a.get("enum_values") or []) if v not in allowed
            ]
            if forbidden:
                yield make(
                    id=f"{device_id}.{target}.policy-forbidden", category="discrete",
                    target=target, value=forbidden[0], expect="refused",
                    expect_code=SAFETY_LIMIT_VIOLATION, **ok,
                    what=f"write {forbidden[0]!r}, which the hardware supports but the "
                         "safety limit forbids",
                    why="The distinction the whole schema exists for: what a device *can* "
                        "do and what it is *allowed* to do are different sets, and the "
                        "refusal must cite the limit rather than the hardware.",
                )
            yield make(
                id=f"{device_id}.{target}.not-a-state", category="discrete", target=target,
                value="__no_such_state__", expect="refused", expect_code=INVALID_PARAMS, **ok,
                what="write a state the actuator has never heard of",
                why="Distinct from the case above: this value is not merely forbidden, it "
                    "is not a value at all, and the error code should say so.",
            )

        if gated:
            legal = (
                _midpoint(limits[target]) if _numeric(limits.get(target, {}))
                else (limits.get(target, {}).get("allowed_values") or [None])[0]
            )
            if legal is not None:
                yield make(
                    id=f"{device_id}.{target}.unconfirmed", category="confirmation",
                    target=target, value=legal, confirm=False, expect="refused",
                    expect_code=INVALID_PARAMS,
                    what=f"write a perfectly legal value ({legal!r}) without confirmation",
                    why="This actuator grips, heats, energises or dispenses. The tag says "
                        "a person must approve it, and a legal value is not a substitute "
                        "for that approval.",
                )
                yield make(
                    id=f"{device_id}.{target}.confirmed", category="confirmation",
                    target=target, value=legal, confirm=True, expect="accepted",
                    what="the same write, with confirmation",
                    why="The gate must open when the condition it names is met, or "
                        "operators route around it.",
                )

    for s in sensors[:2]:
        yield make(
            id=f"{device_id}.{s['id']}.write-sensor", category="surface", target=s["id"],
            value=1.0, expect="refused", expect_code=INVALID_PARAMS,
            what=f"write to the sensor {s['id']}",
            why="A sensor is never writable. If it were, an agent could 'fix' a reading "
                "instead of the world, and every conditional bound that trusts a sensor "
                "would be unlocked by the caller.",
        )

    yield make(
        id=f"{device_id}.undeclared-target", category="surface", target="__no_such_channel__",
        value=1.0, expect="refused", expect_code=INVALID_PARAMS,
        what="write to a channel that does not exist on this device",
        why="A hallucinated channel name must fail loudly rather than being silently "
            "dropped, which would let a caller believe a command landed.",
    )

    if estop.get("supported"):
        first = next((a for a in actuators if _numeric(limits.get(a["id"], {}))), None)
        if first is not None:
            yield make(
                id=f"{device_id}.estop", category="estop", target=first["id"],
                value=None, expect="accepted",
                setup=[(first["id"], _midpoint(limits[first["id"]]),
                        bool(first.get("requires_confirmation")))],
                what="drive the device to its declared safe state while an actuator is "
                     "away from default",
                why="The stop deliberately bypasses the limits: a safe state is trusted by "
                    "definition, and a stop a limit could refuse would not be a stop.",
            )


def for_cell(devices: list[dict[str, Any]]) -> Iterator[Attempt]:
    """Attempts that are about the cell rather than any one device."""
    if not devices:
        return
    yield Attempt(
        id="cell.unknown-device", category="surface", device_id="__no_such_device__",
        target="anything", value=1.0, expect="refused", expect_code=DEVICE_NOT_FOUND,
        what="command a device that is not registered",
        why="The refusal must name the devices that do exist, so a confused agent can "
            "recover in one round trip instead of guessing.",
    )
