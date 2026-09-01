"""Safety-limit evaluation. The only module that interprets a bound.

Both enforcement points call in here — the RPC dispatcher before it touches a driver, and
`BaseDevice.write` before it touches a transport. Two independent calls, one implementation,
so the two points can never disagree about what the envelope means.

Bounds are INCLUSIVE. A value equal to `min` or `max` is legal.

A limit's `on_violation` decides what happens when a value falls outside:

    reject   refuse the write, transmit nothing (the default, and the only mode that
             never surprises the caller)
    clamp    substitute the nearest legal value and proceed
    estop    drive the device to its declared safe state, then refuse

`clamp` is dangerous in a way `reject` is not: the hardware ends up somewhere the caller
did not ask for. Every clamp is therefore logged as a warning and reported back in full —
`SafetyDecision.clamped` is not decoration, it is how the caller learns that its model of
the world is now wrong.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from server.errors import InvalidParams, SafetyLimitViolation
from server.models import Actuator, LimitCondition, SafetyLimit

log = logging.getLogger("open_mhs.safety")

Number = (int, float)


class EmergencyStopRequired(SafetyLimitViolation):
    """A limit with `on_violation: estop` was breached.

    Still a -32001 to the caller, and still refuses the write. The dispatcher catches this
    specifically in order to run the device's emergency stop before the error goes out.
    """


@dataclass(frozen=True)
class SafetyDecision:
    """The outcome of evaluating one write.

    `value` is what may actually be transmitted. When `clamped` is true it is NOT what the
    caller asked for, and every layer above must say so out loud.
    """

    value: Any
    original: Any
    clamped: bool = False
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _is_number(value: Any) -> bool:
    """`bool` is a subclass of `int` in Python. A boolean is never a number here."""
    return isinstance(value, Number) and not isinstance(value, bool)


def coerce(actuator: Actuator, value: Any) -> Any:
    """Check a value against the actuator's declared datatype before any bound is applied.

    A type error is a params problem, not a safety violation: the value was never a
    candidate for the envelope in the first place, so `on_violation` does not apply to it.
    """
    dt = actuator.datatype
    if dt in {"number", "integer"}:
        if not _is_number(value):
            raise InvalidParams(
                f"{actuator.id}: expected a {dt}, got {type(value).__name__}",
                {"target": actuator.id, "datatype": dt, "value": value},
            )
        if dt == "integer" and float(value) != int(value):
            raise InvalidParams(
                f"{actuator.id}: expected an integer, got {value!r}",
                {"target": actuator.id, "datatype": dt, "value": value},
            )
        return value
    if dt == "vector3":
        if not isinstance(value, (list, tuple)) or len(value) != 3 or not all(
            _is_number(v) for v in value
        ):
            raise InvalidParams(
                f"{actuator.id}: expected 3 numbers, got {value!r}",
                {"target": actuator.id, "datatype": dt, "value": value},
            )
        return list(value)
    if dt == "boolean":
        if not isinstance(value, bool):
            raise InvalidParams(
                f"{actuator.id}: expected a boolean, got {type(value).__name__}",
                {"target": actuator.id, "datatype": dt, "value": value},
            )
        return value
    if dt in {"string", "enum"}:
        if not isinstance(value, str):
            raise InvalidParams(
                f"{actuator.id}: expected a string, got {type(value).__name__}",
                {"target": actuator.id, "datatype": dt, "value": value},
            )
        if dt == "enum" and actuator.enum_values and value not in actuator.enum_values:
            raise InvalidParams(
                f"{actuator.id}: {value!r} is not one of {actuator.enum_values}",
                {"target": actuator.id, "value": value, "enum_values": actuator.enum_values},
            )
        return value
    raise InvalidParams(f"{actuator.id}: unsupported datatype {dt!r}")


def resolve_absolute(actuator: Actuator, value: Any, current: Any) -> Any:
    """Turn a `relative` write into the absolute value the limit is evaluated against.

    Limits always govern where the hardware ends up, never the size of the delta.
    """
    if actuator.write_mode != "relative":
        return value
    if not _is_number(value) or not _is_number(current):
        raise InvalidParams(
            f"{actuator.id}: relative writes require numeric values and a known current state",
            {"target": actuator.id, "value": value, "current": current},
        )
    return current + value


def condition_targets(limit: SafetyLimit) -> list[str]:
    """Channels a caller must read before this limit can be evaluated. Usually empty."""
    if not limit.conditions:
        return []
    return sorted({condition.when_target for condition in limit.conditions})


def effective_bounds(
    limit: SafetyLimit, state: Mapping[str, Any] | None
) -> tuple[float | None, float | None, LimitCondition | None]:
    """Resolve (min, max, matched condition) for the device's CURRENT state.

    Conditions are evaluated in declaration order and the first match wins, so a tag reads
    top-to-bottom like the rules it is describing. A condition whose channel is missing
    from `state` is skipped rather than guessed at: the base bound is the stricter promise
    the tag already made, and falling back to it can only ever refuse more, never less.
    """
    if not limit.conditions or limit.min is None or limit.max is None:
        return limit.min, limit.max, None
    for condition in limit.conditions:
        if state is None or condition.when_target not in state:
            continue
        if _same_value(state[condition.when_target], condition.equals):
            low = condition.min if condition.min is not None else limit.min
            high = condition.max if condition.max is not None else limit.max
            return low, high, condition
    return limit.min, limit.max, None


def _same_value(observed: Any, expected: Any) -> bool:
    """Compare a reading to a condition's trigger, tolerating int/float/bool spelling."""
    if isinstance(expected, bool) or isinstance(observed, bool):
        return bool(observed) is bool(expected)
    if _is_number(observed) and _is_number(expected):
        return math.isclose(float(observed), float(expected), rel_tol=1e-9, abs_tol=1e-9)
    return str(observed) == str(expected)


