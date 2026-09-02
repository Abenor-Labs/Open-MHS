# Threat model

What an attacker can reach, what this project defends against, and what it knowingly does
not. Written at the system level; the command-signing wire format has its own narrower
model in [`rt-signing.md`](rt-signing.md).

The point of writing this down is that an unstated gap is worse than a known one. Every
"not defended" row below is a real hole, and several of them are on the roadmap rather
than fixed.

## What is being protected

In order of how much it costs to get wrong:

1. **The physical world.** A machine that moves, heats, dispenses or energises beyond its
   declared envelope. This is the only asset that cannot be restored from backup.
2. **The operator's picture of the world.** A report that says a write succeeded when it
   did not, or that hides a refusal, is dangerous even when nothing moved, because the
   next decision is made on it.
3. **The audit trail.** What was commanded, what was refused, and in what order.
4. **The credentials.** One shared token today, so it is worth exactly as much as the most
   dangerous device on the network.

## The trust boundaries

```text
  operator ─────────────────────────────────────────────────────────┐
     │ writes the tag, holds the token, reads the reports            │
     ▼                                                               │
  ┌─────────┐   tag (free text!) ┌────────────┐                       │
  │  agent  │◄───────────────────│  registry  │                       │
  │ (model) │   readings, refusals└────────────┘                      │
  └────┬────┘                          ▲                              │
       │ MCP / CLI / HTTP              │ POST /register               │
       ▼                               │ (authenticated, NOT attested)│
  ┌──────────────────┐                 │                              │
  │   middleware     │─────────────────┘                              │
  │  enforcement 1   │                                                │
  └────────┬─────────┘                                                │
           │ in-process call                                          │
           ▼                                                          │
  ┌──────────────────┐   third-party code, may lie about readings     │
  │  driver          │   enforcement 2                                │
  └────────┬─────────┘                                                │
           ▼ serial / TCP / GPIO — no authentication at this layer ────┘
      hardware
```

Three boundaries matter. **Agent to middleware** is crossed by a token. **Tag author to
everyone** is crossed by registration, which is authenticated but not attested. **Driver to
hardware** is not crossed by anything: a serial line has no notion of who is talking.

## What is defended, and by what

| Attack | Defence | Evidence |
|---|---|---|
| Agent commands a value outside the envelope | Evaluated against the registry's copy of the tag, before dispatch and again in the driver | `test_safety.py`; every rejection test asserts zero bytes transmitted |
| Agent supplies its own limits in the request | Limits are read from the registry, never from the request body | `rpc.py` reads `tag.limit_map`, not `params` |
| Third-party driver enforces nothing | Middleware refuses first; the two points share one evaluator but never share a result | a test drives the middleware with a deliberately unsafe driver |
| A step change inside the range but too fast | `max_rate`, evaluated against the previous accepted value | `test_safety.py` |
| An actuator left in a dangerous state | `max_duration_s` watchdog returns it to the default, or runs the emergency stop | `test_watchdog.py` |
| A stuck axis reported as success | Feedback verification, `-32003` on disagreement | `test_safety.py` |
| A conditional bound unlocked by a command that did not take effect | Conditions read the *sensor*, and an unreadable channel falls back to the base bound | `test_safety.py` |
| Replayed, forged, or tampered signed command | Ed25519 frame with sequence numbers and domain separation | `test_crypto_bridge.py` |
| A forged human-approval flag | `confirm` lives inside the signed frame | `test_crypto_bridge.py` |
| Unauthenticated access to any hardware route | Mandatory token, constant-time compare, no way to disable it | `test_auth.py` walks every route |
| **Tag prose imitating the report a model reads** | Free text is flattened, delimited and declared as data in the MCP instructions | `test_untrusted_text.py` |
| Edited or deleted audit line | Hash chain; `open-mhs audit verify` names the first broken line | `test_audit.py` |

## Prompt injection through capability tags

