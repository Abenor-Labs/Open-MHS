# Benchmarks

What a device actually refuses, measured rather than asserted.

```bash
export OPEN_MHS_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
open-mhs serve &
open-mhs bench --out my-cell.md --json my-cell.json
```

The corpus is generated from whatever tags are registered, so this works against the
shipped mock cell, against the PyBullet or robosuite twins, against another
implementation of the standard, and — with care and a hardware interlock in place —
against real machinery. Exit code is non-zero if any refusal moved the hardware.

## The measurement

Every attempt is bracketed by reads of the target it aims at. The question is not whether
an error came back, because a refusal that still transmitted would answer that question
correctly and break a machine anyway. The question is **whether the world changed**.

Attribution is deliberately conservative in both directions. The target is read twice
before each attempt to measure its natural jitter, and a numeric change only counts when
it exceeds that jitter *and* closes at least a quarter of the distance to the value that
was commanded. A thermometer drifting a few hundredths is not a safety failure; a driver
that clamped and transmitted anyway lands short of the setpoint and still is.

## Results

| Cell | Devices | Unsafe blocked | Legal accepted | Leaks | Report |
|---|---|---|---|---|---|
| Reference mocks | arm, thermometer, pump | 36/36 | 11/11 | 0 | [reference-cell.md](reference-cell.md) |

## Does the benchmark work?

A clean run means nothing unless a dirty one would be caught, so that is tested two ways.
`tests/test_bench.py` runs the corpus against a middleware that refuses every
out-of-bounds write and performs it anyway, and requires the leak to be detected. And
deleting both of the middleware's enforcement points by hand takes the reference cell from
36/36 blocked to 17/36, with the drop reported rather than hidden.

## What it does not measure

Simulated devices only. Whether the declared bounds are the *right* bounds. Whether an
agent obeys a refusal after reading one. Whether prompt injection through tag text
actually steers a model, which is mitigated and unit-tested but unmeasured against a real
one. Behaviour under concurrent load.
