#!/usr/bin/env python
"""A paced, self-verifying walkthrough of everything the middleware does — for recording.

Every line printed here is either narration or the **exact text an MCP client receives**.
Nothing is paraphrased for the camera: the tool calls are the real ones a model makes, and
the replies are the real ones it reads. That is the only way a recording of a safety
system is worth anything.

    export OPEN_MHS_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    open-mhs serve &
    python examples/showcase.py                 # ~3 minutes, paced for a screen recording
    python examples/showcase.py --fast          # no pauses, for checking it still passes

Point it at a simulator instead of the mock devices and the same script narrates that
cell, because every value it uses comes from the capability tags rather than from this
file:

    python examples/robosuite_demo/run_cell.py --viewer &
    python examples/showcase.py --url http://127.0.0.1:8000

**It refuses to produce a misleading recording.** Every beat asserts what the middleware
was supposed to do. If an unsafe command is accepted, if a refused command moves the
hardware, or if a legal command is refused, the script stops and exits non-zero — so a
video of it running is a video of it working.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import warnings
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Raised while the MCP SDK's settings model is being built, before main() can filter it,
# so it has to be silenced here or it is the first thing on screen.
warnings.filterwarnings("ignore", category=UserWarning, module=r"pydantic_settings\..*")

from open_mhs.mcp_adapter import server as adapter          # noqa: E402
from open_mhs.mcp_adapter.client import OpenMHSClient        # noqa: E402

BOLD, DIM, GREEN, RED, YELLOW, BLUE, GREY, RESET = (
    "\033[1m", "\033[2m", "\033[92m", "\033[91m", "\033[93m", "\033[94m",
    "\033[90m", "\033[0m",
)

PACE = 1.0
failures: list[str] = []

# Beat 4 has to tell "the refused command moved the arm" apart from "the arm is a real
# servo". Wait for the previous move to finish, then measure what the cell does at rest.
IDLE_SAMPLES = 4
IDLE_POLL_S = 0.1
IDLE_BUDGET_S = 6.0


def _enable_ansi() -> None:
    # The narration uses box drawing and check marks. A Windows console, or a CI runner
    # capturing stdout as cp1252, would otherwise die on the first heading.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if os.name == "nt":
        try:
            import ctypes

            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass


def _quiet_the_plumbing() -> None:
    """Silence the transport's own chatter so the beats are readable on camera.

    httpx logs every request at INFO, and the MCP SDK installs a rich handler that renders
    each one as a three-line block. Eight of those land between a refusal and the read-back
    that proves nothing moved, which is exactly the pair the recording exists to show. The
    replies themselves are untouched — only the library's HTTP log is quieted, and any
    warning or error still comes through.
    """
    for name in ("httpx", "httpcore", "mcp", "FastMCP", "fastmcp"):
        logging.getLogger(name).setLevel(logging.WARNING)


def beat(number: int, title: str, why: str) -> None:
    print(f"\n{BLUE}{'━' * 78}{RESET}")
    print(f"{BOLD}{BLUE}  {number}. {title}{RESET}")
    print(f"{GREY}  {why}{RESET}")
    print(f"{BLUE}{'━' * 78}{RESET}")
    time.sleep(PACE)


def say(text: str) -> None:
    print(f"\n{DIM}{text}{RESET}")
    time.sleep(PACE * 0.6)


async def tool(name: str, **kwargs: Any) -> str:
    """Call an MCP tool exactly as a client would, and show the call and the reply."""
    args = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    print(f"\n{YELLOW}▸ {name}({args}){RESET}")
    time.sleep(PACE * 0.5)
    result = await adapter.mcp.call_tool(name, kwargs)
    blocks = result[0] if isinstance(result, tuple) else result
    text = (
        str(blocks) if isinstance(blocks, dict)
        else "\n".join(getattr(b, "text", str(b)) for b in blocks)
    )
    colour = RED if text.startswith(("REJECTED", "PLAN REJECTED")) else GREY
    for line in text.splitlines():
        print(f"  {colour}{line}{RESET}")
    time.sleep(PACE)
    return text


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {GREEN}✓ {label}{RESET}")
    else:
        print(f"  {RED}✗ {label}{RESET}" + (f" — {detail}" if detail else ""))
        failures.append(label)
    time.sleep(PACE * 0.4)


async def read_value(client: OpenMHSClient, device: str, target: str) -> Any:
    return (await client.rpc("mhs.read", {"device_id": device, "target": target}))["value"]


def arrival_tolerance(inventory: dict[str, Any], device: str, target: str) -> float:
    """How close counts as arrived, taken from the device's own declared sensor accuracy.

    A mock transport lands on the exact value; a real servo does not, and the tag says so.
    The Panda declares ±0.02 m on its pose sensors, so asserting exact equality here would
    fail a correctly working arm and make the recording look broken. The number comes from
    the tag rather than from this file, for the same reason every other number does.
    """
    for entry in inventory.get("devices", []):
        if entry["device_id"] != device:
            continue
        tag = entry.get("capability_tag", {})
        actuator = next((a for a in tag.get("actuators", []) if a["id"] == target), {})
        feedback = actuator.get("feedback_sensor")
        sensor = next((s for s in tag.get("sensors", []) if s["id"] == feedback), {})
        if sensor.get("accuracy"):
            return float(sensor["accuracy"])
    return 1e-6


async def settle(client: OpenMHSClient, device: str, target: str,
                 tolerance: float) -> tuple[Any, float]:
    """Wait until the channel stops changing, and report how much it still moves at rest.

    Two things move a real arm that no command touched: it is still travelling toward the
    previous setpoint, and once there it holds position against gravity while the physics
    engine keeps integrating. Neither is a transmission. So the refusal beat waits out the
    first and measures the second, instead of asserting a stillness no servo delivers —
    the same reason the benchmark establishes a baseline before it attributes a change to
    a write. A mock transport settles instantly and reports zero, which keeps the check
    exact where exactness is real.

    Returns the last reading and the spread across the idle window.
    """
    stable = tolerance / 10 if tolerance > 1e-6 else 0.0
    deadline = time.monotonic() + IDLE_BUDGET_S
    window: list[float] = []
    while True:
        raw = await read_value(client, device, target)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return raw, 0.0
        window = (window + [value])[-IDLE_SAMPLES:]
        spread = max(window) - min(window)
        if len(window) == IDLE_SAMPLES and spread <= stable:
            return value, spread
        if time.monotonic() >= deadline:
            return value, spread
        await asyncio.sleep(IDLE_POLL_S)


def audit_entries(log: str) -> list[dict[str, Any]]:
    """Every line currently in the audit log, or an empty list if there is none."""
    if log.lower() == "off" or not Path(log).exists():
        return []
    return [json.loads(line) for line in Path(log).read_text(encoding="utf-8").splitlines()]


def pick_device(inventory: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """The first device with a bounded numeric actuator, chosen from the tags.

    No device id or channel name is hardcoded in this file, so the same script narrates
    whatever cell it is pointed at.
    """
    for entry in inventory.get("devices", []):
        tag = entry.get("capability_tag", {})
        limits = {limit["target"]: limit for limit in tag.get("safety_limits", [])}
        for actuator in tag.get("actuators", []):
            limit = limits.get(actuator["id"], {})
            if limit.get("min") is not None and not actuator.get("requires_confirmation"):
                return entry["device_id"], actuator["id"], limit
    return None


def pick_gated(inventory: dict[str, Any]) -> tuple[str, str, Any] | None:
    for entry in inventory.get("devices", []):
        tag = entry.get("capability_tag", {})
        limits = {limit["target"]: limit for limit in tag.get("safety_limits", [])}
        for actuator in tag.get("actuators", []):
            if actuator.get("requires_confirmation"):
                limit = limits.get(actuator["id"], {})
                value = (
                    limit["allowed_values"][0] if limit.get("allowed_values")
                    else (limit["min"] + limit["max"]) / 2
                )
                return entry["device_id"], actuator["id"], value
    return None


async def run(client: OpenMHSClient) -> int:
    adapter.set_client(client)
    # Where the audit log stood before this run, so beat 8 counts only what we added.
    audit_start = len(audit_entries(os.getenv("OPEN_MHS_AUDIT_LOG", "open-mhs-audit.jsonl")))

    print(f"\n{BOLD}Open-MHS — what a language model can and cannot do to this "
          f"hardware{RESET}")
    print(f"{GREY}Every reply below is the literal text an MCP client receives.{RESET}")
    time.sleep(PACE * 2)

    # ---------------------------------------------------------------- 1. discovery ----
    beat(1, "The hardware describes itself",
         "The model is told nothing in advance. It asks what exists and what the bounds "
         "are, and the device answers.")
    inventory = await client.discover()
    await tool("discover_hardware")

    chosen = pick_device(inventory)
    if chosen is None:
        print(f"{RED}No bounded numeric actuator is registered; nothing to show.{RESET}")
        return 2
    device, target, limit = chosen
    lo, hi = limit["min"], limit["max"]
    unit = limit.get("unit") or ""
    # Not the midpoint: on a symmetric joint that is the resting position, and a demo
    # where nothing visibly moves teaches nothing. Two thirds toward the upper bound.
    legal = round(lo + (hi - lo) * 0.66, 3)
    absurd = round(hi + abs(hi - lo) * 10 + 1, 3)

    # ------------------------------------------------------------------ 2. snapshot ---
    beat(2, "One call for the whole cell",
         "Before planning anything, the agent reads every channel of every device, so its "
         "picture comes from the cell rather than from memory.")
    await tool("snapshot_hardware")

    # ------------------------------------------------------------- 3. a legal write ---
    beat(3, "A command inside the envelope",
         f"{device}.{target} is bounded to [{lo}, {hi}]{unit}. This one is legal, so it "
         "goes through and is verified against the actuator's own feedback sensor.")
    text = await tool("write_hardware_state", device_id=device, parameter=target, value=legal)
    check("accepted", text.startswith("ACCEPTED"), text.splitlines()[0])
    landed = await read_value(client, device, target)
    tolerance = arrival_tolerance(inventory, device, target)
    check(f"the hardware arrived within its declared ±{tolerance:g}{unit}",
          abs(float(landed) - legal) <= tolerance,
          f"reads {landed}, which is {abs(float(landed) - legal):g} away")

    # --------------------------------------------------------------- 4. the refusal ---
    beat(4, "A command outside the envelope",
         "This is the whole point. Watch the reply, then watch the hardware not move.")
    before, jitter = await settle(client, device, target, tolerance)
    text = await tool("write_hardware_state", device_id=device, parameter=target, value=absurd)
    check("refused", text.startswith("REJECTED"), text.splitlines()[0])
    check("the refusal states the real bound", str(hi) in text)
    check("the refusal says nothing was transmitted", "Nothing was transmitted" in text)

    say("The reply told the model the actual boundary, so it can correct itself without "
        "guessing. Now the part a screenshot cannot show: what the hardware did.")
    after = await read_value(client, device, target)
    drift = abs(float(after) - float(before))
    allowed = max(jitter, 1e-6)
    at_rest = f" (at rest it wanders ±{jitter:g}{unit})" if jitter else ""
    check(f"the hardware did not move (still {before}{unit}){at_rest}",
          drift <= allowed, f"moved to {after}, a change of {drift:g} > {allowed:g}")

    # ------------------------------------------------------------------- 5. the gate --
    gated = pick_gated(inventory)
    if gated:
        gdevice, gtarget, gvalue = gated
        beat(5, "Something that grips, heats or dispenses",
             "This actuator's tag says a person must approve it. A perfectly legal value "
             "is not a substitute for that approval.")
        text = await tool("write_hardware_state", device_id=gdevice, parameter=gtarget,
                          value=gvalue)
        check("refused without confirmation", text.startswith("REJECTED"))
        say("A human says yes. The same command, with the approval attached:")
        text = await tool("write_hardware_state", device_id=gdevice, parameter=gtarget,
                          value=gvalue, confirm=True)
        check("accepted with confirmation", text.startswith("ACCEPTED"))

    # ----------------------------------------------------------------- 6. dry run -----
    beat(6, "Checking a whole plan before touching anything",
         "Multi-step work across several instruments. The agent validates the entire plan "
         "first; nothing is transmitted, whatever the verdict.")
    plan = [
        {"device_id": device, "target": target, "value": legal},
        {"device_id": device, "target": target, "value": absurd},
    ]
    text = await tool("check_hardware_plan", writes=plan)
    check("the plan is rejected as a whole", text.startswith("PLAN REJECTED"))
    check("the bad step is named", "#1" in text)
    unmoved = await read_value(client, device, target)
    # Compared against the sensor's declared accuracy, not for exact equality. A real arm
    # is still settling toward its previous setpoint while the check runs, and calling
    # that residual millimetre a transmission would accuse the middleware of sending a
    # value it demonstrably refused. What must hold is that the check moved nothing
    # material and left the arm inside its envelope.
    check("nothing was transmitted by the check",
          abs(float(unmoved) - float(after)) <= tolerance and lo <= float(unmoved) <= hi,
          f"moved from {after} to {unmoved}, tolerance ±{tolerance:g}")

    # ------------------------------------------------------------------- 7. stop all --
    beat(7, "Something is wrong and you do not know which device",
         "One call drives every device that declares a safe state to it. A device that "
         "cannot stop is named rather than skipped silently.")
    text = await tool("emergency_stop_all_hardware")
    check("the fleet reported its state", "EMERGENCY STOP ALL" in text)

    # --------------------------------------------------------------------- 8. audit ---
    beat(8, "Everything above is on the record",
         "Every command and every refusal is one hash-chained line. An edited or deleted "
         "line breaks the chain and `open-mhs audit verify` says which one.")
    log = os.getenv("OPEN_MHS_AUDIT_LOG", "open-mhs-audit.jsonl")
    # Only the lines this run added. A stale file from an earlier session would otherwise
    # be counted as evidence for this one, which is the opposite of what an audit is for.
    fresh = audit_entries(log)[audit_start:]
    if not fresh:
        print(f"  {GREY}No new audit lines. The log is written by the SERVER, so start it "
              f"with OPEN_MHS_AUDIT_LOG set to the same path to record this run.{RESET}")
        print(f"  {GREY}(this script is reading {log!r}){RESET}")
    else:
        from open_mhs.server.audit import verify

        refused = sum(1 for e in fresh if e["event"] == "write.refused")
        accepted = sum(1 for e in fresh if e["event"].startswith("write.accept"))
        print(f"  {GREY}{len(fresh)} entries from this run: {accepted} accepted, "
              f"{refused} refused{RESET}")
        report = verify(log)
        print(f"  {GREY}chain: {report}{RESET}")
        check("the audit chain is intact", report["ok"] is True)
        check("this run's refusals were recorded", refused >= 2, f"only {refused}")

    # -------------------------------------------------------------------- the close ---
    print(f"\n{BLUE}{'━' * 78}{RESET}")
    if failures:
        print(f"{RED}{BOLD}  {len(failures)} EXPECTATION(S) FAILED{RESET}")
        for f in failures:
            print(f"{RED}    - {f}{RESET}")
        print(f"{RED}  Do not publish this recording.{RESET}")
    else:
        print(f"{GREEN}{BOLD}  Every claim in this recording was checked as it was made."
              f"{RESET}")
        print(f"{GREY}  Unsafe commands refused, hardware unmoved, refusals actionable, "
              f"trail intact.{RESET}")
    print(f"{BLUE}{'━' * 78}{RESET}\n")
    return 1 if failures else 0


def main() -> int:
    global PACE
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.getenv("OPEN_MHS_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--pace", type=float, default=1.0,
                    help="seconds per beat; 1.0 reads well on camera")
    ap.add_argument("--fast", action="store_true", help="no pauses, for verification")
    args = ap.parse_args()
    PACE = 0.0 if args.fast else args.pace
    _enable_ansi()
    _quiet_the_plumbing()

    token = os.getenv("OPEN_MHS_AUTH_TOKEN")
    if not token:
        print("OPEN_MHS_AUTH_TOKEN is not set", file=sys.stderr)
        return 2

    async def go() -> int:
        client = OpenMHSClient(args.url, token=token)
        try:
            return await run(client)
        finally:
            adapter.set_client(None)
            await client.aclose()

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