def check_write(
    actuator: Actuator,
    limit: SafetyLimit,
    value: Any,
    *,
    current: Any = None,
    elapsed_s: float | None = None,
    device_id: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> SafetyDecision:
    """Evaluate one write and decide what, if anything, may be transmitted.

    Returns a `SafetyDecision`. Raises `SafetyLimitViolation` (-32001) under `reject`, and
    `EmergencyStopRequired` (also -32001) under `estop`, always with the attempted value and
    the violated bound in `data` so a corrective retry needs no extra round trip. Raises
    `InvalidParams` (-32602) when the value could never have been valid for this type.
    """
    value = coerce(actuator, value)
    absolute = resolve_absolute(actuator, value, current)

    base: dict[str, Any] = {"target": actuator.id, "attempted": absolute}
    if device_id:
        base["device_id"] = device_id
    if actuator.write_mode == "relative":
        base["requested_delta"] = value
        base["current"] = current

    if limit.allowed_values is not None:
        return _check_discrete(actuator, limit, absolute, base, device_id)

    decision = _check_range(actuator, limit, absolute, base, device_id, state)
    return _check_rate(actuator, limit, decision, current, elapsed_s, base, device_id)


# --------------------------------------------------------------------------------------
# Individual bounds
# --------------------------------------------------------------------------------------


def _check_discrete(
    actuator: Actuator,
    limit: SafetyLimit,
    absolute: Any,
    base: dict[str, Any],
    device_id: str | None,
) -> SafetyDecision:
    """Discrete bounds cannot be clamped: there is no 'nearest' member of a set of states.

    A tag declaring `on_violation: clamp` on a discrete limit is refused at ingestion, so
    reaching that branch here means such a tag arrived some other way. Refuse rather than
    guess which state the caller would have wanted.
    """
    if absolute in limit.allowed_values:
        return SafetyDecision(value=absolute, original=absolute)

    data = {
        **base,
        "allowed_values": limit.allowed_values,
        "enforcement": limit.enforcement,
        "on_violation": limit.on_violation,
    }
    message = f"{actuator.id}: {absolute!r} is not an allowed value"
    if limit.on_violation == "clamp":
        message += (
            " (this limit declares on_violation 'clamp', which has no meaning for a discrete "
            "bound; refusing rather than guessing a state)"
        )
    _raise(limit, message, data, device_id)


def _check_range(
    actuator: Actuator,
    limit: SafetyLimit,
    absolute: Any,
    base: dict[str, Any],
    device_id: str | None,
    state: Mapping[str, Any] | None = None,
) -> SafetyDecision:
    # The envelope may depend on what the device is currently doing. Resolve it against
    # the observed state FIRST; everything below then treats the result as the bound.
    low, high, matched = effective_bounds(limit, state)
    components = absolute if isinstance(absolute, list) else [absolute]
    bounded = [min(max(c, low), high) for c in components]  # type: ignore[type-var]

    if bounded == components:
        return SafetyDecision(value=absolute, original=absolute)

    unit = f" {limit.unit}" if limit.unit else ""
    outside = next(c for c, k in zip(components, bounded) if c != k)
    data = {
        **base,
        "min": low,
        "max": high,
        "unit": limit.unit,
        "enforcement": limit.enforcement,
        "on_violation": limit.on_violation,
        "rationale": limit.rationale,
    }
    because = ""
    if matched is not None:
        data["condition"] = {
            "when_target": matched.when_target,
            "equals": matched.equals,
            "observed": (state or {}).get(matched.when_target),
            "rationale": matched.rationale,
        }
        data["base_min"], data["base_max"] = limit.min, limit.max
        because = (f" (tightened because {matched.when_target} reads "
                   f"{(state or {}).get(matched.when_target)!r})")
    message = (
        f"{actuator.id}: {outside}{unit} is outside the inclusive bound "
        f"[{low}, {high}]{unit}{because}"
    )

    if limit.on_violation != "clamp":
        _raise(limit, message, data, device_id)

    value = bounded if isinstance(absolute, list) else bounded[0]
    bound = "max" if outside > high else "min"  # type: ignore[operator]
    log.warning(
        "CLAMPED %s.%s: requested %s, transmitting %s (bound [%s, %s]%s)",
        device_id or "?", actuator.id, absolute, value, low, high, unit,
    )
    return SafetyDecision(
        value=value,
        original=absolute,
        clamped=True,
        reason=f"{absolute}{unit} was outside [{low}, {high}]{unit}{because}; "
               f"clamped to the {bound} bound {value}{unit}",
        details={**data, "clamped_to": value, "bound": bound},
    )


def _check_rate(
    actuator: Actuator,
    limit: SafetyLimit,
    decision: SafetyDecision,
    current: Any,
    elapsed_s: float | None,
    base: dict[str, Any],
    device_id: str | None,
) -> SafetyDecision:
    """A step change can be in-range at both ends and still destructive in between."""
    absolute = decision.value
    if limit.max_rate is None or current is None or not elapsed_s or elapsed_s <= 0:
        return decision
    if not _is_number(absolute) or not _is_number(current):
        return decision

    rate = abs(absolute - current) / elapsed_s
    if rate <= limit.max_rate:
        return decision

    unit = f" {limit.unit}" if limit.unit else ""
    rate_unit = f"{unit or ' units'}/s"
    data = {
        **base,
        "commanded_rate": rate,
        "max_rate": limit.max_rate,
        "unit": limit.unit,
        "elapsed_s": elapsed_s,
        "previous": current,
        "on_violation": limit.on_violation,
        "rationale": limit.rationale,
    }
    message = (
        f"{actuator.id}: commanded rate {rate:.3f}{rate_unit} exceeds max_rate "
        f"{limit.max_rate}{rate_unit}"
    )

    if limit.on_violation != "clamp":
        _raise(limit, message, data, device_id)

    # Travel as far as the rate allows, in the direction that was asked for, and never
    # outside the range bound.
    reachable = current + math.copysign(limit.max_rate * elapsed_s, absolute - current)
    reachable = min(max(reachable, limit.min), limit.max)  # type: ignore[type-var]
    log.warning(
        "RATE-CLAMPED %s.%s: requested %s at %.3f%s, transmitting %s (max_rate %s%s)",
        device_id or "?", actuator.id, decision.original, rate, rate_unit, reachable,
        limit.max_rate, rate_unit,
    )
    return SafetyDecision(
        value=reachable,
        original=decision.original,
        clamped=True,
        reason=f"{decision.original}{unit} would have moved at {rate:.3f}{rate_unit}, above "
               f"max_rate {limit.max_rate}{rate_unit}; rate-limited to {reachable}{unit}",
        details={**data, "clamped_to": reachable, "bound": "max_rate"},
    )


def _raise(
    limit: SafetyLimit, message: str, data: dict[str, Any], device_id: str | None
) -> None:
    """Refuse the write, as `estop` or as plain `reject`."""
    if limit.on_violation == "estop":
        log.warning(
            "ESTOP-ON-VIOLATION %s.%s: %s", device_id or "?", data.get("target"), message
        )
        raise EmergencyStopRequired(
            f"{message}. This limit declares on_violation 'estop': the device is being driven "
            "to its declared safe state.",
            data,
        )
    raise SafetyLimitViolation(message, data)
