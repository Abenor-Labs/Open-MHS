# Capability Tags

A **Capability Tag** is the file a device publishes so an AI agent can discover it,
understand it, and operate it safely. It is the contract between hardware and agent.

Canonical schema: [`schema/capability_schema.json`](../schema/capability_schema.json)
(JSON Schema draft 2020-12). File extension: `.mhs` or `.ohs`. Content is plain JSON.

## Design rules

1. **Declarative, not imperative.** A tag says what exists and what the bounds are. It
   never contains code, scripts, or command sequences.
2. **`additionalProperties: false` everywhere.** A typo like `actuatorz` is a hard error,
   not a silently ignored key. Silently ignoring a misspelled `safety_limits` would disable
   safety enforcement while validation still passed.
3. **Bounds are inclusive.** A write equal to `min` or `max` is permitted.
4. **Sensors are never writable.** The only way to change device state is an entry in
   `actuators[]`, and every actuator is bounded.
5. **Units are compared literally.** `deg` and `rad` are different units. The middleware
   does not convert; a mismatch is a validation error.

## Top-level fields

| Field | Required | Purpose |
|---|---|---|
| `mhs_version` | yes | Spec version: `"0.1"` or `"0.2"`. Any tag using `safety_limits[].conditions` must declare `"0.2"`. |
| `device_id` | yes | Globally unique, reboot-stable addressing key. |
| `name` | yes | Human-readable name. |
| `type` | yes | Controlled vocabulary device class. |
| `sensors` | yes | Read-only states. May be empty. |
| `actuators` | yes | Writable states. May be empty. |
| `safety_limits` | yes | Bounds. Must cover every actuator. |
| `vendor`, `model`, `firmware_version`, `description` | no | Provenance. |
| `driver` | no | Transport + Python driver module the middleware loads. |
| `emergency_stop` | no | Safe-state definition. Its absence is itself information. |
| `power` | no | Voltage, current, and `hazard_class`. |
| `discovery` | no | Registry URL and heartbeat interval. |
| `metadata` | no | Vendor extensions. **Never consulted for safety decisions.** |

## Sensors

```json
{
  "id": "ambient_temp",
  "name": "Ambient temperature",
  "datatype": "number",
  "unit": "degC",
  "nominal_range": { "min": -40.0, "max": 125.0 },
  "accuracy": 0.5,
  "sample_rate_hz": 2.0
}
```

`nominal_range` is what the sensor **can measure**. It is not a safety boundary and is
never used to reject a write. Numeric and `vector3` sensors must declare a `unit`; `enum`
sensors must declare `enum_values`.

## Actuators

```json
{
  "id": "joint_1",
  "datatype": "number",
  "unit": "deg",
  "write_mode": "absolute",
  "default": 0.0,
  "feedback_sensor": "joint_1_actual",
  "settle_time_ms": 800
}
```

- `feedback_sensor` names the sensor reporting the achieved state, so an agent can verify a
  write instead of assuming it took effect.
- `settle_time_ms` is the **longest** the actuator may take to reach a commanded value. It
  is a budget, not a fixed wait: the feedback sensor is polled and verification returns the
  moment it agrees, so a short move is confirmed quickly and a full-span move gets the whole
  budget. Declare the time of the slowest legal move, measured. A budget shorter than that
  makes verification report a desync for hardware doing exactly as told.
- `requires_confirmation: true` forces the middleware to obtain explicit human approval
  before dispatch. Use it for anything that grips, heats, energizes, or dispenses.
- `write_mode: "relative"` means the written value is a delta. Safety limits always apply
  to the **resulting absolute** value, never the delta.

## Safety limits

Two mutually exclusive forms, enforced by `oneOf` in the schema.

**Numeric bound** — requires `min`, `max`, and `unit`:

```json
{
  "target": "joint_1",
  "unit": "deg",
  "min": -90.0,
  "max": 90.0,
  "max_rate": 30.0,
  "enforcement": "firmware",
  "on_violation": "reject",
  "rationale": "Beyond +/-90 deg the arm collides with the bench mount."
}
```

**Discrete bound** — requires `allowed_values`, forbids `min` / `max`:

```json
{
  "target": "gripper",
  "allowed_values": ["open", "closed"],
  "enforcement": "firmware",
  "on_violation": "reject"
}
```

### `enforcement`

| Value | Meaning |
|---|---|
| `hardware` | Physically impossible to exceed (endstop, mechanical stop, fuse). |
| `firmware` | The device rejects it. |
| `software` | **Only this file** stands between the agent and the hardware. |

`software` is the default because it is the pessimistic assumption. Declaring `hardware`
when no physical stop exists is the most dangerous thing a tag author can do.

