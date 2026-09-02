# RFC 0001: `period` for modular quantities

| | |
|---|---|
| **Status** | draft |
| **Spec version** | 0.3 if accepted |
| **Author** | Open-MHS maintainer |
| **Created** | 2026-09-02 |
| **Decided** | |

## The device that cannot be described today

The Franka Panda in `examples/robosuite_demo/`. Its `tcp_yaw` actuator squares the gripper
jaws to a block before grasping. A cube is indistinguishable under 90-degree rotation, so
the wrist never needs to turn more than 45 degrees to line up, and both the commanded yaw
and the measured `tcp_yaw_actual` are wrapped into `[0, 90)`.

**Zero and ninety are the same orientation.** The driver knows this: its servo takes the
shortest modular path and correctly does nothing when asked to move from 89.98 to 0. The
middleware does not know it, because nothing in a Capability Tag can say so. Verification
compares `abs(observed - commanded) <= tolerance`, sees an 89.98-degree error, and reports
a state desync for a wrist that is exactly where it was asked to be.

Found by the benchmark against the live cell, not by reading code:

```
cmd  90.0 -> ok       reached  90.00 in  0.5s
cmd   0.0 -> err -32003 reached  90.00 in 12.5s   <- same orientation, reported as divergence
cmd  45.0 -> ok       reached  44.95 in  0.5s
cmd   0.0 -> ok       reached   0.01 in  0.5s     <- identical request, from 45, succeeds
```

This is not specific to one simulator. It is true of any continuous rotary axis, any
turntable, any filter wheel, any carousel, and any heading.

## What breaks if we do nothing

Three things, in increasing order of seriousness.

**A false desync.** The caller is told its model of the world is wrong when it is right,
and the middleware's instruction on `-32003` is to stop and consider an emergency stop.
Teaching an agent to distrust a correct reading is worse than saying nothing.

**A real desync goes unnoticed at the wrap point.** A wrist genuinely stuck at 90 while
commanded to 0 is indistinguishable from one that arrived correctly, once anybody
"fixes" this by widening the tolerance or removing the feedback sensor.

**Bounds are wrong too, not just verification.** `min: 0, max: 90` on a modular quantity
is an interval on a circle, and `max_rate` computed as `abs(new - old) / dt` reports a
1-degree move across the wrap as an 89-degree lurch. Under `on_violation: clamp` that
would clamp a legal command to the far side of the circle.

This puts pressure on invariant 6 in `CLAUDE.md`: a tag must not claim what the hardware
does not do. Today the honest workaround is to delete `feedback_sensor` from `tcp_yaw`,
which throws away verification entirely rather than performing it correctly.

## Proposal

One optional field on a numeric channel, `period`, giving the value at which the quantity
wraps back to its start.

```json
{
  "id": "tcp_yaw",
  "datatype": "number",
  "unit": "deg",
  "period": 90.0,
  "feedback_sensor": "tcp_yaw_actual",
  "settle_time_ms": 2000
}
```

Prose for `docs/capability-tags.md`:

> `period` marks a quantity that wraps: an angle, a heading, a carousel index. With it
> declared, the distance between two values is the shorter way around the circle, so a
> command of `0` and a reading of `89.98` on a 90-degree period differ by 0.02, not by
> 89.98. Declare it only where the wrap is physically real. A linear axis with a period is
> a bound the middleware will refuse to enforce correctly.

Schema fragment, added to `$defs.sensor` and `$defs.actuator`:

```json
"period": {
  "description": "For a quantity that wraps (an angle, a heading): the value at which it returns to its start. Distances are then measured the short way around. Omit for a linear quantity.",
  "type": "number",
  "exclusiveMinimum": 0
}
```

## What a reader that does not understand this field does

It **rejects the tag**, because tags validate strictly. That is the behaviour we want. A
0.2 reader that silently ignored `period` would enforce linear bounds and linear rate
limits on a circular quantity, which is the bug this RFC exists to fix, arrived at by a
different route. Better to refuse the device than to operate it on a wrong model.

Spec version bumps to **0.3**.

## Enforcement

- **Ingestion** (`open_mhs/server/models.py`): `period` requires a numeric datatype;
  `max - min` must not exceed `period`, because an interval longer than the circle is the
  whole circle; a channel naming a `feedback_sensor` must agree with that sensor's
  `period`, since comparing a modular reading to a linear command is the original bug.
- **Runtime** (`open_mhs/server/safety.py`): one helper, `circular_delta(a, b, period)`,
  used by the range check, by `max_rate`, and by `BaseDevice._verify`. Where `period` is
  absent every path keeps today's linear arithmetic, so nothing changes for existing tags.
- **On violation**: unchanged. A value outside a modular envelope is still `-32001` with
  the bound in `data`; the difference is only how "outside" is computed.

## Tests that must exist before this ships

- A write at the wrap point verifies: commanded `0`, observed `period - epsilon`, accepted.
- A genuine desync at the wrap point is still caught: commanded `0`, observed `period / 2`,
  raises `-32003`. This is the test that stops the fix from becoming a blindfold.
- `max_rate` across the wrap measures the short way: `period - 1` to `1` is a 2-unit move.
- An out-of-envelope modular value is refused with zero transmissions, as everywhere else.
- Ingestion rejects `period` on a non-numeric channel, an interval wider than the period,
  and a mismatch between an actuator's period and its feedback sensor's.
- **Mutation to run:** make `circular_delta` return the linear difference. The wrap-point
  verification test must fail. If it does not, the test is not testing this.
- The benchmark corpus gains a wrap-point probe, and `panda-arm-01.tcp_yaw.at-min` must go
  from a false refusal to a pass.

## Alternatives considered

**Do nothing, and delete `feedback_sensor` from modular actuators.** This is what the
implementation does today as a stopgap. It is honest but it discards closed-loop
verification for a whole class of axis, and the second failure mode above says the class
that loses it is one where a stuck axis is hardest to see.

**Widen the tolerance to cover the period.** A tolerance of 90 degrees on a 90-degree
period accepts every reading, including a wrist that never moved. This converts a false
alarm into a silent failure, which is the wrong direction.

**Let the driver override verification.** `BaseDevice` deliberately owns the safety path
(invariant 3), and pushing modular arithmetic into each driver means every driver author
reimplements it, some of them wrongly, with no way for the middleware to check.

**Infer it from the unit.** `deg` does not imply a 360 period, and it certainly does not
imply 90. The period here comes from the *workpiece*, a cube's four-fold symmetry, not
from the unit. It has to be declared.

**A boolean `circular: true` with the period taken as 360.** Does not describe this
device, whose period is 90.

## Effect on existing tags

None. `period` is optional and absent everywhere today; every path keeps its linear
arithmetic when it is missing. The one tag that will adopt it is
`examples/robosuite_demo/panda_arm.mhs`, whose `tcp_yaw` gains `"period": 90.0` and
regains its `feedback_sensor`.

Because any added field bumps the spec version, every 0.1 and 0.2 tag in the tree stays
valid and unchanged, and a 0.3 reader continues to accept them.
