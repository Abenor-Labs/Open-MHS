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
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from open_mhs.mcp_adapter import server as adapter          # noqa: E402
from open_mhs.mcp_adapter.client import OpenMHSClient        # noqa: E402

BOLD, DIM, GREEN, RED, YELLOW, BLUE, GREY, RESET = (
    "\033[1m", "\033[2m", "\033[92m", "\033[91m", "\033[93m", "\033[94m",
    "\033[90m", "\033[0m",
)

PACE = 1.0
failures: list[str] = []


def _enable_ansi() -> None:
    if os.name == "nt":
        try:
            import ctypes

            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:  # noqa: BLE001 - colour is cosmetic
            pass


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
    check(f"the hardware is at {legal}{unit}", abs(float(landed) - legal) < 1e-6,
          f"reads {landed}")

    # --------------------------------------------------------------- 4. the refusal ---
    beat(4, "A command outside the envelope",
         "This is the whole point. Watch the reply, then watch the hardware not move.")
    before = await read_value(client, device, target)
    text = await tool("write_hardware_state", device_id=device, parameter=target, value=absurd)
    check("refused", text.startswith("REJECTED"), text.splitlines()[0])
    check("the refusal states the real bound", str(hi) in text)
    check("the refusal says nothing was transmitted", "Nothing was transmitted" in text)

    say("The reply told the model the actual boundary, so it can correct itself without "
        "guessing. Now the part a screenshot cannot show: what the hardware did.")
    after = await read_value(client, device, target)
    check(f"the hardware did not move (still {before}{unit})",
          abs(float(after) - float(before)) < 1e-6, f"moved to {after}")

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
    check("nothing was transmitted by the check",
          abs(float(unmoved) - float(after)) < 1e-6, f"moved to {unmoved}")

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
    if log.lower() != "off" and Path(log).exists():
        from open_mhs.server.audit import verify

        lines = [json.loads(line) for line in Path(log).read_text().splitlines()]
        refused = sum(1 for entry in lines if entry["event"] == "write.refused")
        accepted = sum(1 for entry in lines if entry["event"].startswith("write.accept"))
        print(f"  {GREY}{len(lines)} entries: {accepted} accepted, {refused} refused{RESET}")
        report = verify(log)
        print(f"  {GREY}chain: {report}{RESET}")
        check("the audit chain is intact", report["ok"] is True)
        check("the refusals were recorded", refused >= 2, f"only {refused}")
    else:
        print(f"  {GREY}OPEN_MHS_AUDIT_LOG is off; run with it set to see the trail."
              f"{RESET}")

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
