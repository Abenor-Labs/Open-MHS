# Standards map

Machinery safety has a settled vocabulary, and this project borrows two of its words —
`enforcement` and `hazard_class`. This document says exactly what they mean here, what
Open-MHS does **not** claim, and how a tag fits into a safety case that a person is
responsible for signing.

Read the second section first. It is the one that matters.

## What Open-MHS is not

**It is not a safety-related control system.** In the sense of ISO 13849-1 or IEC 62061,
a safety function has a rated architecture, a diagnostic coverage figure, a mean time to
dangerous failure, and an analysis of common-cause failure. Open-MHS is ordinary Python on
general-purpose hardware and an ordinary operating system, with no redundancy, no
self-test, and no independent monitoring channel. It therefore claims **no Performance
Level and no Safety Integrity Level**, and any claim otherwise would be false.

**It is not a substitute for a physical interlock.** If a movement can injure someone, the
thing that stops it must be able to stop it when this software is not running, has crashed,
or has been misconfigured. That is a hardware interlock, a rated safety relay, a light
curtain, an E-stop circuit — something in the energy path rather than in the command path.
Open-MHS refuses commands. A refused command and a de-energised motor are different
guarantees, and only one of them survives a power failure in the wrong state.

**It is not a certification.** Nobody has assessed this code against any standard. The
conformance suite, when it ships, tests whether an implementation reads the Capability Tag
format correctly. That is interoperability, not functional safety.

**A tag is a declaration, not an assurance.** A tag says what its author believes the
device's limits are. Whether those numbers are right, and whether anything downstream can
actually hold them, is outside what any software here can verify. This is why
`enforcement` exists and why its default is the pessimistic value.

What Open-MHS *is*: a machine-readable declaration of a device's limits, a documented and
tested layer that refuses commands outside them, and a tamper-evident record of what was
commanded and what was refused. In a safety case those are contributions to risk reduction
and to traceability. They are not the safety function.

## The vocabulary, mapped

### `enforcement`

Each safety limit declares where it is actually enforced. The word is doing real work here:
it is the tag author telling the reader how much the software layer is carrying.

| Tag value | Means | Rough analogue | What it is worth |
|---|---|---|---|
| `hardware` | Physically cannot be exceeded: a mechanical stop, an endstop switch in the energy path, a fuse, a relief valve | The kind of measure ISO 12100 calls inherently safe design or a guard | Survives software failure, power loss and misconfiguration |
| `firmware` | The device itself rejects the command | A protective measure in the device's own control system, whose rating is the *device's* to declare, not ours | Survives this middleware failing, not the device failing |
| `software` | **Only this specification stands between the agent and the hardware** | Not a protective measure in the ISO 13849 sense at all | Survives nothing; it is a functional check |

`software` is the default because it is the pessimistic assumption, and declaring
`hardware` when no physical stop exists is the most dangerous thing a tag author can do.
Nothing in this project can verify the claim. A reviewer should treat `enforcement:
hardware` in a tag as an assertion requiring the same evidence as any other safety claim:
a drawing, a part number, a test.

**The schema does not carry a Performance Level, deliberately.** Adding a `performance_level`
field would invite tags to assert a rating that this project cannot check and did not earn.
If a device's interlock is rated, that rating belongs in the device's own documentation and
in the safety case, cited by the tag's `rationale` rather than encoded as a number the
middleware would appear to be validating. Changing that is an RFC, and it would have to
answer how a reader distinguishes a verified rating from a claimed one.

### `hazard_class`

`power.hazard_class` says what kind of harm the device can do: `mechanical`, `thermal`,
`electrical`, `chemical`, `optical`, `biological`, `radiation`, or `none`. It is a routing
hint, not a severity score. It tells an agent and an operator which family of consequences
is in play, and it is what makes the generated device document lead with a warning.

It is deliberately not a risk estimate. Severity, frequency of exposure and possibility of
avoidance — the inputs to a risk graph in ISO 12100 or ISO 13849-1 Annex A — depend on the
installation, not on the device, so no field in a device's own tag could carry them
honestly.

