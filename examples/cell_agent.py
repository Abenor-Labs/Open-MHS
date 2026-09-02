#!/usr/bin/env python
"""Operate a whole cell through Open-MHS: snapshot, plan, check, execute, verify, stop.

    python examples/cell_agent.py --url http://127.0.0.1:8000

No device id, target, unit or bound appears in this file. Everything is discovered.

    1. snapshot          what is every device doing right now
    2. plan              for every writable numeric channel, pick the midpoint of its bound
                         and one value past its max
    3. check             dry-run the whole plan; expect the overshoots to be refused and
                         nothing to have moved
    4. execute           send only the items the check passed, one by one
    5. snapshot again    confirm the world matches what was commanded
    6. stop all          leave the cell in its declared safe states

Exit code is non-zero if any expectation fails.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any


def rpc(url: str, token: str, method: str, params: dict[str, Any] | None = None) -> Any:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    ).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/rpc",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if "error" in payload:
        raise RuntimeError(json.dumps(payload["error"]))
    return payload["result"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.getenv("OPEN_MHS_URL", "http://127.0.0.1:8000"))
    args = ap.parse_args()
    token = os.getenv("OPEN_MHS_AUTH_TOKEN")
    if not token:
        print("OPEN_MHS_AUTH_TOKEN is not set", file=sys.stderr)
        return 2
    failures = 0

    devices = rpc(args.url, token, "mhs.discover")["devices"]
    print(f"discovered {len(devices)} device(s): {[d['device_id'] for d in devices]}")

    before = rpc(args.url, token, "mhs.snapshot")
    channels = sum(len(d["channels"]) for d in before["devices"].values())
    print(f"snapshot: {channels} channel(s) read across {before['count']} device(s)")

    plan: list[dict[str, Any]] = []
    overshoots: list[dict[str, Any]] = []
    for d in devices:
        tag = d["capability_tag"]
        limits = {limit["target"]: limit for limit in tag["safety_limits"]}
        for act in tag["actuators"]:
            lim = limits[act["id"]]
            if "min" not in lim or act.get("requires_confirmation"):
                continue
            mid = (lim["min"] + lim["max"]) / 2
            plan.append({"device_id": d["device_id"], "target": act["id"], "value": mid})
            overshoots.append({
                "device_id": d["device_id"], "target": act["id"],
                "value": lim["max"] + abs(lim["max"] - lim["min"]) + 1,
            })
    print(f"plan: {len(plan)} in-bound write(s) + {len(overshoots)} deliberate overshoot(s)")

    check = rpc(args.url, token, "mhs.check", {"writes": plan + overshoots})
    refused = [r for r in check["results"] if not r["ok"]]
    if check["ok"] or len(refused) != len(overshoots) or check["transmitted"]:
        print(f"FAIL: expected {len(overshoots)} refusals, got {len(refused)}")
        failures += 1
    else:
        print(f"check: {len(refused)} overshoot(s) refused, nothing transmitted")
        for r in refused:
            data = r["error"].get("data", {})
            print(f"  {r['device_id']}.{r['target']} = {data.get('attempted')} refused: "
                  f"bound [{data.get('min')}, {data.get('max')}]")
    passed = [plan[r["index"]] for r in check["results"] if r["ok"] and r["index"] < len(plan)]

    for item in passed:
        res = rpc(args.url, token, "mhs.write", item)
        print(f"  write {item['device_id']}.{item['target']} = {item['value']} -> "
              f"verified={res.get('verified')}")

    after = rpc(args.url, token, "mhs.snapshot")
    for item in passed:
        reading = after["devices"][item["device_id"]]["channels"][item["target"]]
        if reading.get("value") != item["value"]:
            print(f"FAIL: {item['device_id']}.{item['target']} reads {reading}")
            failures += 1
    print(f"snapshot: {len(passed)} commanded value(s) confirmed by re-reading the cell")

    stop = rpc(args.url, token, "mhs.emergency_stop_all")
    stopped = sum(1 for r in stop["devices"].values() if r.get("stopped"))
    print(f"stop all: {stopped} stopped, {stop['count'] - stopped - stop['failed']} skipped, "
          f"{stop['failed']} failed")
    failures += stop["failed"]

    print("OK" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
