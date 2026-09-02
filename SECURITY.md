# Security Policy

Open-MHS sits between a language model and machinery that can move. A defect here is not a
data-integrity problem; it is a physical one. Please treat it accordingly.

The full system threat model — what is defended, what is not, and why — is in
[`docs/threat-model.md`](docs/threat-model.md). The table below is a summary of the gaps.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting on this repository:
**Security → Report a vulnerability**. That opens a private channel with the maintainers.

Useful things to include:

- What an attacker (or a confused agent) can cause the hardware to do.
- Whether the middleware, a driver, a capability tag, or the MCP adapter is at fault.
- A minimal reproduction — ideally a test against the in-memory transport, so nobody has to
  point real hardware at it to confirm.

You will get an acknowledgement. This is an alpha project maintained in the open, so please
do not expect an enterprise SLA; do expect the problem to be taken seriously.

## Supported versions

| Version | Supported |
| --- | --- |
| `0.1.x` (alpha) | ✅ current |
| anything earlier | ❌ |

The Capability Tag schema is the stable surface. The middleware and drivers are reference
implementations and may change.

## What Open-MHS protects against

- A command outside a device's declared safety envelope reaching the hardware. Checked
  twice — in the middleware before dispatch, and in the driver before transmission.
- A driver that enforces nothing being handed an out-of-bounds command.
- A caller attaching its own limits to a request to widen its envelope. Limits are read
  from the registry's copy of the tag, never from the request body.
- A write aimed at a read-only sensor.
- Unauthenticated access. The server refuses to start without `OPEN_MHS_AUTH_TOKEN`, and
  there is no flag to disable that.
- A silently clamped value being reported as a plain success.

## What Open-MHS does *not* protect against

Stated plainly, because a safety project that oversells its guarantees is worse than one
that has none.

| Gap | Consequence |
| --- | --- |
| **Capability tags are unsigned.** | Anyone holding the API token can register a tag declaring whatever limits they like. Authentication is not attestation. |
| **One shared secret, no per-device identity.** | Any token holder can command any registered device. A compromised sensor's credentials can drive an arm. |
| **No transport security in-process.** | A bearer token over plain HTTP is a token in everyone's packet capture. Terminate TLS in front of the server for anything but localhost. |
| **`enforcement: software` is a claim, not a proof.** | The middleware cannot verify that a limit declared as `hardware` corresponds to a real physical stop. A dishonest or mistaken tag is enforced exactly as written. |
| **`max_duration_s` is parsed but not enforced.** | A target can be held away from its default indefinitely. |
| **The registry is in-memory.** | Devices must re-announce after a restart; the device list is not persisted. |
| **The audit log is hash-chained, not signed.** | `open-mhs audit verify` detects edits and deletions. It cannot detect a full rebuild by someone with write access to the file. Ship it to append-only storage. |

## The rule that matters most

**Software is a layer of defence, never the only one.**

Do not connect Open-MHS to hardware that can injure a person or destroy an experiment
without an independent hardware interlock — an endstop, a physical e-stop, a current limit,
a fuse. Every limit in a capability tag that honestly reads `enforcement: software` is one
bug away from not being enforced at all.

## Scope

In scope: the middleware, the safety evaluation, the drivers, the MCP adapter, the schema,
and the authentication layer.

Out of scope: vulnerabilities in third-party dependencies (report those upstream, though a
heads-up here is welcome), and the PyBullet demo, which touches no real hardware.
