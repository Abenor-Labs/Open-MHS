# Working notes for Claude Code

Operational facts for this repo. Most of these were learned by breaking something, so
they are worth reading before you touch the demos.

## What this project is

Open-MHS is safety middleware between a language model and physical hardware. A device
declares its own limits in a `.mhs` capability tag; the middleware refuses anything outside
them **before any byte reaches the driver**, and returns an error carrying the real bound so
the model can correct itself.

The safety claims are the product. Treat them as load-bearing.

## Invariants — do not break these

1. **Never widen a safety bound to make something validate.** If a device cannot be
   expressed, that is a schema RFC, not a local edit.
2. **Never weaken a test to make a change pass.** Fix the test in its own commit, with a
   reason.
3. **Never override `BaseDevice.write`.** Wrapping it to add a side effect (see
   `examples/pybullet_demo/live_lab.py`) is fine; reimplementing the safety path is not.
4. **Both enforcement points stay.** Middleware before dispatch, driver before transmit.
   `tests/test_safety.py` fails if you delete either — that is deliberate.
5. **Rejection tests assert zero transmissions**, not just that an error came back. A
   refused write that still emitted bytes passes a return-value assertion and breaks a
   machine.
6. **A capability tag must not claim what the hardware does not do.** This was violated
   once: cube positions were labelled a "part-locating system" while the driver read
   ground truth out of the physics engine. The fix was a `pose_source` sensor reporting
   `vision` or `ground_truth` per read. Do not describe a sensor you did not implement.

## Dependency pins that matter

| Pin | Why |
| --- | --- |
| `mujoco==3.3.7` | robosuite 1.5.2 declares `mujoco>=3.3.0`, but 3.4+ renamed `MjData.qM` to `M`, which robosuite's OSC still calls. Resolving to latest (3.12) crashes on `env.reset()` with `AttributeError: 'MjData' object has no attribute 'qM'`. |
| `anyio>=4.5` | The `mcp` SDK needs it. On anyio 3.x the MCP stdio server dies at startup with `TypeError: 'function' object is not subscriptable`. |
| `pyserial` | Optional. Imported only when a real port is opened, so mock-only installs work without it. |
| `pybullet` | No wheel for CPython 3.12 on Windows — it compiles from an 80 MB source tarball and takes ~15 minutes. Budget for it. |

## Threading: the rule that bit twice

**MuJoCo and PyBullet are not thread-safe, and a GL context belongs to the thread that
created it.** So in every live demo:

- the simulator owns the **main thread** (`build()` then `loop()`),
- uvicorn runs on a **worker thread**,
- drivers never touch the sim — they push to a `queue.Queue` and read a snapshot dict
  under a lock.

Get this backwards and the failure is silent, not loud: offscreen rendering returns garbage
instead of raising. The symptom was `pose_source: ground_truth` with `detection_pixels: 1`
while the same code worked fine single-threaded. If vision "stops working" in a live cell,
check which thread is stepping before you debug the CV.

## Process management on Windows / Git Bash

- **`pkill -f name` silently fails.** Use `taskkill //PID <pid> //F` and verify with
  `netstat -ano | grep :8000`.
- **`nohup cmd &` dies when the shell exits.** Use the Bash tool's `run_in_background`.
- A stale process holding port 8000 makes the new one bind-fail *quietly*: the new window
  opens, but every command hits the old server. If readings look impossible (a cube already
  grasped in a fresh scene), check the port owner first.

## The tooling itself

- **Heredocs with backslash escapes get mangled.** Writing `\\n` inside a `python - <<'PY'`
  block can arrive as a real newline and produce a syntax error. Use the Write/Edit tools
  for content containing escapes, or build strings with `chr(92)`.
- **`README.md` gets clobbered by the editor.** A Markdown formatter reflows tables and has
  twice stripped the hero block. `.prettierignore` covers `*.md`; if content vanishes,
  check `git log` before assuming corruption — one disappearance was a real user commit.

## Measured constants — do not re-guess these

**robosuite cell** (`examples/robosuite_demo/`) — four coloured blocks on a table

Launch it with `python examples/robosuite_demo/run_cell.py --viewer`. Devices:
`cv-camera-01` (read-only, per-block `<name>_x/_y/_z/_source/_pixels`) and `panda-arm-01`
(`tcp_x/y/z`, `gripper_state`). Blocks are `red_block`, `green_block`, `blue_block`,
`yellow_block`; `grasping` reports which one is held, by contact check, or `nothing`.
Occlusion is reported per block — a covered block reads `ground_truth` while the rest
still read `vision`.

| | |
| --- | --- |
| Table top | `z = 0.800` |
| `tcp_z` envelope | `0.83 .. 1.15` — floor is what stops the gripper entering the table |
| `tcp_x/y` envelope | `±0.22` (26/27 sampled corners hold within 20 mm) |
| Grasp height | **`≤ 0.835`**. At 0.845 and above the gripper closes on air |
| Servo | `KP = 10`, clipped to `[-1, 1]`; OSC clamps to ±0.05 m/step → 0.2 mm accuracy |
| Vision error | ~16 mm in XY, ~29 mm total. Blob centroid sits on the cube's visible face |
| Vision-only pick | 5/5 success |

**pybullet cell** (`examples/pybullet_demo/live_lab.py`)

- Bench top `0.626`; the arm is mounted **on** the bench. On the floor its lower links foul
  the bench edge and IK error is 10× worse (577 mm vs 34 mm).
- **IK must pin the tool orientation.** Position-only IK is underdetermined; the solver
  flips elbow and wrist between commands until the arm folds through the table. With
  `targetOrientation` + joint limits, error drops from 30–100 mm to ~1 mm.
- `plane.urdf` **is** the blue checkerboard. Use a dark slab for the lab look.
- PyBullet's `--background_color_*` connect options do not reach the offscreen renderer
  (it clears to **white**) and did not take in the GUI either. Key the background off the
  segmentation mask instead — a pixel is background because nothing was drawn there.

## Running things

```bash
pytest                       # 372 tests, no hardware, ~25 s
ruff check .

# robosuite digital twin (flagship)
pip install -r examples/robosuite_demo/requirements-demo.txt
export OPEN_MHS_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python examples/robosuite_demo/run_cell.py --viewer     # interactive MuJoCo viewer

# pybullet cell (no MuJoCo needed)
python examples/pybullet_demo/live_lab.py
```

`--viewer` uses `mujoco.viewer.launch_passive` — left-drag orbits, right-drag pans, wheel
zooms. robosuite's own `--render` window is a fixed camera with no mouse input.

## Video export

`examples/pybullet_demo/auto_demo.py` writes an MP4. **Never time it with a stopwatch on
the terminal** — `time.sleep()` granularity (~15 ms on Windows against a 4.2 ms tick)
inflates wall clock well past the recorded duration. An early export came out at 39.5 s
while the terminal run took 62 s. Use `--dry-run`, which reports the exact exported length
in about a second without rendering.

## Before you claim something works

This repo's whole argument is that claims are verified rather than asserted. Hold the code
to it:

- Read the finished artifact, do not trust the writer. Three real bugs were found only by
  reading back exported frames and files.
- When a test suite passes first try, mutate the code and confirm the tests fail. Doing
  that here found a genuine coverage hole in the middleware safety check.
- State what you did **not** verify. "Not tested against real hardware" is a fine answer;
  an unstated gap is not.