### The other fields, and what they correspond to

| Tag field | The idea it encodes |
|---|---|
| `safety_limits[].min`/`max` | The declared operating envelope; the boundary a command is checked against |
| `max_rate` | A limit on rate of change, so a command that is in-range at both ends cannot be destructive in between |
| `max_duration_s` | A dead-man requirement: an actuator may not be held away from its default indefinitely |
| `on_violation: estop` | A request to drive to the declared safe state, which is a *command*, not an emergency stop function in the ISO 13850 sense |
| `emergency_stop.safe_state` | The declared safe state, and `max_stop_time_ms` the author's claim about how long reaching it takes |
| `requires_confirmation` | A human authorisation step in the command path |
| `feedback_sensor` | Closed-loop verification that a command took effect, reported as a state desync when it did not |

**On `on_violation: estop` and `emergency_stop`.** ISO 13850 emergency stop is a hardwired
function that remains effective independently of the control system, in every operating
mode. What this project calls an emergency stop is a software command that drives declared
actuators to declared values. It is useful and it is tested; it is not an ISO 13850
emergency stop, and a cell needs a real one regardless.

## Where the audit log fits

EU Regulation 2023/1230 replaces the Machinery Directive on **20 January 2027** and, for
the first time, brings machinery with AI-based safety functions and self-evolving behaviour
into scope. Two consequences are worth naming even at this project's stage.

**A file that constrains a machine's motion may itself be a regulated safety component.**
If an integrator relies on a Capability Tag's bounds as part of their protective measures,
the tag is inside their conformity assessment, not outside it. That is a reason to treat a
tag as a controlled document — reviewed, versioned, and traceable to the person who wrote
it — and a reason the spec versioning policy is strict about changes.

**Traceability is asked for, and the audit log is the artifact.** Every command, every
refusal, the value that was transmitted or `null`, and a hash chain that makes an edited or
deleted line detectable. See [`audit-log.md`](audit-log.md). It is hash-chained rather than
signed, so it evidences integrity against tampering after the fact, not against someone
with write access rebuilding it. Ship it to append-only storage if you need more.

Neither of those makes anything compliant. Regulatory conformity is the machine builder's
or integrator's responsibility for their specific installation, and nothing shipped here
can discharge it.

## Using this in a real safety case

If you are the person responsible, the honest shape of it:

1. **Do the risk assessment first**, to ISO 12100 or your sector's equivalent. It tells you
   which risks need a rated protective measure. Open-MHS is not an input to that decision.
2. **Implement those measures in hardware.** Guards, interlocks, rated relays, E-stop
   circuits, torque and speed limiting in a drive that is rated for it.
3. **Write the tag to match, and mark it honestly.** A limit backed by an endstop is
   `hardware`. A limit that exists only in this file is `software`, and its `rationale`
   should say what would happen if it were exceeded.
4. **Treat Open-MHS as a functional layer above that**: it reduces how often an agent
   commands something outside the envelope, it makes the refusal legible, and it records
   both. Claim exactly that in the safety case and no more.
5. **Test the hardware measures with the software absent.** If the machine is safe only
   while this process is running, it is not safe.

## Sources, and what has not been done

The standards referenced: ISO 12100 (risk assessment), ISO 13849-1 and IEC 62061
(safety-related control systems), IEC 61508 (functional safety of electronic systems),
ISO 13850 (emergency stop), ISO 10218-1 and -2 (industrial robots and their integration),
ISO/TS 15066 (collaborative operation), EU Regulation 2023/1230 (machinery, applicable
20 January 2027).

**Not verified.** This map was written by reading the standards' scope and terminology, not
by a certified functional safety engineer, and no clause-by-clause gap analysis has been
performed. It is intended to keep this project's language honest and to help an integrator
place it correctly, not to be relied on as compliance advice. A review by someone qualified
would be a genuinely valuable contribution, and corrections are welcome as ordinary issues.
