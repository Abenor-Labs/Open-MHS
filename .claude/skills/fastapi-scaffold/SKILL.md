---
name: fastapi-scaffold
description: Use when adding or modifying async FastAPI routers, Pydantic models, or dependency wiring in the Open-MHS middleware server. Triggers on "add an endpoint", "scaffold a router", "wire up /discover", "expose the execute primitive", "add a JSON-RPC method", or any change under server/.
---

# Open-MHS FastAPI Router Scaffold

Generate async FastAPI routers for the Open-MHS Discovery and Execution layers that match
the repo's existing conventions exactly.

## When to use

- Adding a new endpoint to the middleware server.
- Adding a JSON-RPC method to `/execute`.
- Splitting a growing `open_mhs/server/main.py` into routers.

## Read before writing

Always read these first so the new code matches what exists:

```bash
sed -n '1,80p' server/main.py
ls server/
```

If a router for this concern already exists, extend it. Do not create a parallel one.

## Layout

```
server/
  main.py            # app factory + router registration only, no business logic
  models.py          # Pydantic request/response models, shared
  registry.py        # in-memory device registry (single source of truth)
  safety.py          # limit evaluation - the only place bounds are interpreted
  errors.py          # JSON-RPC error codes + typed exceptions
  routers/
    discovery.py     # GET  /discover, POST /register, DELETE /devices/{id}
    rpc.py           # POST /rpc, POST /execute  (JSON-RPC 2.0 dispatcher)
  deps.py            # FastAPI dependencies (get_registry, etc.)
```

## Conventions

1. **Every handler is `async def`.** No blocking I/O in a handler — driver calls go through
   an awaitable transport, or `anyio.to_thread.run_sync` for genuinely blocking hardware SDKs.
2. **`APIRouter` per concern**, with `prefix` and `tags` set at construction:
   ```python
   router = APIRouter(tags=["discovery"])
   ```
3. **Pydantic v2 models for every request and response.** Never return a bare `dict`.
   Declare `response_model=` on the decorator so the OpenAPI doc is accurate — the OpenAPI
   doc is how an agent discovers the server.
4. **Shared state via `Depends`**, never a module-level global reached into directly:
   ```python
   async def discover(registry: Registry = Depends(get_registry)) -> DiscoverResponse: ...
   ```
5. **Errors are typed.** Raise `HTTPException` for transport-level failures; return a
   JSON-RPC `error` object for method-level failures. Never leak a driver traceback.

## JSON-RPC contract

`POST /rpc` is the canonical JSON-RPC 2.0 surface. `POST /execute` is a compatibility alias
onto the same dispatcher. Four methods, and no others:

| Method | Mutating | Purpose |
|---|---|---|
| `mhs.discover` | no | List registered devices and their capability tags. |
| `mhs.read` | no | Read one sensor or actuator state. |
| `mhs.write` | **yes** | Command one actuator, inside its safety limits. |
| `mhs.emergency_stop` | **yes** | Drive a device to its declared safe state. |

Bare `read` / `write` are accepted as legacy aliases for `mhs.read` / `mhs.write`.

```json
{"jsonrpc": "2.0", "id": 1, "method": "mhs.read",
 "params": {"device_id": "mock-temp-01", "target": "ambient_temp"}}
```

```json
{"jsonrpc": "2.0", "id": 2, "method": "mhs.write",
 "params": {"device_id": "arm-01", "target": "joint_1", "value": 45.0}}
```

Reserved error codes — use these, do not invent new ones in the JSON-RPC reserved range:

| Code | Meaning |
|---|---|
| -32700 | Parse error |
| -32600 | Invalid request |
| -32601 | Method not found (anything outside the four `mhs.*` methods and their aliases) |
| -32602 | Invalid params |
| -32000 | Device not found |
| -32001 | Safety limit violation |
| -32002 | Hardware execution error (driver/transport raised) |
| -32003 | State desync (feedback sensor disagrees with commanded value after settle) |

A `write` aimed at a sensor id is `-32602` (invalid params), not a custom code: the target
exists but is not a legal write target, which is a params problem, not a hardware problem.

`-32001` must include the attempted value and the violated bound in `error.data`, so the
agent can correct itself without a second round trip. `-32003` must include both the
commanded value and the observed feedback value: a desync means the world and the agent's
model of it have diverged, and the agent cannot recover without seeing both.

## Safety rule

The safety check runs in the **server**, before the driver is called, and again in the
**driver**. Two independent enforcement points. Never scaffold a `write` path that reaches
a driver without passing the value through the device's `safety_limits`.

Never add an endpoint that mutates hardware outside the JSON-RPC dispatcher. One mutating
surface means one place to audit. `emergency_stop` is the sole exception to limit checking:
it drives to the tag-declared safe state, which is trusted by definition.

## After scaffolding

```bash
python -m pytest tests/ -q
uvicorn open_mhs.server.main:app --reload   # verify /docs renders
```

Add a test alongside the endpoint in the same change. An endpoint with no test does not ship.
