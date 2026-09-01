# Signed commands for the management plane

How an Open-MHS write is authenticated before it reaches a real-time control loop.

**Status: specification and Python reference implementation.** The C++ side described here
is not built. `tests/test_crypto_bridge.py` implements and tests the Python half; it is the
executable version of this document, and the two must not disagree.

---

## 1. What this does and does not give you

A signature proves **who issued a command**. It proves nothing about whether the command is
safe.

`tcp_x = 0.9` signed by the genuine operator console is still a command to drive the arm
half a metre outside its measured envelope. The safety layer refuses it exactly as it
refuses an unsigned one.

> **Authenticated is not authorised.** The envelope is evaluated *after* the signature,
> never *instead of* it. If signing ever becomes a route past a bound, the project has
> built an authenticated way to break the machine.

`test_signed_but_unsafe_is_still_refused` asserts this against the real evaluator and the
real capability tag, not a stub.

**Threat model.** Signing defends against an attacker who can reach the command port:
forged commands, replayed commands, tampered values, and escalation of the human-approval
flag. It does **not** defend against an attacker with root on the RT host — they can drive
the hardware directly. What it preserves in that case is attribution: they cannot forge a
command the audit log will blame on a legitimate operator. Anyone claiming signing stops
local root is overselling it.

---

## 2. The signed frame

JSON is not canonical. Key order, float formatting, and whitespace all vary between
senders, so signing serialized JSON means a valid signature can fail depending on who sent
it — indistinguishable from an attack, and impossible to debug from logs.

So a fixed 200-byte binary frame is signed, and JSON merely carries it.

```
offset  size  field           notes
------  ----  --------------  ------------------------------------------------------
     0    20  domain          "open-mhs/v1/write", zero-padded
    20    32  key_id          zero-padded, NOT NUL-terminated
    52    32  device_id
    84    32  target
   116     1  value_type      0=f64  1=bool  2=enum/string  3=vector3
   117     1  flags           bit0 = confirm (human approval)
   118     2  reserved0       zero
   120    24  value_num[3]    f64 little-endian; unused lanes MUST be zero
   144    32  value_str       enum/string values; zeroed otherwise
   176     8  seq             u64, strictly increasing per key_id
   184     8  issued_at_ns    u64, signer's CLOCK_REALTIME
   192     4  expires_in_ms   u32
   196     4  reserved1       zero
------  ----  --------------  ------------------------------------------------------
         200  total
```

C++:

```cpp
#pragma pack(push, 1)
struct SignedFrame {
  char     domain[20];
  char     key_id[32];
  char     device_id[32];
  char     target[32];
  uint8_t  value_type;
  uint8_t  flags;
  uint16_t reserved0;
  double   value_num[3];
  char     value_str[32];
  uint64_t seq;
  uint64_t issued_at_ns;
  uint32_t expires_in_ms;
  uint32_t reserved1;
};
#pragma pack(pop)
static_assert(sizeof(SignedFrame) == 200, "layout is part of the wire contract");
```

Python (`tests/test_crypto_bridge.py`):

```python
FRAME = struct.Struct("<20s32s32s32sBBH3d32sQQII")   # 200 bytes
```

`<` means little-endian **and unpadded**, matching `#pragma pack(1)`. Native alignment
would silently insert padding and break the contract between the two languages.

### Byte-packing rules

These are requirements, not style. Violating any one produces signatures that verify on one
sender and fail on another.

1. **Zero the whole struct before populating.** Uninitialised padding is the classic reason
   signatures stop reproducing across compilers. Use `sodium_memzero`, not `memset`.
2. **Unused value lanes are zero.** An `f64` command zeroes `value_num[1..2]` and all of
   `value_str`. Both fields are signed regardless of `value_type`, so leaving either
   undefined is nondeterministic.
3. **Strings are zero-padded, not NUL-terminated.** Every byte of the field is signed;
   there is no "don't care" tail.
4. **Reserved fields are zero** and are signed. They exist so the layout can grow without a
   version bump for additive changes.
5. **Little-endian throughout**, including on a big-endian host. Byte order is part of the
   format, not of the platform.

### Domain separation

`domain` is not decoration. Without it, a signature captured from one message type can be
replayed as another wherever the field layouts happen to line up. Bumping `v1` invalidates
every existing signature at once — which is the behaviour you want during a protocol
change.

### Why `confirm` is inside the frame

`requires_confirmation` is the human-approval gate on actuators like `gripper_state`. It is
a security property, so it is signed. If it travelled outside the frame, anyone who
captured a gripper command could flip approval on and replay it.
`test_tampered_confirm_flag_fails_verification` covers exactly that.

---

## 3. JSON-RPC envelope

