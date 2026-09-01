"""Pydantic v2 models for Open-MHS.

This module is enforcement point 1 of 2: **ingestion**. A Capability Tag that reaches the
registry has already been proved well-formed here, so nothing downstream re-checks shape.

`CapabilityTag` mirrors `schema/capability_schema.json` and additionally enforces the
cross-field rules JSON Schema cannot express (limit coverage, unit agreement, id
uniqueness across sensors and actuators, references that resolve). `tests/test_server.py`
asserts the two stay in agreement, so neither can drift silently.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")]
Unit = Annotated[str, StringConstraints(min_length=1, max_length=32)]

DataType = Literal["number", "integer", "boolean", "string", "enum", "vector3"]
NUMERIC_TYPES: frozenset[str] = frozenset({"number", "integer", "vector3"})

DeviceType = Literal[
    "robotic_arm", "mobile_base", "gantry", "gripper", "linear_stage", "sensor_array",
    "thermal_controller", "pump", "valve", "mixer", "centrifuge", "spectrometer",
    "microscope", "power_supply", "camera", "generic",
]

Transport = Literal[
    "serial", "tcp", "udp", "usb", "i2c", "spi", "gpio", "can", "http", "mock",
]


class Strict(BaseModel):
    """Base: unknown keys are errors, exactly as `additionalProperties: false` in the schema.

    A silently ignored `actuatorz` would disable safety enforcement while validation passed.
    """

    model_config = ConfigDict(extra="forbid")


class Range(Strict):
    min: float
    max: float


class _Channel(Strict):
    """Shared shape between a sensor and an actuator."""

    id: Identifier
    name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    datatype: DataType
    unit: Unit | None = None
    enum_values: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _typed_requirements(self) -> _Channel:
        if self.datatype == "enum" and not self.enum_values:
            raise ValueError(f"{self.id}: datatype 'enum' requires enum_values")
        if self.datatype in NUMERIC_TYPES and not self.unit:
            raise ValueError(f"{self.id}: datatype {self.datatype!r} requires a unit")
        if self.enum_values and len(set(self.enum_values)) != len(self.enum_values):
            raise ValueError(f"{self.id}: enum_values must be unique")
        return self


class Sensor(_Channel):
    """A read-only state. Never writable, under any circumstances."""

    nominal_range: Range | None = None
    accuracy: float | None = Field(default=None, ge=0)
    sample_rate_hz: float | None = Field(default=None, gt=0)


class Actuator(_Channel):
    """A writable state. Must have a matching entry in `safety_limits`."""

    write_mode: Literal["absolute", "relative"] = "absolute"
    default: float | bool | str | list[float] | None = None
    feedback_sensor: Identifier | None = None
    settle_time_ms: int | None = Field(default=None, ge=0)
    requires_confirmation: bool = False


class LimitCondition(Strict):
    """A tighter bound that applies only while some other channel reads a given value.

    A work envelope is not static. An empty gripper may descend to the table; the same
    gripper holding a 42 mm block may not, because the payload hangs below the tool and
    would be driven through the surface. Expressing that needs a bound that depends on
    device state, not a constant.

    `when_target` names the channel to consult. Prefer a SENSOR over the actuator that
    drives it: `gripper_state` is what was commanded, `gripper_actual` is what the jaws
    are actually doing, and a safety bound must follow the world rather than the request.
    """

    when_target: Identifier
    equals: str | bool | float
    min: float | None = None
    max: float | None = None
    rationale: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _must_bound_something(self) -> LimitCondition:
        if self.min is None and self.max is None:
            raise ValueError(
                f"condition on {self.when_target}: declares neither min nor max, so it "
                "changes nothing"
            )
        return self


class SafetyLimit(Strict):
    """A boundary for exactly one actuator. Bounds are INCLUSIVE."""

    target: Identifier
    unit: Unit | None = None
    min: float | None = None
    max: float | None = None
    allowed_values: list[str | bool | float] | None = Field(default=None, min_length=1)
    max_rate: float | None = Field(default=None, gt=0)
    max_duration_s: float | None = Field(default=None, gt=0)
    #: Evaluated in order; the FIRST match wins. A condition may only ever NARROW the
    #: base envelope -- see `_check_conditions`.
    conditions: list[LimitCondition] | None = Field(default=None, min_length=1)
    enforcement: Literal["hardware", "firmware", "software"] = "software"
    on_violation: Literal["reject", "clamp", "estop"] = "reject"
    rationale: str | None = Field(default=None, max_length=512)

    @property
    def is_numeric(self) -> bool:
        return self.min is not None and self.max is not None

    @model_validator(mode="after")
    def _exactly_one_form(self) -> SafetyLimit:
        numeric = self.min is not None or self.max is not None
        discrete = self.allowed_values is not None
        if numeric and discrete:
            raise ValueError(
                f"{self.target}: a limit is either numeric (min+max) or discrete "
                "(allowed_values), never both"
            )
        if not numeric and not discrete:
            raise ValueError(
                f"{self.target}: limit must declare either min+max or allowed_values"
            )
        if numeric:
            if self.min is None or self.max is None:
                raise ValueError(f"{self.target}: a numeric limit requires both min and max")
            if self.unit is None:
                raise ValueError(f"{self.target}: a numeric limit requires a unit")
            if self.min >= self.max:
                raise ValueError(
                    f"{self.target}: min ({self.min}) must be strictly less than max ({self.max})"
                )
        return self


class DriverSpec(Strict):
    transport: Transport
    address: str | None = Field(default=None, max_length=256)
    module: str | None = Field(default=None, max_length=256)
    timeout_ms: int = Field(default=2000, ge=1, le=600_000)


class EmergencyStop(Strict):
    supported: bool
    target: Identifier | None = None
    safe_state: dict[str, float | bool | str] | None = None
    max_stop_time_ms: int | None = Field(default=None, ge=0)


class PowerSpec(Strict):
    voltage_v: float | None = Field(default=None, gt=0)
    max_current_a: float | None = Field(default=None, gt=0)
    hazard_class: Literal[
        "none", "thermal", "mechanical", "electrical", "chemical", "optical",
        "biological", "radiation",
    ] | None = None


class DiscoverySpec(Strict):
    registry_url: str | None = None
    heartbeat_interval_s: float = Field(default=30.0, gt=0, le=3600)


class CapabilityTag(Strict):
    """A device's full self-description. See docs/capability-tags.md."""

    mhs_version: Literal["0.1"]
    device_id: Identifier
    name: str = Field(min_length=1, max_length=128)
    type: DeviceType
    sensors: list[Sensor]
    actuators: list[Actuator]
    safety_limits: list[SafetyLimit]
    vendor: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=128)
    firmware_version: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=1024)
    driver: DriverSpec | None = None
    emergency_stop: EmergencyStop | None = None
    power: PowerSpec | None = None
    discovery: DiscoverySpec | None = None
    metadata: dict[str, Any] | None = None

    # --- indexes, built once ---

    @property
    def sensor_map(self) -> dict[str, Sensor]:
        return {s.id: s for s in self.sensors}

    @property
    def actuator_map(self) -> dict[str, Actuator]:
        return {a.id: a for a in self.actuators}

    @property
    def limit_map(self) -> dict[str, SafetyLimit]:
        return {limit.target: limit for limit in self.safety_limits}

    @model_validator(mode="after")
    def _semantic_rules(self) -> CapabilityTag:
        """The physics rules JSON Schema cannot express. Each one is a real field failure."""
        sensors = self.sensor_map
        actuators = self.actuator_map
        limits = self.limit_map

        if len(sensors) != len(self.sensors):
            raise ValueError("duplicate sensor id")
        if len(actuators) != len(self.actuators):
            raise ValueError("duplicate actuator id")
        if len(limits) != len(self.safety_limits):
            raise ValueError("duplicate safety_limits target")

        # read('x') and write('x') must never be ambiguous.
        collisions = sorted(set(sensors) & set(actuators))
        if collisions:
            raise ValueError(
                f"id(s) {collisions} appear as both a sensor and an actuator; ids must be "
                "unique across the union of sensors and actuators"
            )

        # An unbounded actuator is an unbounded machine.
        unbounded = sorted(set(actuators) - set(limits))
        if unbounded:
            raise ValueError(
                f"actuator(s) {unbounded} have no safety_limits entry; every actuator must "
                "declare a boundary"
            )

        orphan = sorted(set(limits) - set(actuators))
        if orphan:
            raise ValueError(f"safety_limits target(s) {orphan} do not match any actuator")

        for act in self.actuators:
            limit = limits[act.id]
            self._check_unit(act, limit)
            self._check_discrete(act, limit)
            self._check_default(act, limit)
            if act.feedback_sensor and act.feedback_sensor not in sensors:
                raise ValueError(
                    f"{act.id}: feedback_sensor {act.feedback_sensor!r} is not a declared sensor"
                )

        for limit in self.safety_limits:
            self._check_on_violation(limit, self.emergency_stop)
            self._check_conditions(limit, sensors, actuators)

        if self.emergency_stop and self.emergency_stop.safe_state:
            unknown = sorted(set(self.emergency_stop.safe_state) - set(actuators))
            if unknown:
                raise ValueError(
                    f"emergency_stop.safe_state references unknown actuator(s) {unknown}"
                )
        if self.emergency_stop and self.emergency_stop.target:
            if self.emergency_stop.target not in actuators:
                raise ValueError(
                    f"emergency_stop.target {self.emergency_stop.target!r} is not an actuator"
                )
        return self

    @staticmethod
    def _check_conditions(
        limit: SafetyLimit, sensors: dict[str, Any], actuators: dict[str, Any]
    ) -> None:
        """Conditional bounds must reference real channels and may only ever TIGHTEN.

        The narrowing rule is the important one. If a condition could widen the envelope,
        a tag could declare a permissive base bound and then relax it further under some
        state -- which turns the declared floor into a suggestion. Every condition must be
        a stricter promise than the one already made, so the base bound is the worst case
        the device will ever accept and can be read as a guarantee on its own.
        """
        if not limit.conditions:
            return
        if limit.allowed_values is not None:
            raise ValueError(
                f"{limit.target}: conditions bound a numeric range and have no meaning "
                "for a discrete limit"
            )

        known = set(sensors) | set(actuators)
        for condition in limit.conditions:
            if condition.when_target not in known:
                raise ValueError(
                    f"{limit.target}: condition references {condition.when_target!r}, "
                    "which is not a declared sensor or actuator"
                )
            if condition.min is not None and condition.min < limit.min:
                raise ValueError(
                    f"{limit.target}: condition on {condition.when_target} sets min "
                    f"{condition.min} BELOW the base min {limit.min}; a condition may "
                    "only narrow the envelope, never widen it"
                )
            if condition.max is not None and condition.max > limit.max:
                raise ValueError(
                    f"{limit.target}: condition on {condition.when_target} sets max "
                    f"{condition.max} ABOVE the base max {limit.max}; a condition may "
                    "only narrow the envelope, never widen it"
                )
            low = condition.min if condition.min is not None else limit.min
            high = condition.max if condition.max is not None else limit.max
            if low >= high:
                raise ValueError(
                    f"{limit.target}: condition on {condition.when_target} leaves an "
                    f"empty envelope [{low}, {high}]"
                )

    @staticmethod
    def _check_on_violation(limit: SafetyLimit, estop: EmergencyStop | None) -> None:
        """A limit must not declare a violation mode the middleware cannot carry out.

        `clamp` needs a nearest legal value, which a set of discrete states does not have.
        `estop` needs a safe state to drive to. Declaring either without the thing it
        depends on is a tag that promises enforcement it will not get.
        """
        if limit.on_violation == "clamp" and limit.allowed_values is not None:
            raise ValueError(
                f"{limit.target}: on_violation 'clamp' is meaningless for a discrete limit - "
                "there is no nearest member of a set of states. Use 'reject' or 'estop'."
            )
        if limit.on_violation == "estop" and not (estop and estop.supported):
            raise ValueError(
                f"{limit.target}: on_violation 'estop' requires the device to declare "
                "emergency_stop.supported = true"
            )

    @staticmethod
    def _check_unit(act: Actuator, limit: SafetyLimit) -> None:
        """`deg` vs `rad` is the classic field failure. Units are compared literally."""
        if limit.is_numeric and act.unit != limit.unit:
            raise ValueError(
                f"{act.id}: unit mismatch - actuator declares {act.unit!r}, its safety limit "
                f"declares {limit.unit!r}. Units are never converted."
            )

    @staticmethod
    def _check_discrete(act: Actuator, limit: SafetyLimit) -> None:
        if limit.allowed_values is None:
            if act.datatype in {"enum", "string", "boolean"}:
                raise ValueError(
                    f"{act.id}: datatype {act.datatype!r} needs a discrete limit "
                    "(allowed_values), not a numeric one"
                )
            return
        if act.datatype in NUMERIC_TYPES:
            raise ValueError(
                f"{act.id}: numeric datatype {act.datatype!r} needs a min/max limit, "
                "not allowed_values"
            )
        if act.enum_values is not None:
            extra = sorted(str(v) for v in limit.allowed_values if v not in act.enum_values)
            if extra:
                raise ValueError(
                    f"{act.id}: safety limit permits {extra}, which the actuator cannot accept; "
                    "allowed_values must be a subset of enum_values"
                )

    @staticmethod
    def _check_default(act: Actuator, limit: SafetyLimit) -> None:
        """Otherwise the device's power-on state is already a violation."""
        if act.default is None:
            return
        if limit.is_numeric:
            values = act.default if isinstance(act.default, list) else [act.default]
            for v in values:
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise ValueError(f"{act.id}: default {act.default!r} is not numeric")
                if not (limit.min <= v <= limit.max):  # type: ignore[operator]
                    raise ValueError(
                        f"{act.id}: default {act.default!r} is outside its safety limit "
                        f"[{limit.min}, {limit.max}]"
                    )
        elif limit.allowed_values is not None and act.default not in limit.allowed_values:
            raise ValueError(
                f"{act.id}: default {act.default!r} is not in allowed_values "
                f"{limit.allowed_values}"
            )


