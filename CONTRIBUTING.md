# Contributing to Open-MHS

Thanks for looking. This project guards machinery, so the bar for changes is a little
different from a typical library — but the process is ordinary and the maintainers are
friendly.

## Getting set up

```bash
git clone https://github.com/Abenor-Labs/Open-MHS.git
cd Open-MHS
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e . ruff
```

Run the suite. It needs no hardware and takes a couple of seconds:

```bash
pytest
ruff check .
```

Both must pass before a pull request is reviewed. CI runs them on Python 3.11 and 3.12
across Ubuntu and Windows.

## The most useful thing you can contribute: a driver

Open-MHS ships a real serial/G-code transport and an in-memory one. Everything else is open:

`Modbus TCP` · `CAN bus` · `ROS 2 bridges` · `Dynamixel servos` · `SCPI lab instruments` ·
`GPIO / I²C sensors` · `3D printers` · `syringe pumps` · `spectrometers`

A driver is two pieces:

1. **A transport** — implement `acquire` and `transmit` from
   [`drivers/transport.py`](drivers/transport.py). It moves bytes and knows nothing about
   capability tags, limits or units.
2. **A device** — subclass `BaseDevice` and implement `encode` / `decode`. This is where
   protocol knowledge lives.

Do **not** override `BaseDevice.write`. The safety path is not a subclass's business, and a
driver that reimplements it will be asked to remove that in review.

[`drivers/serial_robotic_arm.py`](drivers/serial_robotic_arm.py) is the reference example:
capability-tag values in, G-code out, over a real UART.

### Every driver ships with tests

Mock the **transport**, not the driver. A test that mocks the driver proves nothing about
the driver:

```text
test → real /rpc route → real driver class → FAKE transport
```

The checklist your driver's tests must cover:

- [ ] Its capability tag validates against the schema.
- [ ] `read()` on each declared sensor returns the declared type and unit.
- [ ] `read()` on an undeclared target raises rather than returning `None`.
- [ ] `write()` inside limits reaches the transport with the expected encoding.
- [ ] `write()` below `min` is rejected — **and the transport recorded zero bytes**.
- [ ] `write()` above `max` is rejected — same two assertions.
- [ ] `write()` exactly at `min` and `max` is accepted (bounds are inclusive).
- [ ] `write()` to a sensor id is rejected with `-32602`.
- [ ] Transport failure surfaces as `-32002`, not an unhandled exception.
- [ ] A transport that accepts a write but does not move surfaces as `-32003`.

The recurring theme: **assert on the transport, not just the return value.** A rejected
write that still emitted bytes is a safety failure that a return-value assertion will
happily pass.

## Rules that are not negotiable

These exist because the failure mode is physical.

1. **Never widen a safety bound to make something validate.** If a legitimate device cannot
   be expressed, that is a schema RFC, not a local edit.
2. **Never weaken a test to make a change pass.** If a test is wrong, fix the test in its
   own commit with a stated reason.
3. **Schema changes are RFCs.** A change to `schema/capability_schema.json` needs a written
   rationale and re-validation of every fixture and example.
4. **Both enforcement points stay.** The middleware checks before the driver is called; the
   driver checks before the transport is touched. Removing either is not an optimisation.
5. **Refusals must stay actionable.** An error that does not tell the caller the real bound
   and a valid retry is an incomplete error.

## Other high-value work

- **Signed capability tags** — the deepest gap in the current trust model. Tags are
  authenticated but not attested; a token holder can declare any limits.
- **Per-device credentials**, so a compromised sensor cannot command an arm.
- **Real-hardware validation reports** — run a driver against actual metal and tell us what
  broke. This is genuinely valuable and nobody has done it yet.
- **Enforce `max_duration_s`**, which the schema defines and the middleware currently
  parses but ignores.

## Pull requests

- Branch off `main`, keep the change focused, and explain *why* in the description.
- Include tests. A change to safety behaviour without a test that fails before it will be
  sent back.
- Say plainly what you did **not** verify — especially if you could not test against real
  hardware. An honest gap is fine; an unstated one is not.

## Reporting security problems

Do not open a public issue. See [SECURITY.md](SECURITY.md).

## Code of conduct

Be decent to each other. Assume good faith, keep criticism about the code, and remember
that someone reading this may be about to point it at a machine that can hurt them.

## License

By contributing you agree that your contributions are licensed under
[Apache-2.0](LICENSE), the same terms as the project.
