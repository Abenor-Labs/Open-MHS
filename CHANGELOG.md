# Changelog

## 0.2.0 — 2026-09-02

Capability Tag spec: **0.2** (introduced in the previous commit series; `conditions`).
Package: **0.2.0**. First public release.

### Added
- **Audit log.** Hash-chained JSONL of every command and refusal
  (`docs/audit-log.md`, `open-mhs audit verify`).
- **`max_duration_s` is enforced.** A dead-man watchdog at the middleware returns the
  actuator to its default when the timer expires, or runs the emergency stop if that
  return is refused. Every expiry is audited.
- **Multi-device methods.** `mhs.snapshot` (every channel of every device),
  `mhs.check` (dry-run a plan across devices; nothing is transmitted, no e-stop runs),
  `mhs.emergency_stop_all` (stop everything that can stop; never halts on one failure).
- **MCP tools** `snapshot_hardware`, `check_hardware_plan`, `emergency_stop_all_hardware`.
- **`open-mhs` CLI**: discover, read, write, snapshot, check, estop, describe, audit
  verify, serve. Same refusal text as the MCP tools.
- **Reference pump** (`examples/bench_pump.mhs`, `drivers/mock_pump.py`), loaded by
  default alongside the arm and the temperature sensor.
- **`examples/cell_agent.py`**: a device-agnostic multi-device workflow
  (snapshot → plan → check → execute → verify → stop all), run end-to-end in CI.

### Fixed
- `/health` reported spec 0.1 after 0.2 shipped. It now reports the latest version and
  the list of versions this reader accepts.
- `schema/capability_schema.json` is included in the wheel.
- Python 3.10 is tested in CI, matching `requires-python`.
- Docs said 184 tests; the count is in `docs/DEVELOPING.md` and kept current.

### Not in this release
- **No real-hardware validation.** Everything is simulated. The serial driver is tested
  against pyserial's loopback only. A validation report against physical metal is the
  most valuable contribution this project can receive.
- **Registry is still in-memory.** The audit log persists; the device list does not.
- **Watchdog is middleware-only.** A driver used without the middleware runs no timer.

## 0.1.0 — 2026-08-31

Initial commit: Capability Tag schema 0.1, FastAPI middleware with two enforcement points,
MCP adapter, mock and serial drivers, PyBullet and robosuite digital twins.
