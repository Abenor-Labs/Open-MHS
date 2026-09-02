#!/usr/bin/env python
"""The same cell, the same commands, no middleware. The "before" shot.

    python examples/robosuite_demo/without_mhs.py --viewer

Everything `run_cell.py` puts between an agent and the servo is removed here. Commands go
straight into the simulator's target pose, which is exactly what a tool call does in any
integration that has no safety layer: the model says a number, the controller chases it.

The sequence is the one the middleware refuses in the recorded demo. Move over the red
block, then command the tool to z = 0.70 m — ten centimetres below a table top at 0.800 m.
With the middleware that command is clamped to the measured floor and the caller is told
so. Here the arm simply drives the gripper into the table and keeps pushing, and the
"agent" is told nothing at all, because nothing exists to tell it.

Not a scripted render: the physics decide where the tool ends up. It is not 0.70.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cell import Workcell  # noqa: E402

TABLE_TOP = 0.800          # measured, see CLAUDE.md
FLOOR_WITH_MHS = 0.83      # what the capability tag would have held the tool to


def drive(cell: Workcell, results: dict) -> None:
    """Runs on a worker thread; the simulator owns the main thread."""
    def settle(seconds: float) -> None:
        time.sleep(seconds)

    def pose() -> tuple[float, float, float]:
        return tuple(round(cell.read(f"tcp_{a}_actual"), 4) for a in "xyz")

    settle(2.0)
    print(f"\n  start            tcp = {pose()}")

    cell.command("tcp_x", cell.read("red_block_x"))
    cell.command("tcp_y", cell.read("red_block_y"))
    settle(3.0)
    print(f"  over red block   tcp = {pose()}")

    # The command the middleware clamps. Nothing here checks it.
    print("\n  agent: write tcp_z = 0.70    (table top is at 0.800)")
    print("  reply: <none - there is no layer to reply>")
    cell.command("tcp_z", 0.70)
    lowest = 9.0
    for _ in range(60):                      # 6 s of pushing
        settle(0.1)
        lowest = min(lowest, cell.read("tcp_z_actual"))
    z = pose()[2]
    results["lowest_z"] = round(lowest, 4)
    results["final_z"] = z
    print(f"  after 6 s        tcp = {pose()}   lowest z seen = {lowest:.4f}")
    print(f"\n  commanded 0.70, table at {TABLE_TOP}, tool stopped at {lowest:.4f}: "
          f"that is the table stopping it, not software.")
    print(f"  with the middleware the floor is {FLOOR_WITH_MHS} and the arm never touches it.")
    cell.command("tcp_z", 1.05)
    settle(2.0)
    cell.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--viewer", action="store_true", help="interactive MuJoCo window")
    args = ap.parse_args()

    print("  building the cell with NO middleware (first run compiles assets)...")
    cell = Workcell(render=False, interactive=args.viewer, pov=False)
    cell.build()
    results: dict = {}
    threading.Thread(target=drive, args=(cell, results), daemon=True).start()
    try:
        cell.loop()
    except KeyboardInterrupt:
        cell.shutdown()
    # The claim this file makes must be true of the physics, or the script says so.
    lowest = results.get("lowest_z")
    if lowest is None:
        print("  no measurement taken"); return 2
    if lowest >= FLOOR_WITH_MHS:
        print(f"  UNEXPECTED: tool never went below the middleware's floor ({lowest})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
