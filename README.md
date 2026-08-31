<div align="center">

# Open-MHS

**Open Model Hardware Standard — the open-source safety middleware for AI-driven physical hardware.**

[![Tests](https://github.com/Abenor-Labs/Open-MHS/actions/workflows/test.yml/badge.svg)](https://github.com/Abenor-Labs/Open-MHS/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-compatible-8A63D2.svg)](https://modelcontextprotocol.io)
[![Tests passing](https://img.shields.io/badge/tests-180%20passing-brightgreen.svg)](#testing)

*An agent asks for 300°. The arm is bounded to 90°. Nothing moves.*

**180 tests · 2 independent enforcement points · 5 typed error codes · MCP-native · real
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
┌──────────────────┐
│  Claude Desktop  │   "Move the arm to 300 degrees"
│  or any MCP      │
│  client          │
└────────┬─────────┘
         │  MCP (stdio)
         ▼
┌──────────────────┐   4 tools: discover / read / write / emergency_stop
│   MCP Adapter    │   Turns refusals into text a model can act on
│   mcp_adapter/   │
└────────┬─────────┘
         │  JSON-RPC 2.0 over HTTP  ·  Bearer token
         ▼
┌──────────────────┐   ① Ingestion — capability tags validated at registration
│  Open-MHS Server │   ② Runtime   — every write checked against the registry
│      server/     │   ✋ BLOCKED — nothing is dispatched
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
| **Execution Primitives**       | JSON-RPC 2.0 at`POST /rpc` — `mhs.read`, `mhs.write`, `mhs.discover`, `mhs.emergency_stop`.     |
| **MCP Adapter**                | Exposes all of the above to Claude Desktop, Claude Code, or any MCP client.                                |

The agent's entire vocabulary is two primitives — `read` observes, `write` commands — plus
an emergency stop. One mutating surface means one place to audit.

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
- [x] Two independent enforcement points (middleware before dispatch, driver before transmit)
- [x] Inclusive bounds and `max_rate` rate limiting
- [x] All three `on_violation` policies honoured: `reject`, `clamp`, `estop`
- [x] Closed-loop verification against a feedback sensor, reporting `-32003` on desync
- [x] Per-actuator human confirmation gates
- [x] API-key auth that fails safe, Bearer and `x-api-key`, multi-token rotation

**Integration**

- [x] MCP adapter — four tools, refusals rendered as text a model can act on
- [x] Real serial transport driving Marlin/GRBL-style G-code
- [x] In-memory transport with fault injection (dead link, stuck axis)
- [x] PyBullet demo exporting a narrated MP4

**Quality**

- [x] 180 tests, no hardware required
- [x] CI on Python 3.11 and 3.12, across Ubuntu and Windows, plus `ruff`

### Roadmap

Not built. Listed in rough order of how much they matter — contributions welcome on any of
them, and the first two are the ones that would change what this project can honestly claim.

- [ ] **Signed capability tags.** Today a tag is authenticated but not *attested*: anyone
      holding the API token can register a device declaring whatever limits it likes. This
      is the deepest hole in the trust model.
- [ ] **Per-device credentials.** One shared secret means a compromised sensor's token can
      command a robotic arm.
- [ ] **Enforce `max_duration_s`.** The schema defines it; the middleware parses it and
      ignores it. An unenforced field in a safety specification is worse than an absent one.
- [ ] **Persistent registry and an audit log.** The registry is in-memory: a restart forgets
      every device, and nothing records what was commanded or refused.
- [ ] **More drivers** — Modbus TCP, CAN bus, ROS 2, Dynamixel, SCPI instruments, GPIO/I²C.
- [ ] **Real-hardware validation.** The serial driver is tested against a fake port and
      pyserial's loopback. Nobody has driven physical metal with it yet, and that report is
      one of the most valuable things a contributor could file.
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
tests/        180 tests, no hardware required
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
pytest                # 180 tests, no hardware required
ruff check .
```

No mocks of the thing under test. The suite substitutes the **transport** and nothing
above it, so the real routers, the real driver classes and the real safety evaluation all
execute on every run:

```text
test → real /rpc route → real driver class → FAKE transport
                                             ^^^^ only this is fake
```

What the 180 tests actually cover:

| Area | What is proved |
| --- | --- |
| Bounds | Inclusive at both ends; a value one ulp outside is refused with zero bytes sent |
| Independence | An unsafe driver that enforces nothing still cannot be handed a bad command |
| Rate limits | A step change that is in-range at both ends but too fast is refused |
| Policies | `reject`, `clamp` and `estop` each behave as the tag declares |
| Desync | A transport that accepts a command without moving reports `-32003` |
| Auth | Every hardware-facing route is walked and asserted to 401 without a token |
| Schema | Every shipped capability tag validates against both validators, and malformed ones are rejected by both |

The suite runs in about two seconds and needs no hardware, so there is no excuse for a
driver to arrive without tests.

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
