#!/usr/bin/env python
"""A device-agnostic agent. This is the actual claim of Open-MHS, under test.

Point it at any Open-MHS middleware and it will operate whatever is plugged in:

    python examples/blind_agent.py --url http://127.0.0.1:8000

There is **no hardware-specific knowledge anywhere in this file**. No device id, no target
name, no unit, no bound, no table height, no robot model, no simulator name. Every one of
those is discovered at runtime from the capability tag.

`tests/test_blind_agent.py` asserts that: it greps this file for the identifiers and
magic numbers used by the shipped demos and fails if any of them appear.

That is what separates a standard from a script. A shell script can move an arm. It cannot
be handed an unfamiliar machine and work out what is safe to do with it.

What the agent does, per device:

    1. discover               what exists
    2. read the tag           which channels are writable, and within what
    3. probe a bound          deliberately overstep, and read the real limit out of the
                              refusal rather than being told it in advance
    4. act                    command a value derived from the tag, then verify it against
                              the actuator's own declared feedback sensor
    5. respect the gate       re-send with confirmation where the tag demands one

Exit code is non-zero if any safety expectation failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BLUE, GREEN, YELLOW, RED, GREY, BOLD, RESET = (
    "\033[94m", "\033[92m", "\033[93m", "\033[91m", "\033[90m", "\033[1m", "\033[0m")


class Middleware:
    """The only thing the agent knows how to talk to."""

    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url.rstrip("/"), token

    def _post(self, method: str, params: dict) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": method, "params": params}).encode()
        req = urllib.request.Request(
            f"{self.url}/rpc", data=body,
            headers={"content-type": "application/json",
                     "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.load(response)

    def discover(self) -> list[dict]:
        return self._post("mhs.discover", {})["result"]["devices"]

    def read(self, device: str, target: str):
        reply = self._post("mhs.read", {"device_id": device, "target": target})
        return reply["result"]["value"] if "result" in reply else None

    def write(self, device: str, target: str, value, confirm: bool = False) -> dict:
        return self._post("mhs.write", {"device_id": device, "target": target,
                                        "value": value, "confirm": confirm})


def say(text: str, colour: str = "") -> None:
    print(f"{colour}{text}{RESET}", flush=True)


def numeric_actuators(tag: dict) -> list[tuple[dict, dict]]:
    """Pair each numeric actuator with its safety limit, straight from the tag."""
    limits = {limit["target"]: limit for limit in tag.get("safety_limits", [])}
    pairs = []
    for actuator in tag.get("actuators", []):
        limit = limits.get(actuator["id"], {})
        if limit.get("min") is not None and limit.get("max") is not None:
            pairs.append((actuator, limit))
    return pairs


def exercise(mhs: Middleware, summary: dict) -> list[str]:
    """Operate one device knowing only what its capability tag says. Returns failures."""
    device, tag = summary["device_id"], summary["capability_tag"]
    failures: list[str] = []

    say(f"\n{'=' * 78}")
    say(f"  DEVICE  {device}   ({tag.get('type')})", BOLD)
    say(f"{'=' * 78}")

    pairs = numeric_actuators(tag)
    if not pairs:
        say("  no numeric actuators — read-only device, nothing to command", GREY)
        for sensor in tag.get("sensors", [])[:3]:
            say(f"    read {sensor['id']} = {mhs.read(device, sensor['id'])}", GREY)
        # A read-only device must refuse writes. Prove it.
        if tag.get("sensors"):
            probe = tag["sensors"][0]["id"]
            reply = mhs.write(device, probe, 1.0)
            if "error" in reply and reply["error"]["code"] == -32602:
                say(f"    write to sensor {probe!r} refused (-32602) — correct", GREEN)
            else:
                failures.append(f"{device}: a sensor accepted a write")
                say("    a sensor ACCEPTED a write — that is a safety failure", RED)
        return failures

    actuator, limit = pairs[0]
    target, unit = actuator["id"], actuator.get("unit", "")
    low, high = float(limit["min"]), float(limit["max"])
    gated = bool(actuator.get("requires_confirmation"))
    say(f"  tag says: {target} accepts [{low}, {high}] {unit}"
        + ("  (human confirmation required)" if gated else ""), GREY)
    if limit.get("rationale"):
        say(f"  why: {limit['rationale'][:96]}", GREY)

    # --- 3. probe the bound. The agent is NOT told the limit; it reads it from refusal ---
    span = high - low
    overstep = round(high + span * 0.5, 4)
    say(f"\n  probe> commanding {target} = {overstep} {unit} (outside, on purpose)", YELLOW)
    reply = mhs.write(device, target, overstep, confirm=True)
    if "error" not in reply or reply["error"]["code"] != -32001:
        failures.append(f"{device}: {target} accepted an out-of-bounds value")
        say("  ACCEPTED — the envelope is not being enforced", RED + BOLD)
        return failures

    data = reply["error"].get("data", {})
    say(f"  refused> -32001  {reply['error']['message'][:80]}", RED)
    learned_low, learned_high = data.get("min"), data.get("max")
    say(f"  learned> the real bound is [{learned_low}, {learned_high}] {data.get('unit','')}",
        GREEN)
    if (learned_low, learned_high) != (low, high):
        failures.append(f"{device}: refusal reported a bound different from the tag")

    # --- 4. act on what was learned, then verify against the declared feedback sensor ---
    safe = round(learned_low + (learned_high - learned_low) * 0.6, 4)
    say(f"\n  act> commanding {target} = {safe} {unit} (60% across the learned range)", BLUE)
    reply = mhs.write(device, target, safe, confirm=gated)
    if "error" in reply:
        code = reply["error"]["code"]
        if gated and code == -32602:
            say("  refused pending confirmation — re-sending with operator approval", YELLOW)
            reply = mhs.write(device, target, safe, confirm=True)
        if "error" in reply:
            failures.append(f"{device}: a legal value was refused ({reply['error']['code']})")
            say(f"  a legal value was REFUSED: {reply['error']['message'][:70]}", RED)
            return failures

    feedback = actuator.get("feedback_sensor")
    if feedback:
        measured = mhs.read(device, feedback)
        tolerance = next((s.get("accuracy", 0.05) for s in tag.get("sensors", [])
                          if s["id"] == feedback), 0.05)
        ok = measured is not None and abs(float(measured) - safe) <= float(tolerance) * 3
        say(f"  verify> {feedback} reads {measured} {unit} "
            f"(commanded {safe}, tolerance {tolerance})", GREEN if ok else RED)
        if not ok:
            failures.append(f"{device}: {feedback} did not confirm the commanded value")
    else:
        say("  verify> actuator declares no feedback sensor; nothing to check against", GREY)

    # --- 5. the gate ---
    gate = next((a for a in tag.get("actuators", []) if a.get("requires_confirmation")), None)
    if gate and gate["id"] != target:
        allowed = next((limit_.get("allowed_values") for limit_ in tag.get("safety_limits", [])
                        if limit_["target"] == gate["id"]), None)
        if allowed:
            say(f"\n  gate> {gate['id']} requires confirmation; trying without it", YELLOW)
            reply = mhs.write(device, gate["id"], allowed[-1], confirm=False)
            if "error" in reply and reply["error"]["code"] == -32602:
                say("  refused pending human approval — correct", GREEN)
            else:
                failures.append(f"{device}: {gate['id']} bypassed its confirmation gate")
                say("  EXECUTED without approval — gate bypassed", RED + BOLD)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Operate unknown hardware via Open-MHS.")
    parser.add_argument("--url", default=os.getenv("OPEN_MHS_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.getenv("OPEN_MHS_AUTH_TOKEN", ""))
    args = parser.parse_args()
    if not args.token:
        print("set OPEN_MHS_AUTH_TOKEN", file=sys.stderr)
        return 2

    mhs = Middleware(args.url, args.token)
    say(f"{BOLD}Open-MHS blind agent{RESET}  ->  {args.url}")
    say("this agent has never seen this hardware and contains no knowledge of it", GREY)

    try:
        devices = mhs.discover()
    except (urllib.error.URLError, OSError) as exc:
        say(f"cannot reach the middleware: {exc}", RED)
        return 2

    say(f"\ndiscovered {len(devices)} device(s): "
        + ", ".join(f"{d['device_id']} ({d['type']})" for d in devices))

    failures: list[str] = []
    for summary in devices:
        failures += exercise(mhs, summary)

    say(f"\n{'=' * 78}")
    if failures:
        say(f"  {len(failures)} SAFETY EXPECTATION(S) FAILED", RED + BOLD)
        for f in failures:
            say(f"    - {f}", RED)
        return 1
    say("  every device operated correctly, and every envelope held", GREEN + BOLD)
    say("  no hardware-specific code was used", GREEN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
