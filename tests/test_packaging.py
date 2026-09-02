"""What an installed wheel must carry, and what must not drift between copies."""

from __future__ import annotations

import json
from importlib import resources

import pytest

from tests.conftest import REPO_ROOT

CANONICAL = REPO_ROOT / "schema" / "capability_schema.json"
PACKAGED = REPO_ROOT / "server" / "capability_schema.json"


def test_packaged_schema_is_byte_identical_to_the_canonical_one() -> None:
    """Two copies exist so the wheel ships one. They must never disagree.

    If this fails: `cp schema/capability_schema.json server/capability_schema.json`.
    """
    assert PACKAGED.read_bytes() == CANONICAL.read_bytes()


def test_packaged_schema_is_reachable_as_package_data() -> None:
    text = resources.files("server").joinpath("capability_schema.json").read_text("utf-8")
    schema = json.loads(text)
    assert schema["$schema"].startswith("https://json-schema.org/draft/2020-12")


def test_every_package_directory_is_covered_by_package_discovery() -> None:
    """A wheel once shipped without `server.routers`; the app imported fine from a
    checkout and crashed from `pip install`. Every directory with an `__init__.py` under
    a shipped root must match a discovery pattern."""
    pytest.importorskip("tomllib")
    import fnmatch
    import tomllib

    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    patterns = cfg["tool"]["setuptools"]["packages"]["find"]["include"]
    roots = ["server", "drivers", "mcp_adapter", "cli"]
    missing = []
    for root in roots:
        for init in (REPO_ROOT / root).rglob("__init__.py"):
            dotted = ".".join(init.parent.relative_to(REPO_ROOT).parts)
            if not any(fnmatch.fnmatch(dotted, pat) for pat in patterns):
                missing.append(dotted)
    assert missing == [], f"packages not covered by [tool.setuptools.packages.find]: {missing}"


def test_cli_console_script_is_declared() -> None:
    pytest.importorskip("tomllib")
    import tomllib

    scripts = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))["project"]["scripts"]
    assert scripts["open-mhs"] == "cli.main:main"
    assert scripts["open-mhs-mcp"] == "mcp_adapter.server:main"
