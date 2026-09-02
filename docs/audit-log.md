# Audit log

Every command that could change the world, and every refusal, is one JSON line in an
append-only file. Each line carries the SHA-256 of the previous line.

    OPEN_MHS_AUDIT_LOG=/var/log/open-mhs/audit.jsonl   # a path, or "off"

Default: `open-mhs-audit.jsonl` in the working directory. A restarted middleware appends
to an existing file and continues its chain.

## Line format

| Field | Meaning |
|---|---|
| `seq` | 1-based line number, continuous |
| `ts` | Unix time the line was written |
| `event` | `write.accepted`, `write.clamped`, `write.refused`, `write.desync`, `write.failed`, `estop`, `estop_all`, `duration.expired`, `register`, `deregister`, `check` |
| `device_id`, `target` | what was addressed (`null` for cell-wide events) |
| `params` | what the caller sent |
| `outcome` | `transmitted` (the value that reached the driver, or `null`), `error` (the JSON-RPC error object on refusal), and event-specific fields |
| `prev` | hash of the previous line; 64 zeros on the first |
| `hash` | SHA-256 of this line's canonical JSON without `hash` |

`write.refused` means nothing reached the driver. `write.desync` means the value **was**
transmitted and the feedback sensor then disagreed; `transmitted` carries the value that
went out, and `clamped`/`requested` say whether it was the caller's value or a corrected
one. `write.failed` means the transport raised mid-transmission and whether any byte
arrived is unknown, which is what `transmitted: "unknown"` records rather than guessing.

Reads are not logged. They change nothing and would drown the file.

## Verifying

    open-mhs audit verify open-mhs-audit.jsonl

Prints `ok, N line(s), chain intact` or the first line whose chain is broken. This detects
edits and deletions after the fact. It does not detect a complete rebuild by someone with
write access to the file; for that, ship the file to append-only storage or sign it, which
is v0.3 work.

## What it is not

Not a signature, not a persistent registry, not a replacement for the device's own logs.
It is the middleware's record of what it was asked, what it allowed, and what it refused.
