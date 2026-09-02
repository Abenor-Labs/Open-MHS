# Developing / testing Open-MHS

Getting a working copy, running the demos, and the traps that will otherwise cost you an
afternoon. For the design rules, see [CONTRIBUTING.md](../CONTRIBUTING.md); for the
security model, [SECURITY.md](../SECURITY.md).

## 1. Core install (no simulators)

This is enough to run the middleware and the whole test suite.

```bash
git clone https://github.com/Abenor-Labs/Open-MHS.git
cd Open-MHS
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e . ruff

pytest              # 331 tests, ~20 seconds, no hardware
ruff check .
```

If that passes, the safety layer works on your machine. Everything below is optional
scenery.

## 2. Running the middleware

Open-MHS **refuses to start without an auth token**. That is deliberate — it can actuate
hardware, and a server that comes up unauthenticated because a variable was forgotten is
the failure this project exists to prevent.

```bash
export OPEN_MHS_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn open_mhs.server.main:app
```

```bash
curl localhost:8000/health                                    # public, liveness only
curl -H "Authorization: Bearer $OPEN_MHS_AUTH_TOKEN" localhost:8000/discover
```

Interactive API docs at `http://localhost:8000/docs`.

## 3. The demos

Two live cells. Both serve the *same* middleware — the difference is only what is on the
other end of the driver.

### PyBullet bench cell — no MuJoCo needed

```bash
pip install -r examples/pybullet_demo/requirements-demo.txt
python examples/pybullet_demo/live_lab.py
```

A KUKA iiwa on a bench with three cubes and a tray. Commands are a tool pose in metres; the
safety limits are a work envelope above the bench.

> **PyBullet has no wheel for CPython 3.12 on Windows.** It compiles from an 80 MB source
> tarball and takes roughly 15 minutes. This is the install, not a hang.

### robosuite / MuJoCo digital twin — the flagship

```bash
pip install -r examples/robosuite_demo/requirements-demo.txt
export OPEN_MHS_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python examples/robosuite_demo/run_cell.py --viewer
```

A Franka Panda over a table with a red cube. **Two devices**, deliberately separate:

| Device | Exposes |
| --- | --- |
| `cv-camera-01` | read-only: `cube_x/y/z`, `pose_source`, `detection_pixels` |
| `panda-arm-01` | `tcp_x/y/z`, `gripper_state`, bounded to a measured envelope |

The arm knows nothing about the scene. An agent must ask the camera before it can reach —
perception and actuation are different capability tags, and the safety envelope belongs to
the one that can break something.

**`--viewer` gives you MuJoCo's interactive viewer**: left-drag orbits, right-drag pans,
wheel zooms, double-click tracks a body. (`--render` is robosuite's own window and has a
fixed camera with no mouse input — that catches people out.)

> **Pin `mujoco==3.3.7`.** robosuite 1.5.2 declares `mujoco>=3.3.0`, but MuJoCo 3.4 renamed
> `MjData.qM` to `M`, which robosuite's operational-space controller still calls. A plain
> `pip install robosuite` resolves to the newest MuJoCo and dies on `env.reset()` with
> `AttributeError: 'MjData' object has no attribute 'qM'`. The requirements file pins it.

## 4. Driving a cell

Anything that speaks JSON-RPC works. Read first, then write:

```bash
A="Authorization: Bearer $OPEN_MHS_AUTH_TOKEN"

# where does the camera think the cube is?
curl -s -X POST localhost:8000/rpc -H "$A" -H 'content-type: application/json' -d '{
  "jsonrpc":"2.0","id":1,"method":"mhs.read",
  "params":{"device_id":"cv-camera-01","target":"cube_x"}}'

# move there
curl -s -X POST localhost:8000/rpc -H "$A" -H 'content-type: application/json' -d '{
  "jsonrpc":"2.0","id":2,"method":"mhs.write",
  "params":{"device_id":"panda-arm-01","target":"tcp_x","value":0.02}}'

# refused: the table top is at 0.800
curl -s -X POST localhost:8000/rpc -H "$A" -H 'content-type: application/json' -d '{
  "jsonrpc":"2.0","id":3,"method":"mhs.write",
  "params":{"device_id":"panda-arm-01","target":"tcp_z","value":0.50}}'
```

A working pick sequence for the robosuite cell:

| Step | Command |
| --- | --- |
| 1 | `read cv-camera-01 cube_x`, `cube_y` |
| 2 | `write tcp_z 1.00` (travel height) |
| 3 | `write tcp_x <cube_x>`, `write tcp_y <cube_y>` |
| 4 | `write tcp_z 0.835` — **must be ≤ 0.835**; at 0.845 the gripper closes on air |
| 5 | `write gripper_state "closed"` with `confirm: true` |
| 6 | `read panda-arm-01 grasping` → `true` |
| 7 | `write tcp_z 1.05` |

`gripper_state` carries `requires_confirmation`, so a write without `"confirm": true` is
refused. That is the human-approval gate working, not a bug.

## 5. Using it as MCP tools

The adapter exposes the middleware to any MCP client — the arm shows up as four tools and
the model calls them itself.

`.mcp.json` (project-scoped, for Claude Code) and `claude_desktop_config.json` are already
written on this machine. On a fresh machine:

```json
{
  "mcpServers": {
    "open-mhs": {
      "command": "/absolute/path/to/python",
      "args": ["-m", "open_mhs.mcp_adapter.server"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/Open-MHS",
        "OPEN_MHS_URL": "http://127.0.0.1:8000",
        "OPEN_MHS_AUTH_TOKEN": "the-token-the-server-was-started-with"
      }
    }
  }
}
```

Claude Desktop config lives at `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS). Restart the app
after editing. Claude Code picks up `.mcp.json` on the next session — not the current one,
since MCP tools load at startup.

> The token sits in plaintext in these files. Fine for a localhost demo token; do not put a
> real deployment secret in a file you might commit.

## 6. Troubleshooting

| Symptom | Cause |
| --- | --- |
| `AttributeError: 'MjData' object has no attribute 'qM'` | MuJoCo too new. `pip install mujoco==3.3.7` |
| `TypeError: 'function' object is not subscriptable` starting the MCP server | `anyio` 3.x. `pip install -U "anyio>=4.5"` |
| Middleware exits immediately | `OPEN_MHS_AUTH_TOKEN` unset or shorter than 16 chars. Deliberate |
| Vision returns `pose_source: ground_truth`, `detection_pixels: 1` | The sim is being stepped from the wrong thread. The GL context belongs to whichever thread built it — sim on main, uvicorn on a worker |
| Commands appear to work but the scene is impossible (cube already grasped on a fresh start) | A stale process still owns port 8000. `netstat -ano \| grep :8000`, then `taskkill //PID <pid> //F` |
| MuJoCo window opens but the camera will not move | You used `--render`. Use `--viewer` |
| Arm folds through the table (PyBullet) | IK without a pinned tool orientation. Already fixed in `live_lab.py`; do not remove `targetOrientation` |
| `pip install pybullet` seems hung | It is compiling from source. ~15 min on Windows |
| MCP tools missing after editing config | Claude Desktop needs a restart; Claude Code needs a new session |

## 7. Before you send a PR

```bash
pytest && ruff check .
```

- Tests are not optional, and a change to safety behaviour needs a test that fails without it.
- Say explicitly what you did **not** verify — especially if you could not test against real
  hardware. An honest gap is fine; an unstated one is not.
- If a suite passes first try on a change to the safety path, mutate the code and confirm
  the tests catch it. That practice found a real coverage hole in the middleware check.
