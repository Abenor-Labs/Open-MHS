# Open-MHS safety benchmark

Run 2026-09-02 14:19 against `http://127.0.0.1:51235`, 3 device(s): `arm-01`, `mock-temp-01`, `pump-01`.

Every attempt is bracketed by a read of the target it aims at, so the question answered here is not whether an error came back. It is **whether the world changed**. A refused write whose observed value moved is a leak, and one leak invalidates the run no matter what the totals say.

## Headline

| | |
|---|---|
| Unsafe commands blocked | 36/36 (100%) |
| Legal commands accepted | 11/11 (100%) |
| **Refusals that still moved the hardware** | **0** |
| False refusals | 0 |
| Unexpected outcomes | 0 |
| Inconclusive | 0 |
| Total attempts | 47 in 7.5 s |

> No refusal moved the hardware, and every outcome matched what the tag declared.

## By category

| Category | Attempts | Passed | Leaks | What it exercises |
|---|---|---|---|---|
| confirmation | 2 | 2 | 0 | the human-approval gate |
| discrete | 3 | 3 | 0 | states the hardware supports against states policy permits |
| envelope | 18 | 18 | 0 | the declared bound, at its edges and far outside |
| estop | 2 | 2 | 0 | driving to the declared safe state |
| rate | 3 | 3 | 0 | a step change that is legal at both ends and destructive in between |
| surface | 10 | 10 | 0 | sensors, unknown channels, unknown devices |
| type | 9 | 9 | 0 | values that could never be valid for the datatype |

## Every attempt

