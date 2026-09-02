# Changelog

## 0.3.0 — 2026-09-02

**Breaking.** Everything moved under one package and the library surface is now declared.
Done before the first upload precisely so nobody has to be broken by it later.

### Changed
- **Namespace.** `server`, `drivers`, `mcp_adapter` and `cli` were top-level modules, so
  `pip install open-mhs` put four common names into the global namespace and `import
  server` from an installed package collided with the user's own code. They now live at
  `open_mhs.server`, `open_mhs.drivers`, `open_mhs.mcp_adapter`, `open_mhs.cli`. No
  compatibility shims: the package had never been published, so nothing depends on the old
  paths. Console scripts, the MCP server module path, and every tag's `driver.module` field
  were updated to match.
- Package version and Capability Tag spec version now differ (0.3.0 against spec 0.2),
  which is the policy working as intended rather than a mistake.

### Added
- **Governance, written down.** `GOVERNANCE.md` states the actual position — one
  maintainer, bus factor of one, no committee — and defines the one process that makes
  this a standard rather than one person's library: a change to the Capability Tag schema
  goes through an RFC in `docs/rfcs/`, and rejected RFCs stay in the tree so the next
  person can see what was already considered. Also defines what "Open-MHS compliant"
  means: passes the published conformance suite, which nothing can claim until that suite
  ships. Adds `CODEOWNERS`, Contributor Covenant 2.1, and an explicit contribution
  licensing statement.
- **A declared public API.** `import open_mhs` exposes the specification types, the driver
  base class and transports, the safety evaluator, the middleware factory, the audit log,
  and the error classes. `tests/test_public_api.py` pins the exact set: adding a name is a
  feature, removing one is a breaking change.
- **The library path is documented and tested.** A driver plus a transport is a complete
  safety layer with no HTTP, no registry, and no MCP. Tests exercise it directly, including
  that a refusal transmits nothing.
- `py.typed`, so the annotations already in the source reach downstream type checkers.

## 0.2.0 (unreleased as a package) — additions carried into 0.3.0

### Added
- **The code-file gate.** `open-mhs export <tag>.mhs` generates a standalone, typed Python
  module: one `read_*`/`write_*` per channel, `snapshot`, `check`, `emergency_stop`, plus
  the device's bounds in `BOUNDS` and in every docstring. It enforces nothing locally —
  every write goes to the middleware and is refused there — so a controller written
  against it runs with no model in the loop and is still safe.
- **Reference documents.** `open-mhs doc <tag>.mhs` writes the per-device Markdown an
  agent reads instead of a vendor manual: channels, bounds, why each bound exists,
  conditional envelopes, verification, error codes, and the MCP client snippet. Generated
  entirely from the tag; a test fails if any number in it is not declared there.
- **`examples/exported_controller.py`** — the handover pattern end to end: export, import,
  pace to `max_rate`, sweep the envelope, fit a gain, close the loop with no model, then
  probe past the bound and get refused. Run in CI.

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
- Two bugs found only by installing the wheel into a clean venv and constructing the app:
  `server.routers` was not packaged (flat package list), and the reference drivers read
  their tags from `examples/`, which the wheel does not ship. Subpackages are now found
  by pattern and the three reference tags ship inside `drivers/tags/`; both are guarded
  by tests in `tests/test_packaging.py`.

### Not in this release
- **No real-hardware validation.** Everything is simulated. The serial driver is tested
  against pyserial's loopback only. A validation report against physical metal is the
  most valuable contribution this project can receive.
- **Registry is still in-memory.** The audit log persists; the device list does not.
- **Watchdog is middleware-only.** A driver used without the middleware runs no timer.

## 0.1.0 — 2026-08-31

Initial commit: Capability Tag schema 0.1, FastAPI middleware with two enforcement points,
MCP adapter, mock and serial drivers, PyBullet and robosuite digital twins.
