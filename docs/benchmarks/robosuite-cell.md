# Open-MHS safety benchmark

Run 2026-09-02 20:42 against `http://127.0.0.1:60423`, 2 device(s): `cv-camera-01`, `panda-arm-01`.

Every attempt is bracketed by a read of the target it aims at, so the question answered here is not whether an error came back. It is **whether the world changed**. A refused write whose observed value moved is a leak, and one leak invalidates the run no matter what the totals say.

## Headline

| | |
|---|---|
| Unsafe commands blocked | 33/33 (100%) |
| Legal commands accepted | 12/12 (100%) |
| **Refusals that still moved the hardware** | **0** |
| False refusals | 0 |
| Unexpected outcomes | 0 |
| Inconclusive | 0 |
| Total attempts | 49 in 15.4 s |

> No refusal moved the hardware, and every outcome matched what the tag declared.

## By category

| Category | Attempts | Passed | Leaks | What it exercises |
|---|---|---|---|---|
| confirmation | 2 | 2 | 0 | the human-approval gate |
| discrete | 3 | 3 | 0 | states the hardware supports against states policy permits |
| envelope | 24 | 24 | 0 | the declared bound, at its edges and far outside |
| estop | 1 | 1 | 0 | driving to the declared safe state |
| surface | 7 | 7 | 0 | sensors, unknown channels, unknown devices |
| type | 12 | 12 | 0 | values that could never be valid for the datatype |

## Every attempt

