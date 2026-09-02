"""The code-file gate: a generated module drives the middleware with no model in the loop.

The generated code goes over real HTTP (urllib), so these tests run a uvicorn on a free
port and import the module the way a controller script would.
"""

from __future__ import annotations

import ast
import importlib.util
import socket
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

from open_mhs.cli import main as cli
from open_mhs.cli.export import class_name, generate, method_name
from open_mhs.server.models import CapabilityTag
from tests.conftest import EXAMPLES, FIXTURES, TEST_TOKEN, load_tag


def _load(source: str, name: str, tmp_path: Path) -> ModuleType:
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------------------
# Static properties of the generated source
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.mhs")) + sorted(FIXTURES.glob("*.mhs")))
def test_every_valid_tag_exports_to_code_that_compiles(path: Path) -> None:
    try:
        tag = CapabilityTag.model_validate(load_tag(path))
    except Exception:
        pytest.skip("fixture is invalid by design")
    source = generate(tag)
    ast.parse(source)  # raises on a syntax error
    compile(source, str(path), "exec")


def test_generated_module_enforces_nothing() -> None:
    """Bounds are information for the controller, never a second check in the client.

    A local clamp would let a controller believe a value was accepted when the
    middleware would have refused it, and a local refusal would drift from the tag.
    """
    source = generate(CapabilityTag.model_validate(load_tag(EXAMPLES / "robotic_arm.mhs")))
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "min" not in names and "max" not in names
    compares_in_writes = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name.startswith("write_")
        and any(isinstance(m, (ast.Compare, ast.If)) for m in ast.walk(n))
    ]
    assert compares_in_writes == []


def test_names() -> None:
    assert class_name("arm-01") == "Arm01"
    assert class_name("cv-camera-01") == "CvCamera01"
    assert class_name("01-thing") == "Device01Thing"
    assert method_name("joint_1_actual") == "joint_1_actual"
    assert method_name("red_block.x") == "red_block_x"


def test_generated_source_carries_the_bounds_and_the_confirm_gate(tmp_path: Path) -> None:
    source = generate(CapabilityTag.model_validate(load_tag(EXAMPLES / "robotic_arm.mhs")))
    module = _load(source, "arm_static", tmp_path)
    assert module.BOUNDS["joint_1"]["max"] == 90.0
    assert module.DEVICE_ID == "arm-01"
    assert "[-90.0, 90.0] deg" in module.Arm01.write_joint_1.__doc__
    assert "REQUIRES HUMAN CONFIRMATION" in module.Arm01.write_gripper.__doc__
    assert "Verified against read_joint_1_actual()" in module.Arm01.write_joint_1.__doc__


def test_read_only_device_has_no_write_methods_and_no_estop(tmp_path: Path) -> None:
    source = generate(CapabilityTag.model_validate(load_tag(EXAMPLES / "mock_temp_sensor.mhs")))
    module = _load(source, "temp_static", tmp_path)
    cls = module.MockTemp01
    assert not [n for n in dir(cls) if n.startswith("write_")]
    assert not hasattr(cls, "emergency_stop")
    assert hasattr(cls, "read_temperature") or [n for n in dir(cls) if n.startswith("read_")]


# --------------------------------------------------------------------------------------
# Against a live middleware
# --------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path: Path, monkeypatch):
    import uvicorn

    from open_mhs.server.main import create_app

    monkeypatch.setenv("OPEN_MHS_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    port = _free_port()
    app = create_app(auth_token=TEST_TOKEN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def arm(live_server: str, tmp_path: Path):
    source = generate(CapabilityTag.model_validate(load_tag(EXAMPLES / "robotic_arm.mhs")))
    module = _load(source, "arm_live", tmp_path)
    return module.Arm01(url=live_server, token=TEST_TOKEN), module


def test_generated_client_reads_writes_and_is_refused_by_the_middleware(arm) -> None:
    device, module = arm
    assert device.read_joint_1_actual() == 0.0
    result = device.write_joint_1(45.0)
    assert result["accepted"] is True and result["verified"] is True
    assert device.read_joint_1_actual() == 45.0
    with pytest.raises(module.OpenMHSRefused) as exc:
        device.write_joint_1(500.0)
    assert exc.value.code == -32001
    assert exc.value.data["max"] == 90.0
    assert device.read_joint_1_actual() == 45.0  # nothing moved


def test_generated_client_honours_the_confirm_gate(arm) -> None:
    device, module = arm
    with pytest.raises(module.OpenMHSRefused) as exc:
        device.write_gripper("closed")
    assert exc.value.data["requires_confirmation"] is True
    assert device.write_gripper("closed", confirm=True)["accepted"] is True
    assert device.read_gripper() == "closed"


def test_generated_client_snapshot_check_and_estop(arm) -> None:
    device, _ = arm
    snap = device.snapshot()
    assert snap["joint_1_actual"] == {"value": 0.0, "unit": "deg"}
    verdict = device.check([{"target": "joint_1", "value": 10.0}, {"target": "joint_1", "value": 1e6}])
    assert verdict["ok"] is False and verdict["transmitted"] is False
    assert [r["ok"] for r in verdict["results"]] == [True, False]
    device.write_joint_1(30.0)
    assert device.emergency_stop()["stopped"] is True
    assert device.read_joint_1_actual() == 0.0


def test_a_controller_with_no_model_can_plan_inside_bounds(arm) -> None:
    """The QuEra pattern: read BOUNDS, sweep inside them, never get refused."""
    device, module = arm
    lo, hi = module.BOUNDS["joint_1"]["min"], module.BOUNDS["joint_1"]["max"]
    step = (hi - lo) / 4
    accepted = []
    for i in range(5):
        target = lo + i * step
        accepted.append(device.write_joint_1(target)["commanded"])
        device.emergency_stop()  # reset so max_rate never trips between sweeps
    assert accepted == [lo, lo + step, lo + 2 * step, lo + 3 * step, hi]


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def test_the_exported_controller_example_runs_clean(live_server: str, tmp_path: Path) -> None:
    """The shipped code-file example: export, import, sweep, fit, close the loop, probe.

    No model is in the loop after the export. The middleware still refuses the probe.
    """
    import os
    import subprocess
    import sys as _sys

    from tests.conftest import REPO_ROOT

    proc = subprocess.run(
        [_sys.executable, str(REPO_ROOT / "examples" / "exported_controller.py"),
         "--url", live_server],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "OPEN_MHS_AUTH_TOKEN": TEST_TOKEN}, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.rstrip().endswith("OK"), proc.stdout
    assert "refused: [-32001]" in proc.stdout
    assert "nothing transmitted" in proc.stdout


@pytest.mark.asyncio
async def test_cli_export_writes_a_module(tmp_path: Path, capsys) -> None:
    out = tmp_path / "pump.py"
    code = await cli.amain(["export", str(EXAMPLES / "bench_pump.mhs"), "--out", str(out)])
    assert code == 0
    assert "class Pump01" in out.read_text(encoding="utf-8")
    assert str(out) in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_export_to_stdout(capsys) -> None:
    code = await cli.amain(["export", str(EXAMPLES / "robotic_arm.mhs")])
    assert code == 0
    assert "class Arm01" in capsys.readouterr().out