# --------------------------------------------------------------------------------------
# Transport models: registry REST surface
# --------------------------------------------------------------------------------------


class DeviceSummary(Strict):
    """What `/discover` returns per device. The tag is included in full.

    An agent that has to make a second call to learn what a device can do will make its
    first decision without that knowledge.
    """

    device_id: str
    name: str
    type: str
    online: bool
    has_local_driver: bool
    registered_at: float
    last_seen: float
    capability_tag: CapabilityTag


class DiscoverResponse(Strict):
    count: int
    devices: list[DeviceSummary]


class RegisterResponse(Strict):
    registered: bool
    device_id: str
    heartbeat_interval_s: float
    message: str


class DeregisterResponse(Strict):
    deregistered: bool
    device_id: str


class HeartbeatResponse(Strict):
    device_id: str
    last_seen: float


class HealthResponse(Strict):
    """Liveness only. Says nothing about what hardware is attached.

    `/health` is the one unauthenticated endpoint, so it must not leak an inventory to an
    anonymous caller. Device counts live behind `/discover`.
    """

    status: Literal["ok"]
    mhs_version: str


# --------------------------------------------------------------------------------------
# JSON-RPC 2.0 envelope
# --------------------------------------------------------------------------------------


class JsonRpcRequest(Strict):
    jsonrpc: Literal["2.0"]
    method: str
    params: dict[str, Any] | list[Any] | None = None
    id: str | int | None = None

    @property
    def is_notification(self) -> bool:
        """A request with no `id` member at all. Per spec, it gets no response."""
        return "id" not in self.model_fields_set


class ReadParams(Strict):
    device_id: str
    target: str


class WriteParams(Strict):
    device_id: str
    target: str
    value: float | bool | str | list[float]
    confirm: bool = False


class EmergencyStopParams(Strict):
    device_id: str


class DiscoverParams(Strict):
    type: DeviceType | None = None
    online_only: bool = False