```json
{
  "jsonrpc": "2.0", "id": 42, "method": "mhs.write",
  "params": {
    "device_id": "panda-arm-01",
    "target": "tcp_z",
    "value": 0.93,
    "confirm": false,
    "auth": {
      "alg": "ed25519",
      "key_id": "console-01",
      "seq": 918273,
      "issued_at_ns": 1788254992834375100,
      "expires_in_ms": 250,
      "sig": "base64(64 bytes)"
    }
  }
}
```

**The verifier reconstructs the frame from its own parsed fields.** It never signature-checks
bytes handed to it. Any disagreement between what was signed and what the server parsed then
surfaces as a signature failure, rather than as a semantic difference nobody notices.

---

## 4. Verification ordering

```
domain -> key -> freshness -> signature -> replay -> envelope -> swap
```

Two of those positions are load-bearing.

**Freshness before signature.** Ed25519 verification costs roughly 50–100 µs. A flood of
stale frames should not buy an attacker that much CPU each; the timestamp check is nearly
free and filters them first.

**Replay after signature.** If an unverified frame could advance the sequence counter,
anyone able to reach the port could push it to 2⁶⁴ and permanently lock out the real
console — denial of service with no key required.
`test_a_bad_signature_cannot_burn_sequence_numbers` asserts the counter does not move.

| Step | Refuses | Reason |
| --- | --- | --- |
| domain | `BadDomain` | cross-protocol signature reuse |
| key lookup | `UnknownKey` / `RevokedKey` | not provisioned, or withdrawn |
| freshness | `Stale` | expired, or future-dated past clock skew |
| signature | `BadSignature` | forged or tampered |
| replay | `Replay` | `seq` not strictly greater than the last accepted |
| **envelope** | **`-32001`** | **authentic, but outside the declared bound** |

Future-dating is refused past `MAX_SKEW_NS` (2 s) because a frame accepted from the future
can be parked and replayed for as long as its window lasts.

### Where each step runs

**All of it on the management thread. None of it on the RT thread.**

At 1 kHz the control callback has 1000 µs total; an Ed25519 verify at 50–100 µs is 5–10% of
the budget for a check that has nothing to do with control. Verification, envelope
evaluation, and refusal all happen before anything is published to the ring buffer. The RT
thread consumes setpoints that are already authenticated and already in-bounds.

```cpp
void ManagementThread::OnWrite(const JsonRpcRequest& req) {
  SignedFrame frame; unsigned char sig[crypto_sign_BYTES];
  if (!BuildFrameFromParams(req, &frame, sig)) return Reply(req, kInvalidParams);
  if (keyring_.Verify(frame, sig, NowRealtimeNs()) != AuthResult::Ok)
    return Reply(req, kUnauthorized);

  const auto decision = safety_.CheckWrite(frame);      // authentic != authorised
  if (!decision.admissible)
    return Reply(req, kSafetyLimitViolation, decision.bound);

  setpoint_ring_.Publish(decision.value);               // only now
  Reply(req, kAccepted, decision);
}
```

---

## 5. Key distribution

Design so that owning the robot does not let an attacker forge commands.

- **Private keys never touch the RT host.** They live on the operator console, in an HSM,
  or behind a signing service. The robot holds public keys only.
- **Provisioned out-of-band**: configuration management, image build, or TPM-sealed. Never
  fetched over the network at runtime — a runtime fetch is a key-substitution surface.
- **On disk**: `/etc/open-mhs/keys/`, `root:root`, directory `0555`, files `0444`, ideally
  on a read-only mount. Loaded once at startup, before the RT thread spawns and before
  dropping privileges.
- **Rotation**: add the new key, run both through an overlap window, then revoke the old.
  A fixed-size keyring (16 slots) avoids allocation on the management path.
- **Revocation is local state, not a network CRL.** A robot that must reach the internet to
  learn a key was revoked fails the wrong way when the network is down.

---

## 6. Running it

```bash
pip install pynacl
pytest tests/test_crypto_bridge.py     # 16 tests
python tests/test_crypto_bridge.py     # narrated demo
```

`pynacl` is optional. The suite uses `pytest.importorskip`, so a checkout without it still
runs green — the crypto bridge is a prototype, not core middleware.

---

## 7. Not verified

- **No C++ implementation exists.** The struct and the verification sketch are a
  specification. Nothing has been compiled or cross-checked against the Python frame.
- **The 50–100 µs verify figure is from general benchmarks, not measured on an RT_PREEMPT
  host.** The whole "management plane only" argument rests on it; measure it on the target
  before budgeting.
- **No Franka FCI integration.** The 1 kHz callback contract and its behaviour on a missed
  deadline should be confirmed against Franka's documentation rather than assumed.
- The Python reference uses a `dict` keyring; the C++ sketch uses a fixed array. Only the
  wire format is contractual, not the storage.
- **No cross-language round-trip test.** The strongest next check is signing a frame in
  Python and verifying it in C++ (and the reverse), which is the only thing that actually
  proves the two layouts agree.