This one deserves its own section because it is specific to AI-operated hardware, it
applies to any standard shaped like this one rather than only to ours, and it was live in
this codebase until it was fixed.

**The mechanism.** A Capability Tag carries eleven free-text fields: `description`, each
channel's `name` and `description`, each limit's `rationale` and each condition's
`rationale`, plus `vendor`, `model` and `firmware_version`. All of them are rendered into
the text a model reads on `discover_hardware`, into every refusal, and into the Markdown
that `open-mhs doc` generates. Registration is authenticated but not attested, so anybody
holding the API token — a compromised sensor, a supply-chain tag, a well-meaning vendor
who copied a template — can publish prose aimed at the model rather than at the operator.

**What it cannot do.** It cannot widen a bound. The envelope is evaluated in code from the
registry's copy of the tag with no model involved, so no amount of persuasion changes what
the middleware permits. Both enforcement points are unreachable from this vector.

**What it can do.** Change what the agent *chooses*. Which device it drives next, whether
it stops a neighbouring machine, whether it reports a refusal to the operator or quietly
retries, whether it believes a value it should have re-read. Every one of those is a real
consequence even with the envelope holding, because the operator is reading the agent, not
the middleware.

**The mitigation, and its limits.** `quote_device_text` flattens line structure so injected
prose cannot start what looks like a new section, strips the delimiters out of the text so
it cannot close its own quote, removes control characters, caps the length, and wraps the
result in `<<device-text ... device-text>>`. The MCP server's instructions tell the model
what those delimiters mean and what to do when the text inside them tries to give orders.

That is a mitigation, not a proof. Delimiting and labelling raises the cost of an injection
and makes a successful one visible in the transcript; it does not make a model immune to
persuasion. The measurement — how often a labelled injection actually changes a model's
behaviour, across models — is benchmark work that has not been done here or, as far as we
can tell, published anywhere.

The structural fix is attestation: signed capability tags, so prose arrives with a
publisher attached. That is roadmap work.

## What is not defended

Stated plainly, because each of these is a decision rather than an oversight.

**Registration is authenticated but not attested.** Anyone with the token can register a
device declaring whatever limits it likes, or re-register an existing device with a wider
envelope. The middleware will then enforce those limits faithfully. Signed tags and
per-device credentials are the fix; both are roadmap.

**One shared secret.** A token that can read a thermometer can also command an arm. A
compromised sensor is a compromised cell.

**No transport security.** The token travels in the clear over HTTP. TLS is deployment
guidance that has not been written. On a lab network, assume anyone on the wire has the
token.

**A driver can lie about readings.** The second enforcement point stops a driver
transmitting an out-of-bounds value, but nothing stops it reporting a sensor value that is
false. That defeats feedback verification and conditional bounds, both of which trust the
reading. Driver code is code you are choosing to run.

**Nothing below the driver.** Serial, GPIO and most industrial buses have no
authentication. Anything with physical access to the wire owns the device, and no software
layer changes that.

**The audit log is hash-chained, not signed.** Tampering is detectable after the fact;
rebuilding the whole file by someone with write access is not. Ship it somewhere
append-only.

**The registry does not persist.** A restart forgets every device. That is deliberate — a
registry that survives a restart can hand an agent a tag for hardware that was unplugged —
but it also means an attacker who can restart the process can clear state.

**No rate limiting on the HTTP surface.** `max_rate` bounds how fast an actuator may move.
Nothing bounds how many requests a caller may make.

**The agent is a confused deputy.** A jailbroken or hijacked model holds a valid token and
is, from the middleware's point of view, an authorised operator. Every defence in the first
table still applies to it, which is the entire design premise: the envelope does not care
who is asking.

## What would change these answers

- **Signed capability tags** close attestation, and with it most of the injection surface.
- **Per-device credentials** turn one compromised device into one compromised device.
- **TLS guidance** closes the wire.
- **A published injection benchmark** turns "we mitigated it" into a number.

## Reporting

Do not open a public issue. See [`SECURITY.md`](../SECURITY.md).
