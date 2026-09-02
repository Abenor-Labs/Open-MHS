"""Ed25519 signing for the Open-MHS management plane: reference implementation.

    pytest tests/test_crypto_bridge.py          # as a suite
    python tests/test_crypto_bridge.py          # as a demo, prints the flow

This is the Python half of a two-language contract. The wire format defined here is the
same 200-byte frame `docs/rt-signing.md` specifies for the C++ management thread, so a
frame signed by this module must verify in C++ and vice versa. Proving the flow here is
cheaper than proving it in C++ on an RT host.

WHAT THIS DOES AND DOES NOT PROVE
---------------------------------

A signature authenticates the SENDER. It does not make the command safe. `tcp_x = 0.9`
signed by the real operator console is still a command to drive the arm out of its
measured envelope, and the safety layer refuses it exactly as it refuses an unsigned one.
`test_signed_but_unsafe_is_still_refused` asserts that against the real evaluator and the
real capability tag, because "authenticated" being mistaken for "authorised" is the
failure mode that would matter most in the field.

ORDERING
--------

    domain -> key -> freshness -> signature -> replay -> envelope -> swap

Two orderings in that chain are load-bearing rather than stylistic:

* Freshness BEFORE signature verification, because Ed25519 verify costs ~50-100 us and a
  flood of stale frames should not buy an attacker that much CPU each.
* Replay AFTER signature verification, because committing the sequence number for an
  unverified frame lets an unauthenticated peer burn sequence numbers and lock out the
  real console. That is a denial of service for free.
"""

from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

import pytest

# Importable as a script as well as under pytest, which puts the repo root on the path
# itself. The safety evaluator is imported from `server`, so the root has to be reachable.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

nacl = pytest.importorskip(
    "nacl", reason="pynacl is optional; the crypto bridge is a prototype, not core"
)
from nacl import exceptions as nacl_exceptions  # noqa: E402
from nacl.signing import SigningKey, VerifyKey   # noqa: E402

# --------------------------------------------------------------------------------------
# Wire format -- must stay byte-identical to the C++ struct in docs/rt-signing.md
# --------------------------------------------------------------------------------------

#: char[20] key_id[32] device_id[32] target[32] u8 u8 u16 f64[3] char[32] u64 u64 u32 u32
#: '<' is little-endian AND unpadded, which is what `#pragma pack(1)` gives on the C++
#: side. Native alignment would silently insert padding and break the contract.
FRAME = struct.Struct("<20s32s32s32sBBH3d32sQQII")
FRAME_BYTES = 200

#: Domain separation. Without it, a signature captured from one message type can be
#: replayed as another. Bumping the version invalidates every existing signature, which
#: is the desired behaviour during a protocol change.
DOMAIN = b"open-mhs/v1/write"

VALUE_F64, VALUE_BOOL, VALUE_STR, VALUE_VEC3 = 0, 1, 2, 3
FLAG_CONFIRM = 0x01

#: How far a signer's clock may run ahead of ours before we refuse. A future-dated frame
#: that we accepted could be parked and replayed for as long as the skew allows.
MAX_SKEW_NS = 2_000_000_000

NS = 1_000_000_000


def _fixed(text: str | bytes, size: int) -> bytes:
    """Zero-pad to a fixed width. Not NUL-terminated: every byte is signed."""
    raw = text.encode() if isinstance(text, str) else text
    if len(raw) > size:
        raise ValueError(f"{raw!r} exceeds the {size}-byte field")
    return raw.ljust(size, b"\x00")


def pack_frame(
    *,
    key_id: str,
    device_id: str,
    target: str,
    value: float | bool | str | list[float],
    seq: int,
    issued_at_ns: int,
    expires_in_ms: int = 250,
    confirm: bool = False,
    domain: bytes = DOMAIN,
) -> bytes:
    """Build the exact bytes that get signed.

    The verifier RECONSTRUCTS this from its own parsed JSON rather than trusting bytes off
    the wire. Any disagreement between what was signed and what was parsed then shows up
    as a signature failure instead of as a subtle semantic difference.

    Unused value lanes are zeroed. Leaving them undefined would make signatures
    unreproducible across senders, which is the classic way this kind of frame breaks.
    """
    numbers = [0.0, 0.0, 0.0]
    text = b""
    if isinstance(value, bool):                 # bool before float: bool IS an int
        value_type = VALUE_BOOL
        numbers[0] = 1.0 if value else 0.0
    elif isinstance(value, (int, float)):
        value_type = VALUE_F64
        numbers[0] = float(value)
    elif isinstance(value, str):
        value_type = VALUE_STR
        text = _fixed(value, 32)
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        value_type = VALUE_VEC3
        numbers = [float(v) for v in value]
    else:
        raise ValueError(f"unsupported value {value!r}")

    frame = FRAME.pack(
        _fixed(domain, 20),
        _fixed(key_id, 32),
        _fixed(device_id, 32),
        _fixed(target, 32),
        value_type,
        FLAG_CONFIRM if confirm else 0,     # the human-approval gate is SIGNED
        0,
        *numbers,
        _fixed(text, 32),
        seq,
        issued_at_ns,
        expires_in_ms,
        0,
    )
    assert len(frame) == FRAME_BYTES, "frame layout drifted from the wire contract"
    return frame


