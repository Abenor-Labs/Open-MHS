---
name: pytest-hardware-mock
description: Use when writing tests for Open-MHS drivers, the registry, or the execute path without physical hardware attached. Triggers on "test the driver", "mock a robotic arm", "simulate a sensor", "add a fixture for the registry", "test that safety limits hold", or any new file under tests/.
---

# Open-MHS Mock Hardware Test Environment

Generate pytest fixtures and fake devices that stand in for real hardware, so the safety
envelope and the execution primitives can be tested on a laptop with nothing plugged in.

## When to use

- A new driver needs tests.
- A schema or safety-limit change needs regression coverage.
- The `/execute` path changed and the JSON-RPC contract needs verifying end to end.

## Prerequisites

```bash
pip install pytest pytest-asyncio httpx
```

`pytest-asyncio` in strict mode: every async test carries `@pytest.mark.asyncio`.

## The core principle

**Mock the transport, not the driver.** A test that mocks the driver proves nothing about
the driver. Substitute the lowest layer — the byte-level link (serial port, socket, GPIO
handle) — and let the real driver logic, the real safety clamp, and the real registry run
against it.

```
test -> real /execute route -> real driver class -> FAKE transport
                                                    ^^^^ only this is fake
```

## Fixture layout

```
tests/
  conftest.py             # shared fixtures: fake_transport, registry, client
  fixtures/
    mock_temp_sensor.mhs  # valid capability tag
    bad_no_limits.mhs     # invalid: actuator with no safety limit
  test_schema.py          # every fixture tag validates (or fails as expected)
  test_registry.py        # register / discover / deregister
  test_execute.py         # JSON-RPC read + write happy paths
  test_safety.py          # limit violations rejected — the most important file here
```

## Fixture patterns

```python
@pytest.fixture
def capability_tag() -> dict:
    """Load a real tag from tests/fixtures — never inline a dict."""
    path = Path(__file__).parent / "fixtures" / "mock_temp_sensor.mhs"
    return json.loads(path.read_text())


@pytest.fixture
def fake_transport() -> FakeTransport:
    """Records every write; replays a scripted sequence of reads."""
    return FakeTransport(reads={"ambient_temp": [20.0, 20.5, 21.0]})


@pytest.fixture
async def client(registry) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(registry=registry))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
```

Fixtures must be **stateless between tests**. A mock device that accumulates state across
tests produces order-dependent passes — the worst kind of green.

## Required coverage for any driver

Every driver ships with tests for all of these. This is a checklist, not a suggestion:

- [ ] Loads its capability tag and the tag validates against the schema.
- [ ] `read()` on each declared sensor returns a value of the declared type and unit.
- [ ] `read()` on an undeclared target raises, not returns `None`.
- [ ] `write()` inside limits reaches the transport with the expected encoding.
- [ ] `write()` **below** `min` is rejected — assert on the error, and assert the transport
      recorded **zero** bytes.
- [ ] `write()` **above** `max` is rejected — same two assertions.
- [ ] `write()` exactly at `min` and exactly at `max` is accepted (bounds are inclusive).
- [ ] `write()` to a sensor id is rejected with `-32602` (invalid params).
- [ ] Transport failure surfaces as `-32002` (hardware execution error), not an unhandled
      exception.
- [ ] A transport that accepts a write but does not move surfaces as `-32003` (state desync).

## Rules

- **Assert the transport, not just the return value.** A rejected write that still emitted
  bytes is a safety failure that a return-value assertion will happily pass.
- Test the boundary values explicitly. Off-by-one on an inclusive bound is the single most
  likely safety bug in this codebase.
- Never `time.sleep()` for hardware settling. Inject a clock or await a driver-provided event.
- Never let a test touch a real serial port, socket, or device node. A test suite that can
  move a physical arm is a test suite that will move a physical arm.
- Parametrize across devices where the contract is shared:
  ```python
  @pytest.mark.parametrize("tag", ALL_FIXTURE_TAGS, ids=lambda t: t["device_id"])
  ```

## Red flags

| Thought | Reality |
|---|---|
| "I'll mock the driver's write method" | Then you tested your mock. Mock the transport. |
| "Testing both bounds is redundant" | Inclusive/exclusive bugs hide on exactly one side. |
| "Rejected write, test passes, done" | Also assert nothing was transmitted. |
| "I'll use the real serial port, it's unplugged" | Until it isn't. Never. |
