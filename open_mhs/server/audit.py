"""Append-only, hash-chained audit log.

One JSON line per command that could change the world, and one per refusal. Each line
carries the SHA-256 of the previous line, so a deleted or edited line breaks the chain and
`verify()` says where. This is not a signature — anyone with write access to the file can
rebuild the chain — but it turns silent tampering into an act that leaves a trace, and it
is what a lab QA process or EU 2023/1230 traceability actually asks for first.

Reads are not logged. They do not change the world and would drown the file.

Configuration: `OPEN_MHS_AUDIT_LOG` — a path, or `off`. Default `open-mhs-audit.jsonl`.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
ENV_VAR = "OPEN_MHS_AUDIT_LOG"
DEFAULT_PATH = "open-mhs-audit.jsonl"


def _canonical(entry: dict[str, Any]) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str).encode()


def _digest(entry: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(entry)).hexdigest()


class AuditLog:
    """Writer. Thread-safe; one instance per app."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._seq = 0
        self._prev = GENESIS
        if self.path is not None and self.path.exists():
            self._resume()

    @classmethod
    def from_env(cls) -> AuditLog:
        raw = os.getenv(ENV_VAR, DEFAULT_PATH)
        return cls(None if raw.strip().lower() == "off" else raw)

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def _resume(self) -> None:
        """Continue an existing chain rather than restarting it."""
        assert self.path is not None
        last: dict[str, Any] | None = None
        with self.path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if raw:
                    last = json.loads(raw)
        if last is not None:
            self._seq = int(last["seq"])
            self._prev = str(last["hash"])

    def record(
        self,
        event: str,
        *,
        device_id: str | None,
        target: str | None,
        params: dict[str, Any],
        outcome: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Append one line. Returns the entry, or None when logging is off."""
        if self.path is None:
            return None
        with self._lock:
            entry: dict[str, Any] = {
                "seq": self._seq + 1,
                "ts": time.time(),
                "event": event,
                "device_id": device_id,
                "target": target,
                "params": params,
                "outcome": outcome,
                "prev": self._prev,
            }
            entry["hash"] = _digest(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._seq = entry["seq"]
            self._prev = entry["hash"]
            return entry


def verify(path: str | Path) -> dict[str, Any]:
    """Walk the chain. `first_bad_line` is 1-based, None when the log is intact."""
    prev = GENESIS
    expected_seq = 1
    lines = 0
    with Path(path).open("r", encoding="utf-8") as fh:
        for number, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            lines += 1
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                return {"ok": False, "lines": lines, "first_bad_line": number}
            claimed = entry.get("hash")
            body = {k: v for k, v in entry.items() if k != "hash"}
            if (
                entry.get("prev") != prev
                or entry.get("seq") != expected_seq
                or _digest(body) != claimed
            ):
                return {"ok": False, "lines": lines, "first_bad_line": number}
            prev = claimed
            expected_seq += 1
    return {"ok": True, "lines": lines, "first_bad_line": None}
