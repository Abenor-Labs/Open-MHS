"""Render a benchmark run as something a person can read and publish.

Two audiences. Somebody deciding whether to trust this near a machine reads the headline
and the failures. Somebody comparing implementations reads the full table. Both need the
same thing from it: no number that the run did not actually produce.
"""

from __future__ import annotations

import json
import time
from typing import Any

from open_mhs.bench.runner import Result, Run

SEVERITY_MARK = {
    "ok": "pass",
    "critical": "**LEAK**",
    "false-refusal": "false refusal",
    "unexpected": "unexpected",
    "inconclusive": "inconclusive",
}


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, str) and len(value) > 40:
        return value[:37] + "..."
    return str(value)


def summary(run: Run) -> dict[str, Any]:
    """The numbers, computed once, so the prose and the JSON cannot disagree."""
    total = len(run.results)
    refusals = [r for r in run.results if r.attempt.expect == "refused"]
    permits = [r for r in run.results if r.attempt.expect == "accepted"]
    return {
        "attempts": total,
        "devices": run.devices,
        "unsafe_attempts": len(refusals),
        "unsafe_blocked": sum(1 for r in refusals if r.verdict == "refused"),
        "legal_attempts": len(permits),
        "legal_accepted": sum(1 for r in permits if r.verdict in {"accepted", "clamped"}),
        "leaks": len(run.leaks),
        "false_refusals": sum(1 for r in run.results if r.severity == "false-refusal"),
        "unexpected": sum(1 for r in run.results if r.severity == "unexpected"),
        "inconclusive": sum(1 for r in run.results if r.severity == "inconclusive"),
        "passed": sum(1 for r in run.results if r.passed),
        "duration_s": round(run.duration_s, 1),
    }


def _rate(part: int, whole: int) -> str:
    return f"{part}/{whole}" + (f" ({100 * part / whole:.0f}%)" if whole else "")


def _row(r: Result) -> str:
    a = r.attempt
    expected = a.expect + (f" {a.expect_code}" if a.expect_code else "")
    actual = r.verdict + (f" {r.code}" if r.code else "")
    return (
        f"| `{a.id}` | {a.what} | {expected} | {actual} | "
        f"{_fmt(r.observed_before)} → {_fmt(r.observed_after)} | {SEVERITY_MARK[r.severity]} |"
    )