[`standards-map.md`](standards-map.md) defines these three values against the vocabulary of
ISO 13849 and ISO 12100, and states plainly what Open-MHS does not claim: no Performance
Level, no Safety Integrity Level, and no substitute for a physical interlock.

### `on_violation`

What the middleware does when a value falls outside the bound. All three modes are
implemented and enforced at both points.

| Mode | Behaviour |
|---|---|
| `reject` (default) | Refuses the write, transmits nothing, returns `-32001` with the attempted value and the violated bound. |
| `clamp` | Substitutes the nearest legal value and proceeds. The response carries `clamped: true`, the original `requested` value, and a `clamp_reason`. |
| `estop` | Drives the device to its declared safe state, then returns `-32001`. The write itself is still refused. |

`reject` is the default because it is the only mode that never surprises the caller.

**`clamp` is not "reject but friendlier".** The hardware ends up somewhere the caller did
not ask for, so an agent that ignores the `clamped` flag proceeds on a false belief about
the world. Every clamp is logged as a warning and reported in full for exactly that reason.
Choose it only where continuing at a safe value beats stopping — a reaction block driven
back to 80 °C keeps regulating, where a refused setpoint would leave it uncontrolled.

Under `clamp`, `max_rate` is clamped too: the move travels as far as the rate allows in
the requested direction, rather than being refused.

Two coherence rules are enforced at ingestion, because a tag must not declare a policy the
middleware cannot carry out:

- `clamp` on a **discrete** limit is rejected. There is no nearest member of a set of
  states, and guessing one is worse than refusing.
- `estop` requires `emergency_stop.supported: true`. There is nothing to stop to otherwise.

### `max_rate`

Guards against writes that are individually in-range but destructive as a step change.
Expressed in target units per second.

## Rules the schema cannot express

JSON Schema validates shape. It does not validate physics. A tag that passes
`jsonschema` is not yet valid. These are checked by the `schema-validator` skill and
must be enforced in CI:

| Rule | Why |
|---|---|
| Every `actuators[].id` has a `safety_limits[]` entry with the same `target`. | An unbounded actuator is an unbounded machine. |
| `min < max` on every numeric limit. | An inverted bound rejects everything, or is a typo that permits everything. |
| An actuator's `default` falls inside its own limit. | Otherwise power-on state is already a violation. |
| `safety_limits[].unit` equals the target actuator's `unit`. | The `deg`/`rad` mismatch is the classic field failure. |
| `id` unique across the **union** of `sensors[]` and `actuators[]`. | `read("x")` and `write("x")` must never be ambiguous. |
| `feedback_sensor` and `emergency_stop.safe_state` keys resolve to declared ids. | A dangling reference silently disables verification. |
| Discrete limit `allowed_values` is a subset of the actuator's `enum_values`. | A limit cannot permit a value the actuator cannot accept. |

## Validating a tag

```bash
pip install jsonschema
python - <<'PY'
import json
from jsonschema import Draft202012Validator
schema = json.load(open("schema/capability_schema.json"))
doc = json.load(open("examples/robotic_arm.mhs"))
errs = sorted(Draft202012Validator(schema).iter_errors(doc), key=lambda e: list(e.absolute_path))
for e in errs:
    print(f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}")
print("VALID" if not errs else f"INVALID ({len(errs)} errors)")
PY
```

## Worked examples

- [`examples/mock_temp_sensor.mhs`](../examples/mock_temp_sensor.mhs) — read-only device.
  No actuators, therefore no safety limits, therefore `write()` is rejected for every target.
- [`examples/robotic_arm.mhs`](../examples/robotic_arm.mhs) — exercises numeric bounds with
  a rate cap, a discrete bound, a confirmation-gated actuator, and an e-stop safe state.

## Conditional envelopes (spec 0.2)

`safety_limits[].conditions` is a list of `{when_target, equals, min?, max?, rationale?}`.
Conditions are evaluated in declaration order; the first whose `when_target` currently
reads `equals` wins. A condition may only **narrow** the base bound: a `min` below the
base `min` or a `max` above the base `max` is an ingestion error. The base bound is
therefore always the worst case the device will accept and can be quoted on its own.

Prefer a sensor as `when_target`, not the actuator that drives it. A gripper commanded
closed that did not close must not unlock a bound that assumes a payload is held.

If the `when_target` channel cannot be read at write time, the condition is skipped and
the base bound applies. A failed read can only tighten the envelope, never loosen it.

## `max_duration_s`

Longest time an actuator may be held away from its `default` before the middleware forces
it back. Enforced by the middleware's watchdog (see `docs/audit-log.md` for the event it
writes). When the timer expires the middleware writes `default` through the normal safety
path; if that write is refused (for example by `max_rate`) or the actuator declares no
`default`, the device's emergency stop runs instead. Enforced at the middleware only: a
driver used without the middleware does not run a watchdog.
