#!/usr/bin/env python
"""The code-file gate, end to end: explore with an agent, then run without one.

This is the pattern behind every "the agent tuned it overnight, then we deployed the
controller" result: a model is useful while the search space is unknown, and a liability
once it is known. The exported module is the handover point.

    export OPEN_MHS_AUTH_TOKEN=...
    open-mhs serve &
    python examples/exported_controller.py

What it does:

    1. export     generate a typed module from the pump's Capability Tag
    2. import     load it the way a controller script would
    3. pace       read max_rate out of BOUNDS and wait between commands, because a value
                  inside the range is still refused if it gets there too fast
    4. explore    sweep the declared envelope, recording (setpoint -> observed flow);
                  every bound comes from BOUNDS, none is hardcoded here
    5. fit        derive one constant from the sweep
    6. deploy     run a closed-loop controller that hits a target using that constant,
                  with no model, no agent, and no search
    7. probe      command one value past the bound and confirm it is refused

Nothing here enforces a limit. The middleware refuses out-of-envelope writes whether the
caller is Claude, a shell, or this script. That is the point: the safety argument does not
depend on who is calling.

Exit code is non-zero if any expectation fails.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TAG = REPO_ROOT / "examples" / "bench_pump.mhs"


def export_module(tag_path: Path, out_dir: Path) -> ModuleType:
    """Step 1 and 2: tag -> source -> imported module. What `open-mhs export` does."""
    from open_mhs.cli.export import generate
    from open_mhs.server.models import CapabilityTag

    tag = CapabilityTag.model_validate(json.loads(tag_path.read_text(encoding="utf-8")))
    source = generate(tag)
    path = out_dir / f"{tag.device_id.replace('-', '_')}.py"
    path.write_text(source, encoding="utf-8")

    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    print(f"1. exported {path.name} ({len(source.splitlines())} lines) from {tag_path.name}")
    return module


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy codepage
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.getenv("OPEN_MHS_URL", "http://127.0.0.1:8000"))
    args = ap.parse_args()
    token = os.getenv("OPEN_MHS_AUTH_TOKEN")
    if not token:
        print("OPEN_MHS_AUTH_TOKEN is not set", file=sys.stderr)
        return 2

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        module = export_module(TAG, Path(tmp))
        pump = module.Pump01(url=args.url, token=token)
        print(f"2. imported class {type(pump).__name__} for device {module.DEVICE_ID}")

        bounds = module.BOUNDS["flow_rate"]
        lo, hi = bounds["min"], bounds["max"]
        max_rate = bounds.get("max_rate")

        # --- 3. pace, because a legal value is still refused if it arrives too fast ----
        # This is the whole argument for exporting BOUNDS rather than only the min/max.
        # A controller that reads only the range writes 0 -> 10 in one tick and is
        # refused at 156 units/s against a 20 units/s cap. The device said so; read it.
        last = {"value": pump.read_flow_actual(), "at": time.monotonic()}

        def paced_write(value: float) -> dict:
            """Wait long enough that `value` is reachable within max_rate, then write."""
            if max_rate:
                needed = abs(value - last["value"]) / max_rate
                waited = time.monotonic() - last["at"]
                if needed > waited:
                    time.sleep(needed - waited + 0.01)
            result = pump.write_flow_rate(value)
            last["value"], last["at"] = value, time.monotonic()
            return result

        print(f"3. pacing to max_rate {max_rate} {bounds['unit']}/s, read from BOUNDS")

        # --- 4. explore, inside bounds the DEVICE declared, not bounds we assumed ------
        print(f"4. sweeping the declared envelope [{lo}, {hi}] {bounds['unit']}")
        samples: list[tuple[float, float]] = []
        for i in range(5):
            setpoint = lo + (hi - lo) * i / 4
            paced_write(setpoint)
            observed = pump.read_flow_actual()
            samples.append((setpoint, observed))
            print(f"     {setpoint:6.2f} -> {observed:6.2f}")

        # --- 5. fit one constant from the sweep ---------------------------------------
        gains = [obs / sp for sp, obs in samples if sp]
        gain = sum(gains) / len(gains)
        print(f"5. fitted gain observed/commanded = {gain:.4f} from {len(gains)} point(s)")

        # --- 6. closed loop, no model -------------------------------------------------
        target = round((lo + hi) / 3, 3)
        command = min(max(target / gain, lo), hi)
        paced_write(command)
        reached = pump.read_flow_actual()
        error = abs(reached - target)
        print(f"6. closed loop: target {target} -> commanded {command:.3f} -> "
              f"reached {reached} (error {error:.4f})")
        if error > 0.01:
            print("FAIL: controller missed the target")
            failures += 1

        # --- 7. the envelope still holds ----------------------------------------------
        over = hi + 1
        try:
            paced_write(over)
            print(f"FAIL: {over} was accepted; the envelope did not hold")
            failures += 1
        except module.OpenMHSRefused as exc:
            print(f"7. probe {over} refused: [{exc.code}] bound is "
                  f"[{exc.data.get('min')}, {exc.data.get('max')}] — nothing transmitted")
            if exc.code != -32001:
                failures += 1

        after = pump.read_flow_actual()
        if after != reached:
            print(f"FAIL: refused write moved the hardware ({reached} -> {after})")
            failures += 1

        pump.emergency_stop()
        if pump.read_flow_actual() != 0.0:
            print("FAIL: emergency stop did not reach the safe state")
            failures += 1
        print("8. emergency stop: back to the declared safe state")

    print("OK" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
