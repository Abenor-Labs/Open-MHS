"""Open-MHS — safety middleware between a language model and physical hardware.

A device declares its own limits in a Capability Tag; nothing outside them reaches the
driver, and a refusal carries the real bound so the caller can correct itself.

Three ways to use this package, in increasing order of how much of it you take:

**As a library, with no server.** Subclass `BaseDevice`, hand it a tag and a transport,
and every `write` is checked against the tag before your transport sees a byte. No HTTP,
no registry, no MCP.

    from open_mhs import BaseDevice, SafetyLimitViolation

    class MyPump(BaseDevice):
        def encode(self, target, value):
            return f"SETFLOW {value}\\r\\n".encode()

    pump = MyPump("bench_pump.mhs", MySerialTransport("/dev/ttyUSB0"))
    await pump.write("flow_rate", 5.0)          # checked, then transmitted
    await pump.write("flow_rate", 500.0)        # SafetyLimitViolation, nothing sent

**As a service.** `create_app()` builds the FastAPI middleware, which adds a device
registry, a JSON-RPC surface, an audit log, and a `max_duration_s` watchdog on top of the
same evaluator. Run it with `open-mhs serve`.

**As an agent tool.** `open-mhs-mcp` exposes a running middleware to any MCP client, and
`open-mhs export` generates a typed module so a controller can drive it with no model.

Everything re-exported here is public and covered by the package's version policy. Names
reached through submodules (`open_mhs.server.routers`, `open_mhs.cli`, and so on) are
internal: they can move in any release.
"""

from __future__ import annotations

from open_mhs.drivers.base import BaseDevice
from open_mhs.drivers.transport import InMemoryTransport, Transport, TransportError
from open_mhs.server.audit import AuditLog
from open_mhs.server.audit import verify as verify_audit_log
from open_mhs.server.errors import (
    DeviceNotFound,
    HardwareExecutionError,
    InvalidParams,
    MHSError,
    SafetyLimitViolation,
    StateDesync,
)
from open_mhs.server.main import create_app
from open_mhs.server.models import (
    LATEST_SPEC_VERSION,
    SUPPORTED_SPEC_VERSIONS,
    Actuator,
    CapabilityTag,
    EmergencyStop,
    LimitCondition,
    SafetyLimit,
    Sensor,
)
from open_mhs.server.registry import Registry
from open_mhs.server.safety import SafetyDecision, check_write, effective_bounds

#: Implementation version. Distinct from the Capability Tag spec version
#: (`LATEST_SPEC_VERSION`), which is a wire contract between anyone who writes a tag and
#: anyone who reads one. See the Versioning section of the README.
__version__ = "0.3.1"

__all__ = [
    # the specification, as types
    "CapabilityTag",
    "Sensor",
    "Actuator",
    "SafetyLimit",
    "LimitCondition",
    "EmergencyStop",
    "LATEST_SPEC_VERSION",
    "SUPPORTED_SPEC_VERSIONS",
    # writing a driver
    "BaseDevice",
    "Transport",
    "InMemoryTransport",
    "TransportError",
    # the enforcement itself
    "check_write",
    "effective_bounds",
    "SafetyDecision",
    # running the middleware
    "create_app",
    "Registry",
    "AuditLog",
    "verify_audit_log",
    # what a refusal looks like
    "MHSError",
    "SafetyLimitViolation",
    "StateDesync",
    "HardwareExecutionError",
    "DeviceNotFound",
    "InvalidParams",
    "__version__",
]