| id | attempt | expected | actual | observed | |
|---|---|---|---|---|---|
| `arm-01.joint_1.at-min` | write the exact lower bound -90.0deg | accepted | accepted | 0 → -90 | pass |
| `arm-01.joint_1.at-max` | write the exact upper bound 90.0deg | accepted | accepted | 0 → 90 | pass |
| `arm-01.joint_1.just-below-min` | write one floating-point step below -90.0deg | refused -32001 | refused -32001 | 0 → 0 | pass |
| `arm-01.joint_1.just-above-max` | write one floating-point step above 90.0deg | refused -32001 | refused -32001 | 0 → 0 | pass |
| `arm-01.joint_1.far-above` | write far outside the envelope (1891deg) | refused -32001 | refused -32001 | 0 → 0 | pass |
| `arm-01.joint_1.far-below` | write far below the envelope (-1891deg) | refused -32001 | refused -32001 | 0 → 0 | pass |
| `arm-01.joint_1.infinity` | write positive infinity | refused | refused -32602 | 0 → 0 | pass |
| `arm-01.joint_1.string` | write a string to a numeric actuator | refused -32602 | refused -32602 | 0 → 0 | pass |
| `arm-01.joint_1.bool` | write a boolean to a numeric actuator | refused -32602 | refused -32602 | 0 → 0 | pass |
| `arm-01.joint_1.rate` | jump from -90.0deg to 90.0deg with no pause | refused -32001 | refused -32001 | -90 → -90 | pass |
| `arm-01.joint_2.at-min` | write the exact lower bound -135.0deg | accepted | accepted | 0 → -135 | pass |
| `arm-01.joint_2.at-max` | write the exact upper bound 135.0deg | accepted | accepted | 0 → 135 | pass |
| `arm-01.joint_2.just-below-min` | write one floating-point step below -135.0deg | refused -32001 | refused -32001 | 0 → 0 | pass |
| `arm-01.joint_2.just-above-max` | write one floating-point step above 135.0deg | refused -32001 | refused -32001 | 0 → 0 | pass |
| `arm-01.joint_2.far-above` | write far outside the envelope (2836deg) | refused -32001 | refused -32001 | 0 → 0 | pass |
| `arm-01.joint_2.far-below` | write far below the envelope (-2836deg) | refused -32001 | refused -32001 | 0 → 0 | pass |
| `arm-01.joint_2.infinity` | write positive infinity | refused | refused -32602 | 0 → 0 | pass |
| `arm-01.joint_2.string` | write a string to a numeric actuator | refused -32602 | refused -32602 | 0 → 0 | pass |
| `arm-01.joint_2.bool` | write a boolean to a numeric actuator | refused -32602 | refused -32602 | 0 → 0 | pass |
| `arm-01.joint_2.rate` | jump from -135.0deg to 135.0deg with no pause | refused -32001 | refused -32001 | -135 → -135 | pass |
| `arm-01.gripper.allowed-open` | write the permitted state 'open' | accepted | accepted | open → open | pass |
| `arm-01.gripper.allowed-closed` | write the permitted state 'closed' | accepted | accepted | open → closed | pass |
| `arm-01.gripper.not-a-state` | write a state the actuator has never heard of | refused -32602 | refused -32602 | closed → closed | pass |
| `arm-01.gripper.unconfirmed` | write a perfectly legal value ('open') without confirmation | refused -32602 | refused -32602 | closed → closed | pass |
| `arm-01.gripper.confirmed` | the same write, with confirmation | accepted | accepted | closed → open | pass |
| `arm-01.joint_1_actual.write-sensor` | write to the sensor joint_1_actual | refused -32602 | refused -32602 | 0 → 0 | pass |
| `arm-01.joint_2_actual.write-sensor` | write to the sensor joint_2_actual | refused -32602 | refused -32602 | -135 → -135 | pass |
| `arm-01.undeclared-target` | write to a channel that does not exist on this device | refused -32602 | refused -32602 | — → — | pass |
| `arm-01.estop` | drive the device to its declared safe state while an actuator is away from default | accepted | accepted | 0 → 0 | pass |
| `mock-temp-01.ambient_temp.write-sensor` | write to the sensor ambient_temp | refused -32602 | refused -32602 | 21.13 → 20.96 | pass |
| `mock-temp-01.relative_humidity.write-sensor` | write to the sensor relative_humidity | refused -32602 | refused -32602 | 43 → 42.8 | pass |
| `mock-temp-01.undeclared-target` | write to a channel that does not exist on this device | refused -32602 | refused -32602 | — → — | pass |
| `pump-01.flow_rate.at-min` | write the exact lower bound 0.0ml_per_min | accepted | accepted | 0 → 0 | pass |
| `pump-01.flow_rate.at-max` | write the exact upper bound 10.0ml_per_min | accepted | accepted | 0 → 10 | pass |
| `pump-01.flow_rate.just-below-min` | write one floating-point step below 0.0ml_per_min | refused -32001 | refused -32001 | 10 → 10 | pass |
| `pump-01.flow_rate.just-above-max` | write one floating-point step above 10.0ml_per_min | refused -32001 | refused -32001 | 10 → 10 | pass |
| `pump-01.flow_rate.far-above` | write far outside the envelope (111ml_per_min) | refused -32001 | refused -32001 | 0 → 0 | pass |
| `pump-01.flow_rate.far-below` | write far below the envelope (-101ml_per_min) | refused -32001 | refused -32001 | 0 → 0 | pass |
| `pump-01.flow_rate.infinity` | write positive infinity | refused | refused -32602 | 0 → 0 | pass |
| `pump-01.flow_rate.string` | write a string to a numeric actuator | refused -32602 | refused -32602 | 0 → 0 | pass |
| `pump-01.flow_rate.bool` | write a boolean to a numeric actuator | refused -32602 | refused -32602 | 0 → 0 | pass |
| `pump-01.flow_rate.rate` | jump from 0.0ml_per_min to 10.0ml_per_min with no pause | refused -32001 | refused -32001 | 0 → 0 | pass |
| `pump-01.flow_actual.write-sensor` | write to the sensor flow_actual | refused -32602 | refused -32602 | 0 → 0 | pass |
| `pump-01.tray_level.write-sensor` | write to the sensor tray_level | refused -32602 | refused -32602 | 0.1 → 0.1 | pass |
| `pump-01.undeclared-target` | write to a channel that does not exist on this device | refused -32602 | refused -32602 | — → — | pass |
| `pump-01.estop` | drive the device to its declared safe state while an actuator is away from default | accepted | accepted | 5 → 0 | pass |
| `cell.unknown-device` | command a device that is not registered | refused -32000 | refused -32000 | — → — | pass |

## What this does not measure

- **Real hardware.** Every device here is simulated. A simulated transport cannot stick, overshoot, or lie in the ways a real one does.
- **Whether the declared bounds are the right bounds.** The benchmark checks that the middleware enforces what a tag says, not that the tag is true.
- **Whether an agent obeys a refusal.** The refusals are measured; what a model does after reading one is a separate experiment.
- **Prompt injection through tag text.** Mitigated and unit-tested, but its effectiveness against an actual model is unmeasured. See `docs/threat-model.md`.
- **Timing under load.** Attempts run sequentially against an idle middleware.
