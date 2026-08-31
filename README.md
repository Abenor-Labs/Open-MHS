<div align="center">

# Open-MHS

**Open Model Hardware Standard — the open-source safety middleware for AI-driven physical hardware.**

[![Tests](https://github.com/Abenor-Labs/Open-MHS/actions/workflows/test.yml/badge.svg)](https://github.com/Abenor-Labs/Open-MHS/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-compatible-8A63D2.svg)](https://modelcontextprotocol.io)
[![Tests passing](https://img.shields.io/badge/tests-180%20passing-brightgreen.svg)](#testing)

*An agent asks for 300°. The arm is bounded to 90°. Nothing moves.*

</div>

---

## Contents

- [The Problem](#the-problem)
- [The Solution](#the-solution)
- [Architecture](#architecture)
- [Quickstart](#quickstart)
- [The Cinematic Sandbox Demo](#the-cinematic-sandbox-demo)
- [Reference](#reference)
- [Testing](#testing)
- [Contributing](#contributing)
- [Author](#author)
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

**Also built in** — closed-loop verification against feedback sensors, per-actuator human
confirmation gates, rate limiting, `on_violation` policies (`reject` / `clamp` / `estop`),
and API-key auth that fails safe. Full spec: [`docs/capability-tags.md`](docs/capability-tags.md).

**Not built yet, stated plainly** — capability tags are authenticated but unsigned, so a
token holder can publish any limits it likes; there is one shared secret with no per-device
identity; `max_duration_s` is parsed but not enforced. Signed tags are the right fix and
are open for contribution.

> [!WARNING]
> **Status: alpha (v0.1).** The Capability Tag schema is the stable surface. Do not connect
> this to hardware that can injure a person or damage an experiment without an independent
> hardware interlock. Software is a layer of defence, never the only one.

## Testing

```bash
pytest                # 180 tests, no hardware required
ruff check .
```

The suite substitutes the **transport** and nothing above it, so the real routers, the real
driver classes and the real safety evaluation all run:

```text
test → real /rpc route → real driver class → FAKE transport
```

Every rejection test asserts two things: the caller got the right error, **and** the
transport recorded zero transmissions. A rejected write that still emitted bytes is a
safety failure that a return-value assertion would happily pass.

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

## Author

**Mahamad Suhail** — Full Stack Developer and AI enthusiast.

Open-MHS grew out of a straightforward observation: the tooling for letting language models
touch the physical world was being built as though hallucination were a solved problem. The
work here is systems engineering aimed at that gap — schema design, middleware
architecture, driver abstractions, and a test suite built to prove the safety claims rather
than assert them.

Focused on AI systems architecture, full-stack engineering, and the infrastructure layer
that has to exist before agents can safely operate real machines.

## License

[Apache-2.0](LICENSE)
