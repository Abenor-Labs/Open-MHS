<div align="center">

# Open-MHS

**Open Model Hardware Standard — the open-source safety middleware for AI-driven physical hardware.**

[![Tests](https://github.com/Abenor-Labs/Open-MHS/actions/workflows/test.yml/badge.svg)](https://github.com/Abenor-Labs/Open-MHS/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-compatible-8A63D2.svg)](https://modelcontextprotocol.io)
[![Tests passing](https://img.shields.io/badge/tests-207%20passing-brightgreen.svg)](#testing)

*An agent asks for 300°. The arm is bounded to 90°. Nothing moves.*

**207 tests · 2 independent enforcement points · 5 typed error codes · MCP-native · real
serial hardware driver · CI across Python 3.11/3.12 on Linux and Windows**

</div>

---

## Contents

- [The Problem](#the-problem)
- [The Solution](#the-solution)
- [Architecture](#architecture)
- [Quickstart](#quickstart)
- [The Cinematic Sandbox Demo](#the-cinematic-sandbox-demo)
- [Status — what works today, what's next](#status--what-works-today-whats-next)
  - [Manipulation results](#manipulation-results)
  - [Versioning](#versioning)
  - [Roadmap](#roadmap)
- [Reference](#reference)
- [Testing](#testing)
- [Contributing](#contributing)
- [License](#license)

---

## The Problem

**LLMs hallucinate.** That is a property of the technology, not a bug awaiting a patch.

| If an AI hallucinates…            | You get…                             |
| ---------------------------------- | ------------------------------------- |
| A database query                   | An error and a retry                  |
| A file path                        | A stack trace                         |
| **A robotic arm trajectory** | **A destroyed $80,000 machine** |

The blast radius of a token changes completely once that token becomes torque. And the
usual mitigations do not survive contact with hardware:

- **Prompting** — "never exceed 90 degrees" lives in a context window that gets truncated,
  overridden, or ignored under distribution shift.
- **Tool schemas** — a JSON Schema `maximum` is a hint the model is trained to respect, not
  an interlock that stops a command.
- **Code review** — the limit ends up hardcoded in one integration, missing from the next,
  and stale in the third.

So the safety envelope keeps living in a human's head or a PDF, while the thing issuing
commands is a probabilistic system that never read the PDF.

Anthropic's Model Hardware Standard (MHS) targets this problem but is currently a closed
research preview. **Open-MHS is the open, vendor-neutral alternative** — schema, middleware
and reference drivers, all public and implementable by anyone.

### Relationship to Anthropic's Model Hardware Standard

Anthropic announced the [Model Hardware Standard](https://www.anthropic.com/news/model-hardware-standard-research-preview)
on 27 August 2026 as a closed, apply-only research preview co-developed with HHMI Janelia.
As of this release no specification, schema, SDK, or repository has been published.
Open-MHS is an independent, Apache-2.0 implementation of the same idea — a device declares
what it measures, what it adjusts, and what it refuses, and an agent reaches it through
`read`, `write`, and discovery over MCP, a CLI, or HTTP — built in the open with a published
schema, a test suite, and an audit trail. It is not affiliated with or endorsed by
Anthropic. When their specification is published, this project will document a mapping
and, where the two can be reconciled without loosening a single safety bound, an adapter.

## The Solution

**Open-MHS is an intercepting bouncer between the model and the machine.**

Hardware declares its own limits in a standard `.mhs` capability tag. The server intercepts
every tool call, evaluates it mathematically against those limits, and blocks anything
outside the envelope **before a single byte reaches the device** — then returns an
actionable JSON-RPC error so the LLM can self-correct.

#### 1. The limit travels with the hardware

```json
{
  "target": "joint_1",
  "unit": "deg",
  "min": -90.0,
  "max": 90.0,
  "max_rate": 30.0,
  "enforcement": "software",
  "on_violation": "reject",
  "rationale": "Beyond +/-90 deg the arm collides with the bench mount."
}
```

Not in the prompt. Not in the model. In the device's own descriptor, where every agent that
discovers it reads the same numbers.

#### 2. Refusals teach

Most systems return `400 Bad Request`, so the model retries the same command. Open-MHS
returns the boundary *and* the reasoning:

```text
REJECTED - safety limit violation (code -32001). Nothing was transmitted to the hardware.
You commanded joint_1 = 300.0 deg.
The allowed range is -90.0 to 90.0 deg, inclusive.
Retry with a value between -90.0 and 90.0.
Why this limit exists: Beyond +/-90 deg the arm collides with the bench mount.
Do not attempt to work around this limit.
```

#### 3. Enforcement does not trust the driver

The check runs **twice** — once in the middleware before the driver is called, once in the
driver before the transport is touched. A third-party driver that enforces nothing still
cannot be handed an out-of-bounds command. There is a test that fails if you delete either
check.

### Built like safety software, not like a demo

The claims above are only worth anything if they are enforced under adversarial conditions.
So they are tested that way:

- **Every rejection test asserts zero bytes were transmitted**, not merely that an error
  came back. A refused write that still reached the wire would pass a return-value
  assertion and fail the machine.
- **The two enforcement points are proven independent.** A test drives the middleware with
  a deliberately unsafe driver that enforces nothing at all. The command is still refused.
- **The test suite is mutation-tested.** The safety code was deliberately broken three ways
  — inclusive bounds made exclusive, the driver check deleted, the middleware check deleted
  — and the suite was required to catch each one. The third mutation initially survived,
  which exposed a genuine coverage gap that is now closed.
- **Ingestion enforces seven rules JSON Schema structurally cannot express**, including
  "every actuator must be bounded" and "an actuator's unit must match its limit's unit".
  A tag declaring degrees against a limit in radians is rejected, not converted.
- **The demo refuses to produce a misleading recording.** It exits non-zero if the unsafe
  command is *not* rejected, or if the arm drifts more than 1° while blocked.

## Architecture

```text
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│  Claude Desktop  │   │   open-mhs CLI   │   │  any HTTP client │
│  or any MCP      │   │   (a human, or   │   │  (a script the   │
│  client          │   │   an agent shell)│   │   agent wrote)   │
└────────┬─────────┘   └────────┬─────────┘   └────────┬─────────┘
         │  MCP (stdio)         │                      │
         ▼                      │                      │
┌──────────────────┐            │                      │
│   MCP Adapter    │  7 tools   │                      │
│   mcp_adapter/   │            │                      │
└────────┬─────────┘            │                      │
         │  JSON-RPC 2.0 over HTTP  ·  Bearer token    │
         ▼                      ▼                      ▼
┌──────────────────┐   ① Ingestion — capability tags validated at registration
│  Open-MHS Server │   ② Runtime   — every write checked against the registry
│      server/     │   ✋ BLOCKED — nothing is dispatched, refusal is audited
│                  │   ⏱ Watchdog — max_duration_s returns a held actuator to default
│                  │   📜 Audit    — every command and refusal, hash-chained
└────────┬─────────┘
         │  only if the command is inside the envelope
         ▼
┌──────────────────┐   ③ The driver re-checks the same limits independently
│  Hardware Driver │   Translates to bytes: G-code, serial, Modbus, GPIO
│     drivers/     │
└────────┬─────────┘
         ▼
   Physical hardware
```

| Layer                                | Responsibility                                                                                             |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| **Capability Tags** (`.mhs`) | JSON Schema (draft 2020-12) in which a device declares its sensors, actuators and hardcoded safety limits. |
| **Discovery Layer**            | HTTP registry. Hardware announces itself; agents ask what is present and get the full tag inline.          |
| **Execution Primitives**       | JSON-RPC 2.0 at `POST /rpc` — `mhs.read`, `mhs.write`, `mhs.discover`, `mhs.emergency_stop`, and for a whole cell `mhs.snapshot`, `mhs.check`, `mhs.emergency_stop_all`. |
| **MCP Adapter**                | Exposes all of the above to Claude Desktop, Claude Code, or any MCP client.                                |
| **CLI**                        | `open-mhs` — the same primitives from a shell, with the same refusal text.                                |
| **Audit log**                  | Hash-chained JSONL of every command and refusal. [`docs/audit-log.md`](docs/audit-log.md).                |

The agent's entire vocabulary is two primitives — `read` observes, `write` commands — plus
an emergency stop. One mutating surface means one place to audit.

### Three gates, one enforcer

An agent reaches hardware three ways, and all of them land on the same `/rpc` dispatcher
and the same two enforcement points. There is no gate with a looser envelope.

| Gate | Command | Who uses it |
| --- | --- | --- |
| **MCP** | `open-mhs-mcp` | Claude Desktop, Claude Code, any MCP client |
| **Shell** | `open-mhs read/write/snapshot/check/estop` | a person at a terminal, or an agent running in one |
| **Code** | `open-mhs export <tag>.mhs --out arm01.py` | a controller that runs with no model in the loop |

The code gate is the handover point that matters. A model is useful while the search space
is unknown and a liability once it is not: let the agent explore, then export a module and
let plain Python run the result forever. The generated module enforces nothing — it carries
the bounds in `BOUNDS` and in its docstrings so a controller can *plan* inside them, while
every write still goes to the middleware and is refused exactly as an MCP call would be.

```bash
open-mhs export examples/bench_pump.mhs --out pump_01.py
open-mhs doc    examples/bench_pump.mhs --out DEVICE.md   # the reference a model reads
python examples/exported_controller.py                    # sweep, fit, close the loop, probe
```

`examples/exported_controller.py` does that end to end against the reference pump: it reads
`max_rate` out of `BOUNDS` and paces itself (a value inside the range is still refused if it
arrives too fast), sweeps the envelope, fits a gain, hits a target to 0.0000, and has its
out-of-bound probe refused with nothing transmitted. CI runs it.

`open-mhs doc` writes the per-device Markdown an agent reads instead of a vendor PDF. Every
number in it comes from the tag; a test fails if one does not.

### Operating a cell, not a device

Real work spans several instruments. Three methods make that safe without giving up the
two-primitive vocabulary:

| Method | What it does | Transmits? |
| --- | --- | --- |
| `mhs.snapshot` | Every channel of every device in one call; a dead sensor is reported inline, not fatal | never |
| `mhs.check` | Dry-run a list of writes across any devices against the *current* envelopes; returns a per-item verdict with the real bound on each refusal | never, and runs no e-stop |
| `mhs.emergency_stop_all` | Stop everything that declares an e-stop; a failure on one device never halts the loop | safe states only |

`examples/cell_agent.py` uses them with no device-specific knowledge: snapshot, build a
plan from the tags, check it, execute only what passed, snapshot again, stop all. CI runs
it end to end against the three shipped mock devices.

```bash
open-mhs snapshot
open-mhs check plan.json      # exit 1 if any item would be refused; nothing moved
open-mhs estop --all
```

## Quickstart

### 1. Install

```bash
git clone https://github.com/Abenor-Labs/Open-MHS.git
cd Open-MHS
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Start the server

Open-MHS actuates physical hardware, so it **refuses to start without an auth token**.
There is no flag to disable this — a server that comes up unauthenticated because a
variable was forgotten is exactly the failure this project exists to prevent.

```bash
export OPEN_MHS_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn server.main:app
```

<details>
<summary>Windows PowerShell</summary>

```powershell
$env:OPEN_MHS_AUTH_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
uvicorn server.main:app
```

</details>

Two mock devices register themselves on startup, so there is something to talk to
immediately:

```bash
curl -H "Authorization: Bearer $OPEN_MHS_AUTH_TOKEN" localhost:8000/discover
```

Interactive API docs at `http://localhost:8000/docs`.

### 3. Connect it to Claude Desktop

```bash
pip install -e .
```

Add the adapter to `claude_desktop_config.json`:

- **macOS** — `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows** — `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "open-mhs": {
      "command": "/absolute/path/to/.venv/bin/open-mhs-mcp",
      "env": {
        "OPEN_MHS_URL": "http://127.0.0.1:8000",
        "OPEN_MHS_AUTH_TOKEN": "the-same-token-the-server-was-started-with"
      }
    }
  }
}
```

> **Note** — use an absolute path; Claude Desktop does not inherit your shell's `PATH`. On
> Windows, escape the backslashes:
> `"C:\\path\\to\\.venv\\Scripts\\open-mhs-mcp.exe"`.

Restart Claude Desktop. Four tools appear under the connector menu.

### 4. Talk to your hardware

> **You:** What hardware is connected?
>
> **Claude:** Two devices are registered. `arm-01` is a 2-axis bench arm — `joint_1` accepts
> −90 to 90 degrees at up to 30 deg/s, `joint_2` accepts −135 to 135, and the gripper
> requires human confirmation before it will actuate. `mock-temp-01` is a read-only
> environmental sensor.
>
> **You:** Move the arm to 45 degrees.
>
> **Claude:** ACCEPTED. `arm-01.joint_1` commanded to 45.0 deg, verified against its
> feedback sensor `joint_1_actual`.
>
> **You:** Now take it to 300.
>
> **Claude:** That was refused. The allowed range is −90 to 90 degrees and nothing was
> transmitted to the hardware — past ±90 the arm collides with the bench mount. I can take
> it to 90, which is as far as that axis is permitted to go.

The model corrected itself straight from the error object. No prompt engineering, no retry
loop, no bytes on the wire.

## The Cinematic Sandbox Demo

[`examples/pybullet_demo/`](examples/pybullet_demo/) runs the A/B test in a simulated lab:
**the same agent, the same arm, one command inside the envelope and one outside.** It
exports a 60-second MP4 with the narration burned in — no screen recorder required.

```bash
pip install pybullet opencv-python
python examples/pybullet_demo/auto_demo.py
```

A KUKA iiwa moves cleanly to 45°, then the agent hallucinates a 300° rotation. The frame
flips to alarm, the JSON-RPC refusal fills the terminal, and **the arm does not move** —
while physics keeps stepping, so you can see the difference between "refused" and "frozen".
The agent then reads the bound out of the error and returns to 0°.

**The refusal in that video is not staged.** The demo imports `server.safety` and calls the
same `check_write()` that guards real hardware, against a real capability tag; what you see
is `SafetyLimitViolation.to_rpc()` rendered verbatim. The script exits non-zero if the
command is *not* rejected, or if the arm drifts more than 1° during the block — it will not
produce a misleading recording.

> One detail worth the pause: the KUKA's own URDF permits ±170°. The tag declares ±90. That
> gap is the entire argument — the firmware would happily accept 150°, and only the
> declared envelope stops it.

## Status — what works today, what's next

### What works today

**Specification**

- [x] Capability Tag schema (JSON Schema draft 2020-12) — sensors, actuators, safety limits,
      units, rates, enforcement level, violation policy
- [x] Ingestion validation catches what JSON Schema structurally cannot: every actuator is
      bounded, units agree between actuator and limit, ids are unique across the union of
      sensors and actuators, defaults sit inside their own limits, references resolve

**Middleware**

- [x] Discovery registry — register, discover, heartbeat, deregister
- [x] JSON-RPC 2.0 — `mhs.read`, `mhs.write`, `mhs.discover`, `mhs.emergency_stop`, plus
      batches and notifications
- [x] **Multi-device** — `mhs.snapshot`, `mhs.check` (dry-run a cross-device plan; nothing
      transmitted, no e-stop run), `mhs.emergency_stop_all` (never halts on one failure)
- [x] **`max_duration_s` enforced** — a dead-man watchdog returns a held actuator to its
      default, or runs the e-stop if that is refused
- [x] **Audit log** — every command and refusal as a hash-chained JSONL line;
      `open-mhs audit verify` finds the first broken link
- [x] Two independent enforcement points (middleware before dispatch, driver before transmit)
- [x] Inclusive bounds and `max_rate` rate limiting
- [x] All three `on_violation` policies honoured: `reject`, `clamp`, `estop`
- [x] **Conditional envelopes** — a bound that changes with device state. The arm's floor
      rises from 0.83 m to 0.86 m while the gripper holds a block, because the payload
      hangs below the tool. Conditions may only ever *narrow* the base bound, enforced at
      ingestion, so the declared envelope is always the worst case
- [x] Clamped writes carry a `warning` field naming the requested value, the transmitted
      value, and which condition tightened the bound
- [x] Closed-loop verification against a feedback sensor, reporting `-32003` on desync
- [x] Per-actuator human confirmation gates
- [x] API-key auth that fails safe, Bearer and `x-api-key`, multi-token rotation

**Integration**

- [x] MCP adapter — seven tools, refusals rendered as text a model can act on
- [x] `open-mhs` CLI — discover, read, write, snapshot, check, estop, describe, export,
      doc, audit verify, serve; identical refusal text to the MCP tools
- [x] **Code-file gate** — `open-mhs export` generates a typed, dependency-free Python
      module from a tag, so a controller can run with no model in the loop and still be
      refused by the middleware on every write
- [x] **Reference documents** — `open-mhs doc` generates the per-device Markdown an agent
      reads instead of a manual, entirely from the tag
- [x] Three reference devices loaded by default: arm, temperature sensor, pump
- [x] Real serial transport driving Marlin/GRBL-style G-code
- [x] In-memory transport with fault injection (dead link, stuck axis)
- [x] PyBullet demo exporting a narrated MP4
- [x] **robosuite/MuJoCo digital twin** — a Franka Panda and a colour-segmentation camera
      as two separate devices, four blocks, wrist yaw under closed-loop control, and a
      live wrist-camera inset overlaid on the viewer (`run_cell.py --viewer --pov`)
- [x] **Ed25519 command signing** — [`docs/rt-signing.md`](docs/rt-signing.md) specifies a
      200-byte signed frame for a real-time management plane; `tests/test_crypto_bridge.py`
      is a working Python reference with 16 tests. Spec and prototype only: no C++ exists

**Quality**

- [x] 264 tests, no hardware required, including the multi-device example run end to end
- [x] CI on Python 3.10, 3.11 and 3.12, across Ubuntu and Windows, plus `ruff`
- [x] Driver compliance smoke test — five checks in 0.1 s against a real driver and a real
      tag, covering reads, in-bounds writes, refusals, clamping and conditional bounds

### Manipulation results

`examples/robosuite_demo/stack_blocks.py` builds a block tower through the middleware, and
is the closest thing here to an end-to-end honesty test: an agent planning a physical task
with nothing but `mhs.read`, `mhs.write` and a capability tag.

| Result | Status |
| --- | --- |
| Three-tier tower | **Reproducible.** Verified by tier height *and* occlusion signature |
| Four-tier tower | **Achieved once, not yet re-verified.** A verification bug of ours then dismantled it — see below |
| Grasp quality | 29.6–35.6 mm carry offset against a 30 mm geometric ideal, first attempt |
| Placement drift | ~8 mm across a whole tower, down from ~14 mm *per tier* |

Two findings are worth more than the tower:

**Vision belongs in verification, never in the command path.** Targeting each placement at
the camera's estimate of the block below made the camera's bias compound once per tier —
~14 mm each, 40 mm by the third, past the 21 mm half-width that keeps a tower standing.
Commanding in the tool frame instead, and using the camera only to answer *did that work*,
removed the drift entirely.

**Most of our failures were bad acceptance criteria, not bad mechanics.** Four separate
"failures" turned out to be checks stricter than the physics they were checking: an
occlusion threshold tuned on a three-tier tower that called a correct four-tier one broken;
a convergence tolerance below what the servo delivers under load; a demand that a *release*
descent converge, when landing short is exactly what setting a block down means. Because
each was wired to a retry that undoes work, they did not merely report wrongly — one of
them dismantled a finished tower. **A verification step that cannot distinguish success
from failure is not neutral.**

### Versioning

Two version numbers, and they mean different things.

| | What it versions | Where |
| --- | --- | --- |
| `mhs_version` | the **Capability Tag format** — a wire contract between anyone who writes a tag and anyone who reads one | inside every `.mhs` file |
| package version | this **implementation** | `pyproject.toml` |

They are allowed to diverge, and will. A tag written for spec 0.2 must be readable by any
0.2 implementation, whoever wrote it.

**Any added field bumps the spec version, even a purely additive one.** Tags validate
strictly — `additionalProperties: false`, `extra="forbid"` — so a reader built against an
older spec does not ignore an unknown field, it *rejects the tag*. Shipping a new field
under the old version number silently breaks every existing implementation. Ingestion
enforces this: a tag declaring `0.1` while using a `0.2` feature is refused, and says so.

| Spec | Adds |
| --- | --- |
| **0.1** | the original format |
| **0.2** | `safety_limits[].conditions` — bounds that resolve against live device state |

### Roadmap

Grouped by the claim each milestone would let the project honestly make. Contributions
welcome on any of it; the v0.2 items are the ones that most change what can be said today.

#### v0.2 — trust the readings

The middleware enforces bounds correctly. What it cannot do is tell whether the *reading*
a bound was evaluated against described reality. Every guarantee sits downstream of that.

- [ ] **Sensor confidence in the tag.** `vision` vs `ground_truth` is honest about total
      occlusion and silent about partial: a blob at a quarter of its clean pixel count
      still calls itself `vision` while being 28 mm wrong. A device should be able to
      report *how much* to trust a reading, and the middleware should be able to refuse to
      act on a degraded one.
- [x] **Enforce `max_duration_s`.** *Shipped in 0.2.0: a dead-man timer returns the
      actuator to its default, or runs the emergency stop if that is refused. Every expiry
      is audited.*
- [x] **Conditional envelopes.** Bounds that change with device state. *Shipped in spec 0.2.*
- [x] **Multi-device orchestration.** Snapshot, dry-run plan check, fleet stop. *Shipped
      in 0.2.0.*

#### v0.3 — trust the sender

- [ ] **Signed capability tags.** A tag is authenticated but not *attested*: anyone holding
      the API token can register a device declaring whatever limits it likes.
      [`docs/rt-signing.md`](docs/rt-signing.md) specifies the wire format and
      `tests/test_crypto_bridge.py` implements signer and verifier — but nothing in the
      registry requires a signature yet.
- [ ] **Per-device credentials.** One shared secret means a compromised sensor's token can
      command a robotic arm.
- [x] **Audit log.** *Shipped in 0.2.0.* Hash-chained, not yet signed.
- [ ] **Persistent registry.** The registry is in-memory: a restart forgets every device.
      Deliberate for now — a registry that survives a restart can hand an agent a tag for
      hardware that is no longer plugged in — but a "stale until re-announced" state would
      let both be true.

#### v0.4 — trust it off this desk

- [ ] **Real-hardware validation.** Everything here is simulated. The serial driver is
      tested against a fake port and pyserial's loopback; nobody has driven physical metal
      with it. That report is the single most valuable thing a contributor could file.
- [ ] **More drivers** — Modbus TCP, CAN bus, ROS 2, Dynamixel, SCPI instruments, GPIO/I²C.
- [ ] **Orientation of a held object.** The camera gives a blob centroid; nothing knows
      whether a grasped part hangs square. Tilt is the last unmeasured variable in
      placement, and the likeliest reason a fourth tier is harder than a third.

#### v1.0 — freeze the spec

- [ ] **Capability Tag 1.0**, with a compatibility policy and a conformance suite an
      independent implementation can run against itself.
- [ ] **Deployment hardening** — TLS termination guidance, rate limiting on the HTTP surface.
- [ ] **PyPI release**, so `pip install open-mhs` works without a clone.

## Reference

```text
schema/       capability_schema.json — the specification
server/       FastAPI middleware: registry, JSON-RPC dispatcher, safety, auth
drivers/      driver contract, in-memory transport, real serial (G-code) transport
mcp_adapter/  MCP server wrapping the HTTP surface
examples/     worked capability tags and the PyBullet demo
docs/         specification documentation
tests/        207 tests, no hardware required
```

**Error codes**

| Code       | Meaning                                                       |
| ---------- | ------------------------------------------------------------- |
| `-32000` | Device not found                                              |
| `-32001` | Safety limit violation                                        |
| `-32002` | Hardware execution error                                      |
| `-32003` | State desync                                                  |
| `-32602` | Invalid params (includes a write aimed at a read-only sensor) |

`-32001` carries the attempted value and the violated bound, so a corrective retry needs no
extra round trip. `-32003` carries both the commanded and the observed value, because a
desync means the agent's model of the world has diverged from the world.

Full specification: [`docs/capability-tags.md`](docs/capability-tags.md).

> [!WARNING]
> **Status: alpha (v0.1).** The Capability Tag schema is the stable surface. Do not connect
> this to hardware that can injure a person or damage an experiment without an independent
> hardware interlock. Software is a layer of defence, never the only one.

## Testing

```bash
pytest                                    # 264 tests, no hardware required
pytest tests/test_driver_compliance.py    # 5-check smoke test, 0.1 s
python tests/test_crypto_bridge.py        # signing flow, narrated
ruff check .
```

No mocks of the thing under test. The suite substitutes the **transport** and nothing
above it, so the real routers, the real driver classes and the real safety evaluation all
execute on every run:

```text
test → real /rpc route → real driver class → FAKE transport
                                             ^^^^ only this is fake
```

What the 264 tests actually cover:

| Area | What is proved |
| --- | --- |
| Bounds | Inclusive at both ends; a value one ulp outside is refused with zero bytes sent |
| Independence | An unsafe driver that enforces nothing still cannot be handed a bad command |
| Rate limits | A step change that is in-range at both ends but too fast is refused |
| Policies | `reject`, `clamp` and `estop` each behave as the tag declares |
| Desync | A transport that accepts a command without moving reports `-32003` |
| Auth | Every hardware-facing route is walked and asserted to 401 without a token |
| Schema | Every shipped capability tag validates against both validators, and malformed ones are rejected by both |
| Conditional bounds | A state-dependent floor tightens with the gripper, reads the *sensor* not the commanded value, and may never widen the base bound |
| Signing | Replay, forgery, tampering, a forged approval flag, and a signed-but-unsafe command that the envelope still refuses |
| Audit | A refused write leaves a line with the error and `transmitted: null`; an edited or deleted line breaks the chain at the right line number |
| Watchdog | A held actuator returns to default after `max_duration_s`; a newer write restarts the timer; an e-stop cancels it; a refused return falls back to the e-stop |
| Multi-device | `mhs.check` transmits nothing and runs no e-stop even for an `estop` limit; `mhs.emergency_stop_all` keeps going past a device that fails to stop |
| CLI | Every command exits non-zero on refusal, and a refused write transmits nothing |
| Example | `examples/cell_agent.py` runs end to end against a live uvicorn and the three mocks |

The suite runs in about ten seconds and needs no hardware, so there is no excuse for a
driver to arrive without tests. Three of the safety modules were also mutation-checked
during development: break the check, watch the tests fail, restore it.

## Contributing

**The most valuable thing you can add is a driver.**

Open-MHS ships a real serial/G-code transport and a mock one. Every device class beyond
that is an opportunity:

`Modbus TCP` · `CAN bus` · `ROS 2 bridges` · `Dynamixel servos` · `SCPI lab instruments` ·
`GPIO / I²C sensors` · `3D printers` · `syringe pumps` · `spectrometers`

Writing one means implementing `acquire` and `transmit` from
[`drivers/transport.py`](drivers/transport.py) and letting `BaseDevice` handle the safety
path. Keep protocol knowledge in the device's `encode`/`decode` and the link dumb, and your
driver stays fully testable against a fake port with no hardware attached.

Other high-value work:

- **Signed capability tags** — the deepest gap in the current trust model.
- **Per-device credentials**, so a compromised sensor cannot command an arm.
- **Real-hardware validation reports** — run a driver against the metal and tell us what broke.
- **Schema RFCs** — a change to `capability_schema.json` needs a stated rationale and
  re-validation of every fixture. Never widen a safety bound to make a device validate.

Issues and pull requests:
[github.com/Abenor-Labs/Open-MHS](https://github.com/Abenor-Labs/Open-MHS)

## License

[Apache-2.0](LICENSE)
