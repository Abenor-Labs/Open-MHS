---
name: schema-validator
description: Use when validating, authoring, or debugging an Open-MHS Capability Tag (.mhs / .ohs / .json) against schema/capability_schema.json. Triggers on "validate this capability tag", "is my .mhs file valid", "check device manifest", "why is my hardware rejected by the registry", or any task that creates or edits a device descriptor.
---

# Open-MHS Capability Tag Validator

Validate a hardware Capability Tag against the official Open-MHS JSON Schema and report
actionable, line-anchored errors.

## When to use

- A new driver author wrote a `.mhs` / `.ohs` file and needs it checked before registration.
- The Discovery Layer rejected a device and the reason is unclear.
- You are about to edit `schema/capability_schema.json` and must re-validate all fixtures.
- CI needs a deterministic pass/fail gate over `examples/**/*.mhs`.

## Prerequisites

```bash
pip install jsonschema
```

The canonical schema lives at `schema/capability_schema.json`. Never validate against a
copy — always resolve the repo-root path so drift is impossible.

## Workflow

### 1. Locate the artifacts

```bash
ls schema/capability_schema.json
find . -name '*.mhs' -o -name '*.ohs' | grep -v node_modules
```

If the schema file is missing, stop and say so. Do not invent a schema.

### 2. Run structural validation

```bash
python -m tools.validate_tag <path-to-tag>          # preferred, if tools/ exists
```

Fallback one-liner when no CLI is present:

```bash
python - <<'PY'
import json, sys
from jsonschema import Draft202012Validator
schema = json.load(open("schema/capability_schema.json"))
doc = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "device.mhs"))
errs = sorted(Draft202012Validator(schema).iter_errors(doc), key=lambda e: e.path)
for e in errs:
    print(f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}")
print("VALID" if not errs else f"INVALID ({len(errs)} error(s))")
PY
```

### 3. Run the semantic checks the schema cannot express

JSON Schema catches shape. It does not catch physics. Always additionally verify:

| Check               | Rule                                                                                                                                                          |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Safety coverage     | Every entry in`actuators[]` has a matching `safety_limits[]` entry keyed by the same `id`. An unbounded actuator is a hard failure, not a warning.      |
| Limit sanity        | `min < max` for every limit. `default` (if present) falls inside `[min, max]`.                                                                          |
| Unit agreement      | The`unit` on an actuator matches the `unit` on its safety limit. `deg` vs `rad` mismatches are the classic field failure.                             |
| ID uniqueness       | `id` is unique across the union of `sensors[]` and `actuators[]`, not just within each array. `read("x")` and `write("x")` must never be ambiguous. |
| Sensor immutability | No`id` appears in both `sensors[]` and `actuators[]` unless the actuator declares a paired `feedback_sensor`.                                         |
| Rate limits         | Any actuator with`max_rate` also declares `unit` and a time base.                                                                                         |

### 4. Report

Emit one line per problem, most severe first:

```
<file>:<json-pointer>: <ERROR|WARN>: <what is wrong>. <how to fix>.
```

Close with a single verdict line: `VALID` or `INVALID (n errors, m warnings)`.

## Rules

- **Never auto-fix a safety limit.** Widening a bound to make validation pass can move real
  hardware into a destructive range. Report it and let a human decide.
- Never relax `schema/capability_schema.json` to accommodate one device. If a legitimate
  device cannot be expressed, that is a schema RFC, not a local edit.
- Unknown top-level keys are errors, not warnings — `additionalProperties: false` is
  deliberate so typos like `actuatorz` cannot silently disable safety enforcement.
- Validate the file as written on disk. Do not normalize, reformat, or reorder first.

## Red flags

| Thought                                         | Reality                                                 |
| ----------------------------------------------- | ------------------------------------------------------- |
| "The limits are obviously wrong, I'll fix them" | You do not know the hardware. Report only.              |
| "Just add the field to the schema"              | Schema changes need an RFC + all fixtures re-validated. |
| "It parsed as JSON, so it's fine"               | Parsing is not validation. Run the validator.           |
| "One missing safety limit is a warning"         | It is an ERROR. Unbounded write = unbounded hardware.   |