def unpack_frame(frame: bytes) -> dict:
    """Decode for inspection and logging. Never used to decide anything."""
    fields = FRAME.unpack(frame)
    return {
        "domain": fields[0].rstrip(b"\x00").decode(),
        "key_id": fields[1].rstrip(b"\x00").decode(),
        "device_id": fields[2].rstrip(b"\x00").decode(),
        "target": fields[3].rstrip(b"\x00").decode(),
        "value_type": fields[4],
        "confirm": bool(fields[5] & FLAG_CONFIRM),
        "value_num": list(fields[7:10]),
        "value_str": fields[10].rstrip(b"\x00").decode(),
        "seq": fields[11],
        "issued_at_ns": fields[12],
        "expires_in_ms": fields[13],
    }


# --------------------------------------------------------------------------------------
# Verifier -- the management plane. Never the control plane.
# --------------------------------------------------------------------------------------


class AuthError(Exception):
    """Refused before any safety evaluation happened. Nothing was transmitted."""


class UnknownKey(AuthError): pass          # noqa: E701
class RevokedKey(AuthError): pass          # noqa: E701
class BadDomain(AuthError): pass           # noqa: E701
class BadSignature(AuthError): pass        # noqa: E701
class Stale(AuthError): pass               # noqa: E701
class Replay(AuthError): pass              # noqa: E701


class KeyRing:
    """Public keys and per-key replay state.

    Only PUBLIC keys live here. The private half stays on the operator console or in an
    HSM and never reaches the robot, so compromising the robot yields no ability to forge
    a command that the audit log will attribute to an operator.
    """

    def __init__(self) -> None:
        self._keys: dict[str, VerifyKey] = {}
        self._last_seq: dict[str, int] = {}
        self._revoked: set[str] = set()

    def add(self, key_id: str, verify_key: VerifyKey) -> None:
        self._keys[key_id] = verify_key
        self._last_seq.setdefault(key_id, 0)

    def revoke(self, key_id: str) -> None:
        """Local state, not a network CRL. A robot that must reach the internet to learn
        a key has been revoked fails the wrong way when the network is down."""
        self._revoked.add(key_id)

    def last_seq(self, key_id: str) -> int:
        return self._last_seq.get(key_id, 0)

    def verify(self, frame: bytes, signature: bytes, *, now_ns: int | None = None) -> dict:
        """Authenticate one frame. Returns the decoded fields, or raises.

        Authenticating is NOT authorising. The caller must still put the result through
        the safety envelope before anything reaches the hardware.
        """
        if len(frame) != FRAME_BYTES:
            raise BadDomain(f"frame is {len(frame)} bytes, expected {FRAME_BYTES}")
        now_ns = time.time_ns() if now_ns is None else now_ns
        fields = unpack_frame(frame)

        # 1. Domain separation.
        if fields["domain"].encode() != DOMAIN:
            raise BadDomain(f"wrong domain {fields['domain']!r}")

        key_id = fields["key_id"]
        if key_id in self._revoked:
            raise RevokedKey(key_id)
        verify_key = self._keys.get(key_id)
        if verify_key is None:
            raise UnknownKey(key_id)

        # 2. Freshness, before the expensive part. Also refuses future-dated frames
        #    beyond clock skew: those could otherwise be parked and replayed later.
        age_ns = now_ns - fields["issued_at_ns"]
        if age_ns > fields["expires_in_ms"] * 1_000_000:
            raise Stale(f"expired {age_ns / 1e6:.1f} ms ago")
        if age_ns < -MAX_SKEW_NS:
            raise Stale(f"dated {-age_ns / 1e9:.1f} s in the future")

        # 3. Signature.
        try:
            verify_key.verify(frame, signature)
        except nacl_exceptions.BadSignatureError as exc:
            raise BadSignature(f"{key_id}: signature does not verify") from exc

        # 4. Replay -- committed only now. Checking it earlier would let an
        #    unauthenticated peer advance the counter and lock out the real console.
        if fields["seq"] <= self._last_seq.get(key_id, 0):
            raise Replay(
                f"seq {fields['seq']} <= last accepted {self._last_seq.get(key_id, 0)}"
            )
        self._last_seq[key_id] = fields["seq"]
        return fields


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------

