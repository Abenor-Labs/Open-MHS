# PyBullet demo — the safety envelope, on screen

A self-contained 60-second sequence: an agent discovers a simulated KUKA iiwa, makes a safe
move, hallucinates a wildly out-of-range one, gets refused by Open-MHS, and corrects itself
from the refusal.

Running it writes **`open_mhs_cinematic_demo.mp4`** next to this file — narration burned in,
ready to upload. No screen recorder needed.

```bash
pip install -r examples/pybullet_demo/requirements-demo.txt
python examples/pybullet_demo/auto_demo.py
```

| Flag | Effect |
|---|---|
| `--headless` | No GUI window. For CI, or checking the script runs at all. |
| `--fast` | Skip typing delays and shorten holds. For iterating, not for recording. |
| `--speed X` | Scale typing delays (default `1.44`). Lower is faster. |
| `--dry-run` | Report the duration the export *will* have, in seconds, without rendering. |
| `--resolution WxH` | Frame size (default `1280x720`). `1920x1080` to publish, `640x360` to check quickly. |
| `--no-shadows` | ~4x faster offscreen export, flatter image. |
| `--no-video` | Run the sequence without writing an MP4. |
| `--out PATH` | Write the MP4 somewhere else. |

A full run is **59.9 seconds** of video, against a 60-second brief. Holds are not scaled by
`--speed`: the two-second pause on the blocked command is the shot that proves the arm did
not move.

Tune the length by measuring, not by guessing:

```bash
python examples/pybullet_demo/auto_demo.py --dry-run --speed 1.6
#   dry run: the exported video would be 65.3s (1958 frames at 30 fps)
```

`--dry-run` steps the whole sequence with no rendering and no sleeping, so it reports the
exact exported duration in about a second. The relationship is linear: 13.3 s of fixed
holds plus 32.3 s of typing per unit of `--speed`.

**Do not time this with a stopwatch on the terminal.** Wall-clock runtime is not the video
length — `time.sleep()` granularity (~15 ms on Windows, against a 4.2 ms tick) inflates the
live run well past the simulated duration that actually gets recorded. The first export of
this demo came out at 39.5 s while the terminal run had taken 62 s of wall clock. Trust
`--dry-run` and the frame count.

## How the video stays in sync

Frames are captured **per physics tick, not per wall-clock second** — one frame every 8
steps of a 240 Hz simulation, which is exactly 30 fps of simulated time. The video's
timeline and the simulation's timeline are therefore the same timeline, and the terminal
narration lands on definite ticks because `type_text` steps the simulation between
characters.

This matters because rendering is slower than real time offscreen. A wall-clock capture
loop would drop or double frames the moment a render overran its slot, and the arm would
drift out of step with the narration. Tick-driven capture cannot drift: the export takes
as long as it takes, and the file still plays at exactly the right speed.

## Export cost

Measured on this repo, offscreen (`--headless`, CPU TinyRenderer), 62 s of video:

| Setting | Per frame | Full export |
|---|---|---|
| 1280x720, shadows | ~380 ms | ~12 min |
| 1280x720, `--no-shadows` | ~115 ms | ~3.5 min |
| 640x360, shadows | ~120 ms | ~4 min |

Shadows are the cost, not resolution — TinyRenderer's shadow pass scales with the area of
the receiving geometry. The floor is deliberately only 5.2 m across for this reason: a
14 m slab measured 797 ms/frame against 378 ms for the one that ships.

Running **with** the GUI window renders through OpenGL on the GPU instead and is far
faster; the offscreen numbers above are the worst case.

Nothing else in the repo depends on `pybullet`, and this directory imports from `open_mhs/`
but never modifies it.

## The refusal is real

Phase 4 does not print a hardcoded error. `auto_demo.py` imports `server.safety` and calls
the same `check_write()` the middleware calls, against the same
[`demo_arm.mhs`](demo_arm.mhs) capability tag. What appears on screen is
`SafetyLimitViolation.to_rpc()` — the actual JSON-RPC error object, rendered verbatim:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "error": {
    "code": -32001,
    "message": "joint_0: 300.0 deg is outside the inclusive bound [-90.0, 90.0] deg",
    "data": {
      "target": "joint_0",
      "attempted": 300.0,
      "min": -90.0,
      "max": 90.0,
      "unit": "deg",
      "enforcement": "software",
      "on_violation": "reject",
      "rationale": "Past +/-90 degrees the arm sweeps through the operator's side of the bench..."
    }
  }
}
```

If the repo is not importable — someone copied this folder on its own — the script says so
on screen in yellow and falls back to an equivalent local bounds check. It does not quietly
pretend to be the real thing.

The script also refuses to produce a misleading recording. It exits non-zero if 300° is
*not* rejected, or if the arm moves more than 1° during the blocked command.

## Why the tag says 90 when the robot can do 170

The KUKA iiwa's own URDF permits ±170° on the base joint. The capability tag declares ±90.
That gap is the whole point: the firmware would happily accept 150°, and only the declared
envelope stops it. `enforcement` on that limit honestly reads `software`, not `firmware`.

## The look

- Background is keyed from the **segmentation mask**, not colour. PyBullet's
  `--background_color_*` connect options only reach the GUI window's clear colour — the
  offscreen renderer ignores them and clears to **white**, which would have handed us a
  bright frame in a dark-lab demo. A pixel is background because nothing was drawn there,
  which no colour test can tell you.
- Dark metallic floor with two cyan strips, repainted KUKA, low camera looking slightly up
  (`pitch=+10.5` puts the lens ~0.19 m off the floor), tight 34° lens, slow 1.1°/s orbit.
- During the block the whole frame flips to alarm: red border, red arm.
- All PyBullet UI is off, including the depth and segmentation preview thumbnails.

## Recording notes

- You do not need a screen recorder: the MP4 already contains the narration and the arm.
  Record the window only if you want the live terminal alongside it.
- The window opens with side panels off, shadows on, and the camera framed on the base
  joint, so it is ready to capture without fiddling.
- The 3D view carries its own caption (`BLOCKED BY OPEN-MHS · -32001` in red during phase
  4), which survives being cropped away from the terminal.
- The money shot is phase 4: the physics keeps stepping while the arm sits still. A frozen
  simulation and a refused command look identical in a still frame, so let it run.
- Phase 4 prints the joint angle before and after the blocked command from the actual
  feedback sensor. That line is worth leaving legible.
