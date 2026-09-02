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


def test_cli_console_script_is_declared() -> None:
    pytest.importorskip("tomllib")
    import tomllib

    scripts = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))["project"]["scripts"]
    assert scripts["open-mhs"] == "cli.main:main"
    assert scripts["open-mhs-mcp"] == "mcp_adapter.server:main"