CONSOLE = "console-01"
ARM = "panda-arm-01"


@pytest.fixture
def signing_key() -> SigningKey:
    """Deterministic: a test that regenerates entropy each run cannot be bisected."""
    return SigningKey(b"\x11" * 32)


@pytest.fixture
def keyring(signing_key: SigningKey) -> KeyRing:
    ring = KeyRing()
    ring.add(CONSOLE, signing_key.verify_key)
    return ring


def _signed(signing_key: SigningKey, **kwargs) -> tuple[bytes, bytes]:
    kwargs.setdefault("key_id", CONSOLE)
    kwargs.setdefault("device_id", ARM)
    kwargs.setdefault("target", "tcp_z")
    kwargs.setdefault("value", 0.93)
    kwargs.setdefault("seq", 1)
    kwargs.setdefault("issued_at_ns", time.time_ns())
    frame = pack_frame(**kwargs)
    return frame, signing_key.sign(frame).signature


# --------------------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------------------


def test_frame_is_exactly_200_bytes():
    """The size is part of the contract with the C++ struct, not an implementation detail."""
    assert FRAME.size == FRAME_BYTES


def test_packing_is_deterministic(signing_key):
    """Two senders building the same command must produce identical bytes.

    If they did not, a valid signature would fail to verify depending on who sent it --
    which looks exactly like an attack and is impossible to debug from the logs.
    """
    now = time.time_ns()
    a = pack_frame(key_id=CONSOLE, device_id=ARM, target="tcp_z", value=0.93,
                   seq=7, issued_at_ns=now)
    b = pack_frame(key_id=CONSOLE, device_id=ARM, target="tcp_z", value=0.93,
                   seq=7, issued_at_ns=now)
    assert a == b


def test_unused_value_lanes_are_zeroed():
    """Undefined padding is the classic reason signatures stop reproducing across senders."""
    frame = pack_frame(key_id=CONSOLE, device_id=ARM, target="tcp_z", value=0.93,
                       seq=1, issued_at_ns=time.time_ns())
    fields = unpack_frame(frame)
    assert fields["value_num"][1:] == [0.0, 0.0]
    assert fields["value_str"] == ""


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


def test_valid_command_is_accepted(keyring, signing_key):
    frame, sig = _signed(signing_key, seq=1)
    fields = keyring.verify(frame, sig)
    assert fields["device_id"] == ARM
    assert fields["target"] == "tcp_z"
    assert fields["value_num"][0] == pytest.approx(0.93)
    assert keyring.last_seq(CONSOLE) == 1


def test_sequence_advances_monotonically(keyring, signing_key):
    for seq in (1, 2, 7, 100):
        frame, sig = _signed(signing_key, seq=seq)
        keyring.verify(frame, sig)
    assert keyring.last_seq(CONSOLE) == 100


# --------------------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------------------


def test_replayed_frame_is_rejected(keyring, signing_key):
    """The captured bytes are byte-identical and the signature is genuine. Only the
    sequence number stops it -- which is the entire reason it is in the signed frame."""
    frame, sig = _signed(signing_key, seq=5)
    keyring.verify(frame, sig)

    with pytest.raises(Replay):
        keyring.verify(frame, sig)          # same bytes, same valid signature


def test_out_of_order_frame_is_rejected(keyring, signing_key):
    keyring.verify(*_signed(signing_key, seq=10))
    with pytest.raises(Replay):
        keyring.verify(*_signed(signing_key, seq=9))


def test_a_bad_signature_cannot_burn_sequence_numbers(keyring, signing_key):
    """DoS resistance, and the reason replay is checked AFTER the signature.

    If an unauthenticated frame could advance the counter, anyone able to reach the port
    could push it to 2^64 and permanently lock out the real console without ever holding
    a key.
    """
    attacker = SigningKey(b"\x99" * 32)
    frame = pack_frame(key_id=CONSOLE, device_id=ARM, target="tcp_z", value=0.93,
                       seq=999_999, issued_at_ns=time.time_ns())
    with pytest.raises(BadSignature):
        keyring.verify(frame, attacker.sign(frame).signature)

    assert keyring.last_seq(CONSOLE) == 0, "an unverified frame advanced the counter"
    keyring.verify(*_signed(signing_key, seq=1))       # the real console still works