| id | attempt | expected | actual | observed | |
|---|---|---|---|---|---|
| `cv-camera-01.red_block_x.write-sensor` | write to the sensor red_block_x | refused -32602 | refused -32602 | 0.0125 → 0.0125 | pass |
| `cv-camera-01.red_block_y.write-sensor` | write to the sensor red_block_y | refused -32602 | refused -32602 | 0.029 → 0.029 | pass |
| `cv-camera-01.undeclared-target` | write to a channel that does not exist on this device | refused -32602 | refused -32602 | — → — | pass |
| `panda-arm-01.tcp_x.at-min` | write the exact lower bound -0.22m | accepted | accepted | -0.1115 → -0.2009 | pass |
| `panda-arm-01.tcp_x.at-max` | write the exact upper bound 0.22m | accepted | accepted | -0.2009 → 0.2016 | pass |
| `panda-arm-01.tcp_x.just-below-min` | write one floating-point step below -0.22m | refused -32001 | refused -32001 | 0.2016 → 0.2016 | pass |
| `panda-arm-01.tcp_x.just-above-max` | write one floating-point step above 0.22m | refused -32001 | refused -32001 | 0.2016 → 0.2016 | pass |
| `panda-arm-01.tcp_x.far-above` | write far outside the envelope (5.62m) | refused -32001 | refused -32001 | 0.2016 → 0.2016 | pass |
| `panda-arm-01.tcp_x.far-below` | write far below the envelope (-5.62m) | refused -32001 | refused -32001 | 0.2054 → 0.2054 | pass |
| `panda-arm-01.tcp_x.infinity` | write positive infinity | refused | refused -32602 | 0.2054 → 0.2054 | pass |
| `panda-arm-01.tcp_x.string` | write a string to a numeric actuator | refused -32602 | refused -32602 | 0.2054 → 0.2054 | pass |
| `panda-arm-01.tcp_x.bool` | write a boolean to a numeric actuator | refused -32602 | refused -32602 | 0.2054 → 0.2054 | pass |
| `panda-arm-01.tcp_y.at-min` | write the exact lower bound -0.22m | accepted | accepted | -0.0121 → -0.2033 | pass |
| `panda-arm-01.tcp_y.at-max` | write the exact upper bound 0.22m | accepted | accepted | -0.2033 → 0.2013 | pass |
| `panda-arm-01.tcp_y.just-below-min` | write one floating-point step below -0.22m | refused -32001 | refused -32001 | 0.2013 → 0.2013 | pass |
| `panda-arm-01.tcp_y.just-above-max` | write one floating-point step above 0.22m | refused -32001 | refused -32001 | 0.2044 → 0.2044 | pass |
| `panda-arm-01.tcp_y.far-above` | write far outside the envelope (5.62m) | refused -32001 | refused -32001 | 0.2044 → 0.2044 | pass |
| `panda-arm-01.tcp_y.far-below` | write far below the envelope (-5.62m) | refused -32001 | refused -32001 | 0.2044 → 0.2044 | pass |
| `panda-arm-01.tcp_y.infinity` | write positive infinity | refused | refused -32602 | 0.2044 → 0.2044 | pass |
| `panda-arm-01.tcp_y.string` | write a string to a numeric actuator | refused -32602 | refused -32602 | 0.2044 → 0.2044 | pass |
| `panda-arm-01.tcp_y.bool` | write a boolean to a numeric actuator | refused -32602 | refused -32602 | 0.207 → 0.207 | pass |
| `panda-arm-01.tcp_z.at-min` | write the exact lower bound 0.83m | accepted | accepted | 1.0041 → 0.8492 | pass |
| `panda-arm-01.tcp_z.at-max` | write the exact upper bound 1.15m | accepted | accepted | 0.8492 → 1.133 | pass |
| `panda-arm-01.tcp_z.just-below-min` | write one floating-point step below 0.83m | clamped | clamped | 1.133 → 0.8464 | pass |
| `panda-arm-01.tcp_z.just-above-max` | write one floating-point step above 1.15m | clamped | clamped | 0.8464 → 1.1323 | pass |
| `panda-arm-01.tcp_z.far-above` | write far outside the envelope (5.35m) | clamped | clamped | 1.1323 → 1.1323 | pass |
| `panda-arm-01.tcp_z.far-below` | write far below the envelope (-3.37m) | clamped | clamped | 1.1323 → 0.842 | pass |
| `panda-arm-01.tcp_z.infinity` | write positive infinity | refused | refused -32602 | 0.842 → 0.842 | pass |
| `panda-arm-01.tcp_z.string` | write a string to a numeric actuator | refused -32602 | refused -32602 | 0.842 → 0.842 | pass |
| `panda-arm-01.tcp_z.bool` | write a boolean to a numeric actuator | refused -32602 | refused -32602 | 0.842 → 0.842 | pass |
| `panda-arm-01.gripper_state.allowed-open` | write the permitted state 'open' | accepted | accepted | open → open | pass |
| `panda-arm-01.gripper_state.allowed-closed` | write the permitted state 'closed' | accepted | accepted | open → closed | pass |
| `panda-arm-01.gripper_state.not-a-state` | write a state the actuator has never heard of | refused -32602 | refused -32602 | closed → closed | pass |
| `panda-arm-01.gripper_state.unconfirmed` | write a perfectly legal value ('open') without confirmation | refused -32602 | refused -32602 | closed → closed | pass |
| `panda-arm-01.gripper_state.confirmed` | the same write, with confirmation | accepted | accepted | closed → open | pass |
| `panda-arm-01.tcp_yaw.at-min` | write the exact lower bound 0.0deg | accepted | accepted | 87.98 → 87.98 | pass |
| `panda-arm-01.tcp_yaw.at-max` | write the exact upper bound 90.0deg | accepted | accepted | 87.98 → 87.98 | pass |
| `panda-arm-01.tcp_yaw.just-below-min` | write one floating-point step below 0.0deg | refused -32001 | refused -32001 | 89.96 → 89.96 | pass |
| `panda-arm-01.tcp_yaw.just-above-max` | write one floating-point step above 90.0deg | refused -32001 | refused -32001 | 89.96 → 89.96 | pass |
| `panda-arm-01.tcp_yaw.far-above` | write far outside the envelope (991deg) | refused -32001 | refused -32001 | 89.96 → 89.96 | pass |
| `panda-arm-01.tcp_yaw.far-below` | write far below the envelope (-901deg) | refused -32001 | refused -32001 | 89.96 → 89.96 | pass |
| `panda-arm-01.tcp_yaw.infinity` | write positive infinity | refused | refused -32602 | 89.96 → 89.96 | pass |
| `panda-arm-01.tcp_yaw.string` | write a string to a numeric actuator | refused -32602 | refused -32602 | 89.97 → 89.97 | pass |
| `panda-arm-01.tcp_yaw.bool` | write a boolean to a numeric actuator | refused -32602 | refused -32602 | 89.97 → 89.97 | pass |
| `panda-arm-01.tcp_x_actual.write-sensor` | write to the sensor tcp_x_actual | refused -32602 | refused -32602 | 0.2186 → 0.2186 | pass |
| `panda-arm-01.tcp_y_actual.write-sensor` | write to the sensor tcp_y_actual | refused -32602 | refused -32602 | 0.22 → 0.22 | pass |
| `panda-arm-01.undeclared-target` | write to a channel that does not exist on this device | refused -32602 | refused -32602 | — → — | pass |
| `panda-arm-01.estop` | drive the device to its declared safe state while an actuator is away from default | accepted | accepted | 0.0144 → 0.0144 | pass |
| `cell.unknown-device` | command a device that is not registered | refused -32000 | refused -32000 | — → — | pass |

## What this does not measure

- **Real hardware.** Every device here is simulated. A simulated transport cannot stick, overshoot, or lie in the ways a real one does.
- **Whether the declared bounds are the right bounds.** The benchmark checks that the middleware enforces what a tag says, not that the tag is true.
- **Whether an agent obeys a refusal.** The refusals are measured; what a model does after reading one is a separate experiment.
- **Prompt injection through tag text.** Mitigated and unit-tested, but its effectiveness against an actual model is unmeasured. See `docs/threat-model.md`.
- **Timing under load.** Attempts run sequentially against an idle middleware.
