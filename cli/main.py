"""`open-mhs` — the command-line gate.

Same middleware, same safety path, same refusal text as the MCP tools. A human at a shell
and a model behind MCP read identical words when a write is refused.

    open-mhs discover
    open-mhs read arm-01 joint_1_actual
    open-mhs write arm-01 joint_1 45
    open-mhs snapshot [DEVICE ...]
    open-mhs check plan.json
    open-mhs estop arm-01 | --all
    open-mhs describe examples/robotic_arm.mhs      # no server needed
    open-mhs export examples/robotic_arm.mhs --out arm01.py   # the code-file gate
    open-mhs doc examples/robotic_arm.mhs --out DEVICE.md     # the reference a model reads
    open-mhs audit verify open-mhs-audit.jsonl     # no server needed
    open-mhs serve [--host] [--port] [--no-mocks]

Exit codes: 0 ok, 1 refused or plan rejected, 2 usage, 3 middleware unreachable or
unauthorized.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp_adapter.client import OpenMHSClient, OpenMHSUnreachable, RemoteRPCError, Unauthorized
from mcp_adapter.formatting import (
    format_check,
    format_discovery,
    format_emergency_stop,
    format_estop_all,
    format_read,
    format_rpc_error,
    format_snapshot,
    format_unauthorized,
    format_unreachable,
    format_write,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3


def parse_value(raw: str) -> Any:
    """`45` -> 45.0, `true` -> True, `[1,2,3]` -> list, anything else stays a string.

    Enum and string actuators take words; a word that happens to parse as a number is
    sent as a number, which the middleware then refuses with a type error. That is the
    correct outcome: the tag, not the shell, decides what a channel accepts.
    """
    low = raw.strip().lower()
    if low in {"true", "false"}:
        return low == "true"
    try:
        return float(raw)
    except ValueError:
        pass
    if raw.startswith("["):
        return json.loads(raw)
    return raw


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="open-mhs", description="Operate hardware through an Open-MHS middleware."
    )
    p.add_argument(
        "--url", default=None,
        help="middleware URL (default $OPEN_MHS_URL, then http://127.0.0.1:8000)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover", help="list devices and their capability tags")

    r = sub.add_parser("read", help="read one sensor or actuator channel")
    r.add_argument("device")
    r.add_argument("target")

    w = sub.add_parser("write", help="command one actuator (MOVES HARDWARE)")
    w.add_argument("device")
    w.add_argument("target")
    w.add_argument("value")
    w.add_argument("--confirm", action="store_true", help="a human has approved this write")

    s = sub.add_parser("snapshot", help="read every channel of every device")
    s.add_argument("devices", nargs="*")

    c = sub.add_parser(
        "check", help="dry-run a plan file: a JSON list of {device_id,target,value,confirm?}"
    )
    c.add_argument("plan")

    e = sub.add_parser("estop", help="emergency stop one device, or --all")
    e.add_argument("device", nargs="?")
    e.add_argument("--all", action="store_true")

    d = sub.add_parser(
        "describe", help="render a .mhs tag as the reference a model reads; no server needed"
    )
    d.add_argument("tag")

    x = sub.add_parser(
        "export",
        help="generate a standalone typed Python module for a device from its .mhs tag; "
             "no server needed",
    )
    x.add_argument("tag")
    x.add_argument("--out", default=None, help="write here instead of stdout")

    doc = sub.add_parser(
        "doc",
        help="generate the Markdown reference an agent reads before touching this device; "
             "no server needed",
    )
    doc.add_argument("tag")
    doc.add_argument("--out", default=None, help="write here instead of stdout")
    doc.add_argument("--url", default="http://127.0.0.1:8000",
                     help="middleware URL to print in the MCP client snippet")

    a = sub.add_parser("audit", help="audit log tools")
    asub = a.add_subparsers(dest="audit_cmd", required=True)
    asub.add_parser("verify", help="walk the hash chain").add_argument("file")

    sv = sub.add_parser("serve", help="run the middleware")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--no-mocks", action="store_true", help="start with no reference devices")
    return p


async def _run(args: argparse.Namespace, client: OpenMHSClient) -> int:
    try:
        if args.cmd == "discover":
            print(format_discovery(await client.discover()))
            return EXIT_OK
        if args.cmd == "read":
            result = await client.rpc(
                "mhs.read", {"device_id": args.device, "target": args.target}
            )
            print(format_read(result))
            return EXIT_OK
        if args.cmd == "write":
            result = await client.rpc("mhs.write", {
                "device_id": args.device, "target": args.target,
                "value": parse_value(args.value), "confirm": args.confirm,
            })
            print(format_write(result))
            return EXIT_OK
        if args.cmd == "snapshot":
            params = {"device_ids": args.devices} if args.devices else {}
            print(format_snapshot(await client.rpc("mhs.snapshot", params)))
            return EXIT_OK
        if args.cmd == "check":
            writes = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            result = await client.rpc("mhs.check", {"writes": writes})
            print(format_check(result))
            return EXIT_OK if result["ok"] else EXIT_REFUSED
        if args.cmd == "estop":
            if args.all:
                result = await client.rpc("mhs.emergency_stop_all", {})
                print(format_estop_all(result))
                return EXIT_OK if result["failed"] == 0 else EXIT_REFUSED
            if not args.device:
                print("estop: give a device id or --all", file=sys.stderr)
                return EXIT_USAGE
            result = await client.rpc("mhs.emergency_stop", {"device_id": args.device})
            print(format_emergency_stop(result))
            return EXIT_OK
    except RemoteRPCError as exc:
        print(format_rpc_error(exc))
        return EXIT_REFUSED
    except Unauthorized as exc:
        print(format_unauthorized(exc))
        return EXIT_UNREACHABLE
    except OpenMHSUnreachable as exc:
        print(format_unreachable(exc))
        return EXIT_UNREACHABLE
    return EXIT_USAGE


def _describe(path: str) -> int:
    from server.models import CapabilityTag

    tag = CapabilityTag.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
    summary = {"count": 1, "devices": [{
        "device_id": tag.device_id, "name": tag.name, "type": tag.type,
        "online": False, "has_local_driver": False, "registered_at": 0, "last_seen": 0,
        "capability_tag": tag.model_dump(mode="json", exclude_none=True),
    }]}
    # The renderer describes live devices. This is a file on disk, and it must not be
    # described as a device that has stopped answering.
    text = format_discovery(summary).replace(
        "status: STALE (missed heartbeats)", "status: tag file only, not a live device"
    )
    print(text)
    return EXIT_OK


def _export(tag_path: str, out: str | None) -> int:
    from cli.export import generate
    from server.models import CapabilityTag

    tag = CapabilityTag.model_validate(json.loads(Path(tag_path).read_text(encoding="utf-8")))
    source = generate(tag)
    if out:
        Path(out).write_text(source, encoding="utf-8")
        print(f"wrote {out}: class {source.split('class ')[2].split(':')[0]} for {tag.device_id}")
    else:
        print(source, end="")
    return EXIT_OK


def _doc(tag_path: str, out: str | None, url: str) -> int:
    from cli.device_doc import generate
    from server.models import CapabilityTag

    tag = CapabilityTag.model_validate(json.loads(Path(tag_path).read_text(encoding="utf-8")))
    text = generate(tag, url=url)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"wrote {out}: reference for {tag.device_id}")
    else:
        print(text, end="")
    return EXIT_OK


def _audit_verify(path: str) -> int:
    from server.audit import verify

    report = verify(path)
    if report["ok"]:
        print(f"ok, {report['lines']} line(s), chain intact")
        return EXIT_OK
    print(f"BROKEN at line {report['first_bad_line']} ({report['lines']} line(s) read)")
    return EXIT_REFUSED


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from server.main import create_app

    if not os.getenv("OPEN_MHS_AUTH_TOKEN"):
        print(
            "OPEN_MHS_AUTH_TOKEN is not set; the middleware will not start without it.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    uvicorn.run(create_app(load_mocks=not args.no_mocks), host=args.host, port=args.port)
    return EXIT_OK


async def amain(argv: list[str] | None = None, *, client: OpenMHSClient | None = None) -> int:
    """Async entry point. Tests call this with an in-process client."""
    args = build_parser().parse_args(argv)
    if args.cmd == "describe":
        return _describe(args.tag)
    if args.cmd == "export":
        return _export(args.tag, args.out)
    if args.cmd == "doc":
        return _doc(args.tag, args.out, args.url)
    if args.cmd == "audit":
        return _audit_verify(args.file)
    if args.cmd == "serve":
        return _serve(args)
    own = client is None
    client = client or OpenMHSClient(args.url)
    try:
        return await _run(args, client)
    finally:
        if own:
            await client.aclose()


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point."""
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    sys.exit(main())
