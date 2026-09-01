#!/usr/bin/env python
"""Run the robosuite digital twin behind a live Open-MHS server.

    export OPEN_MHS_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    python examples/robosuite_demo/run_cell.py

Two devices are registered:

    cv-camera-01   read-only.  cube_x / cube_y / cube_z, plus pose_source so the caller
                   can tell a vision estimate from a ground-truth fallback.
    panda-arm-01   tcp_x / tcp_y / tcp_z / gripper_state, bounded to a measured work
                   envelope whose z floor sits above the table.

An agent has to ask the camera where the cube is before it can reach for it — the arm
device exposes no knowledge of the scene at all. That separation is the point: perception
and actuation are different devices with different capability tags, and the safety
envelope belongs to the one that can hurt something.

MuJoCo is not thread-safe, so the environment owns the MAIN thread and uvicorn runs on a
worker.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from cell import Workcell                     # noqa: E402
from cv_camera import CVCameraDevice          # noqa: E402
from panda_arm import PandaArmDevice          # noqa: E402
from server.main import create_app            # noqa: E402
from server.registry import Registry          # noqa: E402


def build(token: str, render: bool = False, interactive: bool = False,
          pov: bool = False):
    """Bring up the twin and the middleware around it. Caller runs `cell.loop()`."""
    cell = Workcell(render=render, interactive=interactive, pov=pov)
    cell.build()

    registry = Registry()
    for device in (CVCameraDevice(cell), PandaArmDevice(cell)):
        registry.register(device.tag, device)
    return cell, create_app(registry, load_mocks=False, auth_token=token)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="robosuite digital twin served through the Open-MHS middleware.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--render", action="store_true",
                        help="robosuite's fixed-camera on-screen renderer")
    parser.add_argument("--viewer", action="store_true",
                        help="MuJoCo's interactive viewer: left-drag orbits, right-drag "
                             "pans, wheel zooms, double-click tracks a body")
    parser.add_argument("--pov", action="store_true",
                        help="live wrist-camera window: what the arm itself sees, with "
                             "the middleware's world model drawn over it")
    args = parser.parse_args()

    token = os.getenv("OPEN_MHS_AUTH_TOKEN")
    if not token:
        print("OPEN_MHS_AUTH_TOKEN is not set. This serves live hardware control; the "
              "middleware will not start without it.", file=sys.stderr)
        return 1

    print("  building the MuJoCo environment (first run compiles assets, give it a moment)...")
    cell, app = build(token, render=args.render, interactive=args.viewer,
                      pov=args.pov)

    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    threading.Thread(target=server.run, name="open-mhs-http", daemon=True).start()

    print(f"\n  Open-MHS robosuite cell ready on http://{args.host}:{args.port}")
    print("  devices: cv-camera-01 (read-only)   panda-arm-01 (tcp_x/y/z, gripper_state)")
    print("  table top z=0.800 m   tcp_z floor 0.83 m   Ctrl-C to stop.\n")
    try:
        cell.loop()               # the main thread owns MuJoCo
    except KeyboardInterrupt:
        pass
    finally:
        cell.shutdown()
        server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