# --------------------------------------------------------------------------------------
# Tampering
# --------------------------------------------------------------------------------------


def test_tampered_value_fails_verification(keyring, signing_key):
    now = time.time_ns()
    frame, sig = _signed(signing_key, value=0.93, seq=1, issued_at_ns=now)
    forged = pack_frame(key_id=CONSOLE, device_id=ARM, target="tcp_z", value=0.50,
                        seq=1, issued_at_ns=now)
    assert forged != frame
    with pytest.raises(BadSignature):
        keyring.verify(forged, sig)


def test_tampered_confirm_flag_fails_verification(keyring, signing_key):
    """`confirm` is the human-approval gate, so it lives INSIDE the signed frame.

    Outside it, an attacker who captured any gripper command could flip approval on and
    replay it. This asserts the gate cannot be granted by anyone but the signer.
    """
    now = time.time_ns()
    frame, sig = _signed(signing_key, target="gripper_state", value="closed",
                         confirm=False, seq=1, issued_at_ns=now)
    escalated = pack_frame(key_id=CONSOLE, device_id=ARM, target="gripper_state",
                           value="closed", confirm=True, seq=1, issued_at_ns=now)
    with pytest.raises(BadSignature):
        keyring.verify(escalated, sig)


def test_wrong_domain_is_rejected(keyring, signing_key):
    """A signature is only valid for the protocol it was issued under."""
    frame = pack_frame(key_id=CONSOLE, device_id=ARM, target="tcp_z", value=0.93,
                       seq=1, issued_at_ns=time.time_ns(), domain=b"open-mhs/v0/write")
    with pytest.raises(BadDomain):
        keyring.verify(frame, signing_key.sign(frame).signature)


def test_unknown_and_revoked_keys_are_rejected(keyring, signing_key):
    stranger = SigningKey(b"\x42" * 32)
    frame = pack_frame(key_id="not-provisioned", device_id=ARM, target="tcp_z",
                       value=0.93, seq=1, issued_at_ns=time.time_ns())
    with pytest.raises(UnknownKey):
        keyring.verify(frame, stranger.sign(frame).signature)

    keyring.revoke(CONSOLE)
    with pytest.raises(RevokedKey):
        keyring.verify(*_signed(signing_key, seq=1))


# --------------------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------------------


def test_expired_frame_is_rejected(keyring, signing_key):
    issued = time.time_ns() - 5 * NS
    frame, sig = _signed(signing_key, seq=1, issued_at_ns=issued, expires_in_ms=250)
    with pytest.raises(Stale):
        keyring.verify(frame, sig)


def test_future_dated_frame_is_rejected(keyring, signing_key):
    """Otherwise a captured frame can be parked and replayed whenever it suits."""
    issued = time.time_ns() + 60 * NS
    frame, sig = _signed(signing_key, seq=1, issued_at_ns=issued)
    with pytest.raises(Stale):
        keyring.verify(frame, sig)


def test_freshness_is_checked_before_the_signature(keyring, signing_key):
    """A stale frame must be cheap to refuse. Ed25519 verify is ~50-100 us, so making a
    flood of stale garbage pay for it is a denial-of-service the attacker gets for free."""
    frame = pack_frame(key_id=CONSOLE, device_id=ARM, target="tcp_z", value=0.93,
                       seq=1, issued_at_ns=time.time_ns() - 60 * NS)
    junk = b"\x00" * 64                      # would raise BadSignature if reached
    with pytest.raises(Stale):
        keyring.verify(frame, junk)


# --------------------------------------------------------------------------------------
# The headline: authenticated is not authorised
# --------------------------------------------------------------------------------------


def test_signed_but_unsafe_is_still_refused(keyring, signing_key):
    """A genuinely signed command that leaves the envelope is refused anyway.

    Run against the REAL evaluator and the REAL capability tag, not a stub. If signing
    ever became a way past a bound, the project would have built an authenticated route
    to breaking the machine.
    """
    import json

    from open_mhs.server import safety
    from open_mhs.server.errors import SafetyLimitViolation
    from open_mhs.server.models import CapabilityTag

    tag_path = REPO_ROOT / "examples" / "robosuite_demo" / "panda_arm.mhs"
    tag = CapabilityTag.model_validate(json.loads(tag_path.read_text(encoding="utf-8")))
    limit = tag.limit_map["tcp_x"]
    assert limit.on_violation == "reject", "this test needs a rejecting axis"

    outside = limit.max + 0.5
    fields = keyring.verify(*_signed(signing_key, target="tcp_x", value=outside, seq=1))
    assert fields["value_num"][0] == pytest.approx(outside)   # authentic...

    with pytest.raises(SafetyLimitViolation):                 # ...and still refused
        safety.check_write(tag.actuator_map["tcp_x"], limit, fields["value_num"][0],
                           device_id=ARM)


