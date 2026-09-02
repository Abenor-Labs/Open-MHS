# Recording a demo

The hard part of filming a safety system is that the interesting outcome is **nothing
happening**. A refused command looks identical to a command that was never sent, so the
recording has to show the refusal and the stillness in the same frame.

`examples/showcase.py` is built for that. It drives the real MCP tools at a readable pace,
prints the literal text a model receives, and after every refusal reads the hardware back
and states on screen that it did not move.

It also refuses to produce a misleading recording. Every beat asserts what the middleware
was supposed to do, and the script exits non-zero if an unsafe command is accepted, if a
refused command moves anything, or if a legal command is refused. A recording of a green
run is therefore a recording of a working system rather than a rehearsal.

## Before you record

PowerShell, which is what you are probably in:

```powershell
$env:OPEN_MHS_AUTH_TOKEN = "robosuite-demo-token-0123456789ab"
$env:OPEN_MHS_AUDIT_LOG  = "demo-audit.jsonl"
Remove-Item demo-audit.jsonl -ErrorAction SilentlyContinue
python examples/robosuite_demo/run_cell.py --viewer --pov
```

Then, in a second window with the same two variables set:

```powershell
python examples/showcase.py --fast     # confirm it passes BEFORE you press record
```

Both variables matter. The audit log is written by the **server**, so the showcase can
only read this run's trail if the server was started with the same path. And do not delete
the log while the server is running: it keeps numbering from memory, so the new file starts
mid-chain and `verify` correctly reports it as broken — which is the audit log working, not
a bug, but it will look like one on camera.

If that exits non-zero, fix the finding rather than recording around it.

## The layout

Two panes side by side. The terminal is the story; the simulator is the evidence.

```
┌─────────────────────────────┬──────────────────────────┐
│  terminal: showcase.py      │  the cell                │
│  tool calls and replies     │  MuJoCo viewer, PyBullet │
│  in the model's own words   │  window, or real hardware│
└─────────────────────────────┴──────────────────────────┘
```

For the visual pane, in decreasing order of how good it looks:

```bash
python examples/robosuite_demo/run_cell.py --viewer --pov   # arm, blocks, wrist camera
python examples/pybullet_demo/live_lab.py                   # no MuJoCo needed
```

Then point the showcase at whichever is running:

```bash
python examples/showcase.py --url http://127.0.0.1:8000 --pace 1.2
```

Nothing in the script is specific to a device: it reads the tags and narrates whatever is
registered, so the same eight beats work on the mock cell, on either simulator, and on real
hardware.

## Shot 0: the before

Open with the same cell and the same command, with no middleware at all:

```bash
python examples/robosuite_demo/without_mhs.py --viewer
```

It moves over the red block and commands the tool to z = 0.70 m, ten centimetres below a
table top at 0.800 m. Nothing checks it. The gripper is driven into the table and keeps
pushing; measured, the tool bottoms out at about 0.808 m, and the only thing that stopped
it was the table. The script exits non-zero if the physics do not show that, so the
"before" is measured as honestly as the "after".

Cut from that to the middleware refusing the identical command, and the video has made
its argument before anyone says a word.

## The shot list

| # | Beat | What the camera should catch |
|---|---|---|
| 1 | The hardware describes itself | The bounds appearing in the reply. Nobody typed them into the prompt. |
| 2 | One call for the whole cell | Several devices, one snapshot. |
| 3 | A legal command | **The arm moves.** Establish that it can. |
| 4 | An illegal command | The red refusal, then the read-back proving the arm is where it was. Hold on the still arm for a beat. |
| 5 | The confirmation gate | Refused, then the same command accepted once a person approves. |
| 6 | The plan check | A whole multi-step plan rejected before anything is transmitted. |
| 7 | Stop everything | Every device driven to its declared safe state at once. |
| 8 | The audit trail | The chain verifying, with the refusals counted. |

Beat 4 is the one worth re-shooting until it reads. Everything else is context.

## Terminal settings that film well

- Dark background, a font at 16pt or larger. The text has to survive compression.
- At least 100 columns, or the refusal text wraps and loses its shape.
- `--pace 1.2` reads well; `--pace 2` if you plan to narrate over it.
- Windows Terminal, iTerm2 and most modern terminals render the box drawing and colour
  correctly. If yours does not, the script still runs, it just looks worse.

## Capturing

OBS with two sources is the straightforward route. For a terminal-only capture, `asciinema`
gives a crisp, small, text-selectable recording:

```bash
asciinema rec showcase.cast -c "python examples/showcase.py --pace 1.2"
```

To cut a plain screen capture down afterwards:

```bash
ffmpeg -i raw.mkv -vf "crop=1920:1080:0:0,fps=30" -c:v libx264 -crf 20 showcase.mp4
```

## Do not

- Do not re-record only the successful beats and stitch them together. The script checks
  the whole sequence; a cut recording is no longer evidence of anything.
- Do not narrate a claim the run did not make. If the audit section says the chain is
  intact, say that; do not say the log is signed, because it is not.
- Do not film real hardware without a physical interlock in place, whatever the tag says.
  See [`standards-map.md`](standards-map.md).
