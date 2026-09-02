"""Execute the corpus against a running middleware and record what happened.

The measurement that matters is not "did an error come back". It is **did the world
change**. A refusal that still moved the hardware would pass a return-value assertion and
break a machine, so every attempt is bracketed by a read of the target: snapshot, attempt,
snapshot. A refused write whose observed value moved is recorded as a critical failure
regardless of what the response said.

That check works over plain HTTP against any middleware, including one whose drivers this
process cannot see, which is the point: you can point this at a simulator, at someone
else's implementation, or eventually at real hardware, and get the same table.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from open_mhs.bench.corpus import Attempt, for_cell, for_device
from open_mhs.mcp_adapter.client import OpenMHSClient, RemoteRPCError

#: Verdicts a result can carry, beyond the three an attempt can expect.
SETUP_FAILED = "setup-failed"
ERROR = "error"

#: How much of the distance to the commanded value a move must close before it counts as
#: that command having taken effect. Well under half, because a partial move is still a
#: leak — a driver that clamps and transmits anyway would land short — but far enough
#: above zero that a noisy sensor drifting a few hundredths is not evidence of anything.
MIN_ATTRIBUTABLE_FRACTION = 0.25

#: A channel is treated as at rest once two consecutive reads differ by less than this.
#: Loose enough for a servo's dither, tight enough that real travel keeps it polling.
BASELINE_STABLE_STEP = 1e-3
BASELINE_POLL_S = 0.1


@dataclass
class Result:
    attempt: Attempt
    verdict: str
    code: int | None = None
    message: str = ""
    observed_before: Any = None
    observed_after: Any = None
    transmitted: Any = None
    elapsed_ms: float = 0.0
    note: str = ""

    #: Natural variation of the target, measured by reading it twice before the attempt.
    #: A live thermometer moves on its own; without this the harness blames the
    #: middleware for the weather.
    jitter: float = 0.0

    @property
    def world_changed(self) -> bool:
        """Did the target move, in a way attributable to this attempt?

        Two things are deliberately not counted. An unreadable channel — `None` on either
        side — is not evidence of anything. And a numeric change no larger than the
        channel's own measured jitter, or one that moves *away* from the value that was
        commanded, is drift rather than a command taking effect.

        Getting this wrong in either direction is bad. Too strict and a real leak is
        dismissed as noise; too loose and a thermometer's ordinary wander is reported as a
        safety failure. The discriminator is direction: hardware that obeyed a refused
        command moves toward what was asked for.
        """
        before, after = self.observed_before, self.observed_after
        if before is None or after is None:
            return False
        numeric = (
            isinstance(before, (int, float)) and not isinstance(before, bool)
            and isinstance(after, (int, float)) and not isinstance(after, bool)
        )
        if not numeric:
            return before != after

        moved = abs(float(after) - float(before))
        if moved <= max(self.jitter * 2, 1e-9):
            return False

        commanded = self.attempt.value
        if not isinstance(commanded, (int, float)) or isinstance(commanded, bool):
            # A string or a boolean aimed at a numeric channel has no target position, so
            # movement cannot be attributed to it. On a continuous actuator still coasting
            # toward its previous setpoint, treating any motion as a leak accuses the
            # middleware of transmitting a value it correctly refused as the wrong type.
            return False

        gap = abs(float(commanded) - float(before))
        if gap <= 1e-9:
            return False  # already at the commanded value; nothing to attribute
        closed = (gap - abs(float(commanded) - float(after))) / gap
        # A command that actually landed closes most of the distance to what was asked
        # for. A thermometer wandering a few hundredths happens to close a little of it
        # by chance, and over enough attempts chance will point the right way roughly
        # half the time. Requiring a substantial fraction is what separates the two.
        return closed >= MIN_ATTRIBUTABLE_FRACTION

    @property
    def leaked(self) -> bool:
        """The one unforgivable outcome: refused, and the hardware moved anyway."""
        return self.verdict == "refused" and self.world_changed

    @property
    def landed_outside(self) -> bool:
        """A clamp that corrected to a value the envelope still forbids.

        Worse than a refusal, because it is reported as a success. Only meaningful for an
        attempt whose limit declares `clamp`.
        """
        bounds = self.attempt.expect_within
        if bounds is None or self.verdict != "clamped":
            return False
        landed = self.transmitted
        if not isinstance(landed, (int, float)) or isinstance(landed, bool):
            return False
        low, high = bounds
        return not (low <= float(landed) <= high)

    @property
    def passed(self) -> bool:
        if self.leaked or self.landed_outside or self.verdict in {SETUP_FAILED, ERROR}:
            return False
        if self.verdict != self.attempt.expect:
            # A clamp where a refusal was expected is still a controlled outcome, but it
            # is not what the tag declared, so it does not pass.
            return False
        if self.attempt.expect_code is not None and self.code != self.attempt.expect_code:
            return False
        return True

    @property
    def severity(self) -> str:
        if self.leaked or self.landed_outside:
            return "critical"
        if self.passed:
            return "ok"
        if self.verdict in {SETUP_FAILED, ERROR}:
            return "inconclusive"
        if self.attempt.expect == "accepted":
            return "false-refusal"
        return "unexpected"


@dataclass
class Run:
    results: list[Result] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    url: str = ""
    started: float = field(default_factory=time.time)
    duration_s: float = 0.0

    @property
    def leaks(self) -> list[Result]:
        return [r for r in self.results if r.leaked]

    @property
    def failures(self) -> list[Result]:
        return [r for r in self.results if not r.passed]

    def by_category(self) -> dict[str, list[Result]]:
        out: dict[str, list[Result]] = {}
        for r in self.results:
            out.setdefault(r.attempt.category, []).append(r)
        return dict(sorted(out.items()))


class Bench:
    """Runs attempts against one middleware over its JSON-RPC surface."""

    def __init__(self, client: OpenMHSClient) -> None:
        self.client = client
        self._last_write: dict[tuple[str, str], tuple[Any, float]] = {}
        self._can_estop: dict[str, bool] = {}

    # --- primitives -------------------------------------------------------------------

    async def _read(self, device_id: str, target: str | None) -> Any:
        if not target:
            return None
        try:
            result = await self.client.rpc(
                "mhs.read", {"device_id": device_id, "target": target}
            )
            return result.get("value")
        except (RemoteRPCError, Exception):
            return None

    async def _baseline(
        self, device_id: str, target: str | None, budget_s: float = 4.0
    ) -> tuple[Any, Any, float]:
        """Wait for the channel to stop moving, then measure its residual jitter.

        Returns (first read, settled read, jitter). A non-numeric channel settles by
        definition after one read. A channel that never settles inside the budget reports
        its last observed step as jitter, so the attribution logic widens rather than
        accusing the middleware of motion the previous attempt caused.
        """
        first = await self._read(device_id, target)
        previous = first
        jitter = 0.0
        deadline = time.monotonic() + budget_s
        while True:
            current = await self._read(device_id, target)
            numeric = all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in (previous, current)
            )
            if not numeric:
                return first, current, 0.0
            step = abs(float(current) - float(previous))
            jitter = step
            if step <= BASELINE_STABLE_STEP or time.monotonic() >= deadline:
                return first, current, jitter
            previous = current
            time.sleep(BASELINE_POLL_S)

    async def _pace(self, attempt: Attempt, tag_limits: dict[str, Any]) -> None:
        """Make sure this attempt is measured against the bound it is actually testing.

        Only the `rate` attempts are supposed to trip `max_rate`. For everything else the
        harness must not turn its own speed into a refusal: writing the minimum and then
        the maximum back to back is two legal values and one illegal transition, and
        reporting that as "the declared maximum is unreachable" would be a lie about the
        device.

        Sleeping out the difference works but is slow, and a cap on that sleep silently
        reintroduces the bug. An emergency stop clears the middleware's rate history for
        the device, so the next write is evaluated with no previous value — exactly the
        state a fresh caller would be in — and it takes milliseconds.
        """
        if not attempt.pace or attempt.target is None:
            return
        limit = tag_limits.get(attempt.target) or {}
        rate = limit.get("max_rate")
        if not rate or not isinstance(attempt.value, (int, float)):
            return
        previous = self._last_write.get((attempt.device_id, attempt.target))
        if previous is None:
            return
        value, at = previous
        if not isinstance(value, (int, float)):
            return
        needed = abs(float(attempt.value) - float(value)) / rate
        waited = time.monotonic() - at
        if needed <= waited:
            return
        if self._can_estop.get(attempt.device_id) and needed - waited > 0.5:
            try:
                await self.client.rpc("mhs.emergency_stop", {"device_id": attempt.device_id})
                self._last_write.clear()
                return
            except Exception:
                pass
        time.sleep(needed - waited + 0.02)

    async def _write(self, device_id: str, target: str, value: Any, confirm: bool) -> Any:
        result = await self.client.rpc("mhs.write", {
            "device_id": device_id, "target": target, "value": value, "confirm": confirm,
        })
        self._last_write[(device_id, target)] = (value, time.monotonic())
        return result

    # --- one attempt ------------------------------------------------------------------

    async def run_attempt(self, attempt: Attempt, tag_limits: dict[str, Any]) -> Result:
        for target, value, confirm in attempt.setup:
            try:
                await self._pace(
                    Attempt(id="setup", category="setup", device_id=attempt.device_id,
                            what="", expect="accepted", why="", target=target, value=value),
                    tag_limits,
                )
                await self._write(attempt.device_id, target, value, confirm)
            except RemoteRPCError as exc:
                return Result(
                    attempt=attempt, verdict=SETUP_FAILED, code=exc.code,
                    message=exc.message,
                    note="the state this attempt needed could not be reached, so nothing "
                         "was measured",
                )

        # Pace FIRST. Pacing can emergency-stop the device to clear its rate history,
        # which moves the actuator, and a baseline read before that would attribute the
        # harness's own tidy-up to the attempt.
        if attempt.category != "estop":
            await self._pace(attempt, tag_limits)

        # Then establish a baseline. A real actuator is still coasting toward its previous
        # setpoint for a second or two, and a baseline taken mid-travel makes the next
        # attempt look as though it moved the hardware. Poll until two consecutive reads
        # agree, or give up and record the residual motion as this attempt's jitter, which
        # is the honest fallback for a channel that genuinely never sits still.
        baseline, before, jitter = await self._baseline(attempt.device_id, attempt.target)
        started = time.monotonic()

        try:
            if attempt.category == "estop":
                result = await self.client.rpc(
                    "mhs.emergency_stop", {"device_id": attempt.device_id}
                )
                self._last_write.clear()
            else:
                assert attempt.target is not None
                result = await self._write(
                    attempt.device_id, attempt.target, attempt.value, attempt.confirm
                )
            elapsed = (time.monotonic() - started) * 1000
            if attempt.settle_s:
                time.sleep(attempt.settle_s)
            after = await self._read(attempt.device_id, attempt.target)
            clamped = bool(result.get("clamped")) if isinstance(result, dict) else False
            return Result(
                attempt=attempt,
                verdict="clamped" if clamped else "accepted",
                observed_before=before, observed_after=after, jitter=jitter,
                transmitted=result.get("commanded") if isinstance(result, dict) else None,
                elapsed_ms=elapsed,
                note=self._accepted_note(attempt, result, after),
            )
        except RemoteRPCError as exc:
            elapsed = (time.monotonic() - started) * 1000
            after = await self._read(attempt.device_id, attempt.target)
            return Result(
                attempt=attempt, verdict="refused", code=exc.code, message=exc.message,
                observed_before=before, observed_after=after, jitter=jitter, elapsed_ms=elapsed,
                note=self._refusal_note(exc),
            )
        except Exception as exc:
            after = await self._read(attempt.device_id, attempt.target)
            return Result(
                attempt=attempt, verdict=ERROR, message=f"{type(exc).__name__}: {exc}",
                observed_before=before, observed_after=after, jitter=jitter,
                note="the benchmark itself failed here; the middleware's behaviour is "
                     "unknown for this attempt",
            )

    @staticmethod
    def _accepted_note(attempt: Attempt, result: Any, after: Any) -> str:
        if attempt.category == "duration":
            default_reached = after is not None and (
                not isinstance(after, float) or abs(after - float(attempt.value)) > 1e-9
            )
            return (
                f"after {attempt.settle_s:g} s the actuator reads {after!r}"
                + ("; the dead-man timer returned it" if default_reached
                   else "; it was NOT returned, so max_duration_s did nothing")
            )
        if isinstance(result, dict) and result.get("clamped"):
            return f"clamped from {result.get('requested')!r} to {result.get('commanded')!r}"
        if isinstance(result, dict) and result.get("verified"):
            return f"verified against {result.get('feedback_sensor')}, reads {after!r}"
        return ""

    @staticmethod
    def _refusal_note(exc: RemoteRPCError) -> str:
        """Does the refusal carry what a caller needs in order to correct itself?"""
        data = exc.data or {}
        if "min" in data and "max" in data:
            return f"cites the bound [{data['min']}, {data['max']}]"
        if "allowed_values" in data:
            return f"cites the permitted states {data['allowed_values']}"
        if "known_devices" in data:
            return "names the devices that do exist"
        if data.get("requires_confirmation"):
            return "names the confirmation requirement"
        if "max_rate" in data:
            return f"cites max_rate {data['max_rate']}"
        return "carries no corrective detail" if not data else "carries detail"

    # --- a whole run ------------------------------------------------------------------

    async def run(self, url: str = "") -> Run:
        inventory = await self.client.discover()
        devices = inventory.get("devices", [])
        run = Run(devices=[d["device_id"] for d in devices], url=url)
        started = time.monotonic()

        for entry in devices:
            self._can_estop[entry["device_id"]] = bool(
                (entry.get("capability_tag", {}).get("emergency_stop") or {}).get("supported")
            )

        for entry in devices:
            tag = entry.get("capability_tag", {})
            limits = {limit["target"]: limit for limit in tag.get("safety_limits", [])}
            for attempt in for_device(entry):
                run.results.append(await self.run_attempt(attempt, limits))
            # Leave each device in its declared safe state before moving on, so one
            # device's leftovers cannot explain the next device's results.
            if (tag.get("emergency_stop") or {}).get("supported"):
                try:
                    await self.client.rpc(
                        "mhs.emergency_stop", {"device_id": entry["device_id"]}
                    )
                    self._last_write.clear()
                except Exception:
                    pass

        for attempt in for_cell(devices):
            run.results.append(await self.run_attempt(attempt, {}))

        run.duration_s = time.monotonic() - started
        return run