# --------------------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------------------


def _demo() -> int:
    """`python tests/test_crypto_bridge.py` -- the flow, narrated."""
    green, red, grey, bold, reset = (
        "\033[92m", "\033[91m", "\033[90m", "\033[1m", "\033[0m")

    def line(ok: bool, label: str, detail: str) -> None:
        mark = f"{green}PASS{reset}" if ok else f"{red}FAIL{reset}"
        print(f"  [{mark}] {label:<44} {grey}{detail}{reset}")

    print(f"\n{bold}Open-MHS management plane - Ed25519 bridge{reset}")
    print(f"{grey}  frame {FRAME.size} bytes | domain {DOMAIN.decode()} "
          f"| skew {MAX_SKEW_NS // NS}s{reset}\n")

    console = SigningKey(b"\x11" * 32)
    ring = KeyRing()
    ring.add(CONSOLE, console.verify_key)
    print(f"{grey}  provisioned public key for {CONSOLE!r} "
          f"({console.verify_key.encode().hex()[:16]}...){reset}")
    print(f"{grey}  private half never leaves the signer{reset}\n")

    failures = 0

    frame, sig = _signed(console, target="tcp_z", value=0.93, seq=1)
    fields = ring.verify(frame, sig)
    line(True, "valid command accepted",
         f"{fields['target']}={fields['value_num'][0]} seq={fields['seq']}")

    try:
        ring.verify(frame, sig)
        line(False, "REPLAY WAS ACCEPTED", "identical bytes, genuine signature")
        failures += 1
    except Replay as exc:
        line(True, "replay rejected", str(exc))

    attacker = SigningKey(b"\x99" * 32)
    hostile = pack_frame(key_id=CONSOLE, device_id=ARM, target="tcp_z", value=0.50,
                         seq=999_999, issued_at_ns=time.time_ns())
    try:
        ring.verify(hostile, attacker.sign(hostile).signature)
        line(False, "FORGED SIGNATURE ACCEPTED", "")
        failures += 1
    except BadSignature as exc:
        line(True, "forged signature rejected", str(exc))
    line(ring.last_seq(CONSOLE) == 1, "forgery did not burn sequence numbers",
         f"last_seq still {ring.last_seq(CONSOLE)}")

    stale = pack_frame(key_id=CONSOLE, device_id=ARM, target="tcp_z", value=0.93,
                       seq=2, issued_at_ns=time.time_ns() - 5 * NS)
    try:
        ring.verify(stale, console.sign(stale).signature)
        line(False, "STALE FRAME ACCEPTED", "")
        failures += 1
    except Stale as exc:
        line(True, "stale frame rejected", str(exc))

    now = time.time_ns()
    honest, honest_sig = _signed(console, target="gripper_state", value="closed",
                                 confirm=False, seq=2, issued_at_ns=now)
    escalated = pack_frame(key_id=CONSOLE, device_id=ARM, target="gripper_state",
                           value="closed", confirm=True, seq=2, issued_at_ns=now)
    try:
        ring.verify(escalated, honest_sig)
        line(False, "APPROVAL FLAG FORGED", "confirm flipped without the key")
        failures += 1
    except BadSignature as exc:
        line(True, "forged human-approval flag rejected", str(exc))
    del honest

    # The one that matters.
    import json

    from open_mhs.server import safety
    from open_mhs.server.errors import SafetyLimitViolation
    from open_mhs.server.models import CapabilityTag

    tag_path = REPO_ROOT / "examples" / "robosuite_demo" / "panda_arm.mhs"
    tag = CapabilityTag.model_validate(json.loads(tag_path.read_text(encoding="utf-8")))
    limit = tag.limit_map["tcp_x"]
    outside = limit.max + 0.5
    fields = ring.verify(*_signed(console, target="tcp_x", value=outside, seq=3))
    try:
        safety.check_write(tag.actuator_map["tcp_x"], limit, fields["value_num"][0],
                           device_id=ARM)
        line(False, "SIGNED-BUT-UNSAFE COMMAND ADMITTED", "envelope did not hold")
        failures += 1
    except SafetyLimitViolation as exc:
        line(True, "signed but unsafe -> still refused", str(exc)[:60])

    print(f"\n{grey}  authenticated is not authorised: the envelope is evaluated after"
          f" the signature, never instead of it.{reset}")
    print(f"\n{bold}{'all checks held' if not failures else f'{failures} FAILED'}"
          f"{reset}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_demo())