def to_markdown(run: Run) -> str:
    s = summary(run)
    out: list[str] = []
    w = out.append

    w("# Open-MHS safety benchmark")
    w("")
    w(f"Run {time.strftime('%Y-%m-%d %H:%M', time.localtime(run.started))} against "
      f"`{run.url or 'an in-process middleware'}`, {len(run.devices)} device(s): "
      + ", ".join(f"`{d}`" for d in run.devices) + ".")
    w("")
    w("Every attempt is bracketed by a read of the target it aims at, so the question "
      "answered here is not whether an error came back. It is **whether the world "
      "changed**. A refused write whose observed value moved is a leak, and one leak "
      "invalidates the run no matter what the totals say.")
    w("")

    w("## Headline")
    w("")
    w("| | |")
    w("|---|---|")
    w(f"| Unsafe commands blocked | {_rate(s['unsafe_blocked'], s['unsafe_attempts'])} |")
    w(f"| Legal commands accepted | {_rate(s['legal_accepted'], s['legal_attempts'])} |")
    w(f"| **Refusals that still moved the hardware** | **{s['leaks']}** |")
    w(f"| False refusals | {s['false_refusals']} |")
    w(f"| Unexpected outcomes | {s['unexpected']} |")
    w(f"| Inconclusive | {s['inconclusive']} |")
    w(f"| Total attempts | {s['attempts']} in {s['duration_s']} s |")
    w("")

    if s["leaks"]:
        w(f"> **{s['leaks']} refusal(s) moved the hardware.** This is the one outcome the "
          "project exists to prevent. Details below; treat every other number in this "
          "report as unreliable until they are explained.")
    elif s["unexpected"] or s["false_refusals"]:
        w("> No refusal moved the hardware. Some outcomes did not match what the tag "
          "declared; those are listed below and are worth reading before relying on this.")
    else:
        w("> No refusal moved the hardware, and every outcome matched what the tag "
          "declared.")
    w("")

    w("## By category")
    w("")
    w("| Category | Attempts | Passed | Leaks | What it exercises |")
    w("|---|---|---|---|---|")
    blurbs = {
        "envelope": "the declared bound, at its edges and far outside",
        "type": "values that could never be valid for the datatype",
        "rate": "a step change that is legal at both ends and destructive in between",
        "duration": "an actuator held away from its default past `max_duration_s`",
        "discrete": "states the hardware supports against states policy permits",
        "confirmation": "the human-approval gate",
        "surface": "sensors, unknown channels, unknown devices",
        "estop": "driving to the declared safe state",
    }
    for category, results in run.by_category().items():
        leaks = sum(1 for r in results if r.leaked)
        w(f"| {category} | {len(results)} | {sum(1 for r in results if r.passed)} | "
          f"{leaks} | {blurbs.get(category, '')} |")
    w("")

    problems = [r for r in run.results if not r.passed]
    if problems:
        w("## Everything that did not go as declared")
        w("")
        for r in sorted(problems, key=lambda r: (r.severity != "critical", r.attempt.id)):
            a = r.attempt
            w(f"### `{a.id}` — {SEVERITY_MARK[r.severity]}")
            w("")
            w(f"**Tried:** {a.what} on `{a.label}`.")
            w("")
            w(f"**Expected** {a.expect}"
              + (f" with code {a.expect_code}" if a.expect_code else "")
              + f". **Got** {r.verdict}"
              + (f" with code {r.code}" if r.code else "") + ".")
            if r.message:
                w("")
                w(f"> {r.message}")
            if r.landed_outside:
                w("")
                w(f"**The clamp corrected to {_fmt(r.transmitted)}, which is still "
                  f"outside {r.attempt.expect_within}.** A clamp that lands illegally is "
                  "worse than a refusal, because it is reported as a success.")
            if r.world_changed:
                w("")
                w(f"**The target moved toward the commanded value: "
                  f"{_fmt(r.observed_before)} → {_fmt(r.observed_after)}"
                  + (f", against a measured jitter of {r.jitter:g}" if r.jitter else "")
                  + ".**")
            w("")
            w(f"**Why this matters:** {a.why}")
            w("")

    w("## Every attempt")
    w("")
    w("| id | attempt | expected | actual | observed | |")
    w("|---|---|---|---|---|---|")
    for r in run.results:
        w(_row(r))
    w("")

    w("## What this does not measure")
    w("")
    w("- **Real hardware.** Every device here is simulated. A simulated transport cannot "
      "stick, overshoot, or lie in the ways a real one does.")
    w("- **Whether the declared bounds are the right bounds.** The benchmark checks that "
      "the middleware enforces what a tag says, not that the tag is true.")
    w("- **Whether an agent obeys a refusal.** The refusals are measured; what a model "
      "does after reading one is a separate experiment.")
    w("- **Prompt injection through tag text.** Mitigated and unit-tested, but its "
      "effectiveness against an actual model is unmeasured. See `docs/threat-model.md`.")
    w("- **Timing under load.** Attempts run sequentially against an idle middleware.")
    w("")
    return "\n".join(out)


def to_json(run: Run) -> str:
    return json.dumps({
        "summary": summary(run),
        "url": run.url,
        "started": run.started,
        "results": [
            {
                "id": r.attempt.id,
                "category": r.attempt.category,
                "device_id": r.attempt.device_id,
                "target": r.attempt.target,
                "what": r.attempt.what,
                "why": r.attempt.why,
                "expected": r.attempt.expect,
                "expected_code": r.attempt.expect_code,
                "verdict": r.verdict,
                "code": r.code,
                "message": r.message,
                "observed_before": r.observed_before,
                "observed_after": r.observed_after,
                "jitter": r.jitter,
                "world_changed": r.world_changed,
                "leaked": r.leaked,
                "expect_within": list(r.attempt.expect_within) if r.attempt.expect_within else None,
                "landed_outside": r.landed_outside,
                "passed": r.passed,
                "severity": r.severity,
                "note": r.note,
                "elapsed_ms": round(r.elapsed_ms, 2),
            }
            for r in run.results
        ],
    }, indent=2, default=str)


def to_console(run: Run) -> str:
    """A short live summary, for the terminal rather than the report file."""
    s = summary(run)
    lines = [
        f"{s['attempts']} attempts across {len(run.devices)} device(s) in {s['duration_s']} s",
        f"  unsafe blocked   {_rate(s['unsafe_blocked'], s['unsafe_attempts'])}",
        f"  legal accepted   {_rate(s['legal_accepted'], s['legal_attempts'])}",
        f"  LEAKS            {s['leaks']}",
    ]
    for r in run.results:
        if not r.passed:
            lines.append(
                f"  {SEVERITY_MARK[r.severity]:>14}  {r.attempt.id}: expected "
                f"{r.attempt.expect}, got {r.verdict}"
                + (f" ({r.code})" if r.code else "")
            )
    return "\n".join(lines)
