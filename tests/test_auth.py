"""Authentication.

This surface actuates physical hardware, so the tests here assert two separate things:
that a valid token gets in, and that *every* route which can see or touch hardware is
closed without one. The second is enforced by walking the app's own route table rather
than by listing paths by hand — a route added later cannot quietly escape the guard.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from server.auth import ENV_VAR, MIN_TOKEN_LENGTH, AuthNotConfigured, AuthPolicy, load_tokens
from server.main import create_app
from server.registry import Registry
from tests.conftest import AUTH_HEADERS, TEST_TOKEN

WRONG_TOKEN = "wrong-token-also-long-enough-0123456789"

#: Routes that exist but are not part of the guarded surface.
PUBLIC_PATHS = {"/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


@pytest_asyncio.fixture
async def anon_client(registry: Registry):
    """A client that sends no credentials at all."""
    app = create_app(registry, load_mocks=False, auth_token=TEST_TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


# --------------------------------------------------------------------------------------
# Configuration is fail-safe
# --------------------------------------------------------------------------------------


def test_the_app_refuses_to_exist_without_a_token(monkeypatch) -> None:
    """Forgetting the variable must not produce an open server."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    with pytest.raises(AuthNotConfigured, match="will not start without authentication"):
        create_app(Registry(), load_mocks=False)


def test_an_empty_or_whitespace_token_counts_as_unset(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, "   ")
    with pytest.raises(AuthNotConfigured):
        create_app(Registry(), load_mocks=False)


def test_a_token_too_short_to_be_a_secret_is_refused() -> None:
    with pytest.raises(AuthNotConfigured, match=f"shorter than {MIN_TOKEN_LENGTH}"):
        create_app(Registry(), load_mocks=False, auth_token="hunter2")


def test_the_token_is_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, TEST_TOKEN)
    app = create_app(Registry(), load_mocks=False)
    assert app.state.auth.tokens == (TEST_TOKEN,)


def test_several_tokens_can_be_accepted_at_once_for_rotation() -> None:
    """Add the new token, migrate callers, drop the old one - without downtime."""
    app = create_app(
        Registry(), load_mocks=False, auth_token=f"{TEST_TOKEN},{WRONG_TOKEN}"
    )
    assert app.state.auth.verify(TEST_TOKEN)
    assert app.state.auth.verify(WRONG_TOKEN)


def test_load_tokens_strips_surrounding_whitespace() -> None:
    assert load_tokens(f"  {TEST_TOKEN} , {WRONG_TOKEN}  ") == (TEST_TOKEN, WRONG_TOKEN)


# --------------------------------------------------------------------------------------
# The policy itself
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("presented", [None, "", "   ", WRONG_TOKEN, TEST_TOKEN[:-1]])
def test_only_an_exact_token_verifies(presented) -> None:
    """A prefix of the real token is not the real token."""
    assert AuthPolicy((TEST_TOKEN,)).verify(presented) is False


def test_the_correct_token_verifies() -> None:
    assert AuthPolicy((TEST_TOKEN,)).verify(TEST_TOKEN) is True


# --------------------------------------------------------------------------------------
# Every hardware-facing route is closed
# --------------------------------------------------------------------------------------


def _guarded_routes(app) -> list[tuple[str, str]]:
    """(method, path) for every route that is not deliberately public."""
    found = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path in PUBLIC_PATHS or path is None:
            continue
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            found.append((method, path))
    return found


@pytest.mark.asyncio
async def test_every_hardware_facing_route_requires_a_token(anon_client) -> None:
    """Walks the route table so a future endpoint cannot be added unprotected."""
    routes = _guarded_routes(anon_client.app)
    assert routes, "no routes discovered - the guard test would be vacuous"

    for method, path in routes:
        url = path.replace("{device_id}", "arm-01")
        response = await anon_client.request(method, url, json={})
        assert response.status_code == 401, f"{method} {path} answered {response.status_code}"


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("GET", "/discover", None),
        ("POST", "/rpc", {"jsonrpc": "2.0", "id": 1, "method": "mhs.discover", "params": {}}),
        ("POST", "/execute", {"jsonrpc": "2.0", "id": 1, "method": "mhs.discover", "params": {}}),
        ("GET", "/devices/arm-01", None),
        ("DELETE", "/devices/arm-01", None),
        ("POST", "/devices/arm-01/heartbeat", None),
        ("POST", "/register", {}),
    ],
)
@pytest.mark.asyncio
async def test_named_endpoints_are_401_without_credentials(
    anon_client, method, path, body
) -> None:
    response = await anon_client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.asyncio
async def test_a_write_cannot_reach_hardware_without_a_token(anon_client, arm_device) -> None:
    """The point of the exercise: no token, no bytes."""
    response = await anon_client.post(
        "/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "mhs.write",
              "params": {"device_id": "arm-01", "target": "joint_1", "value": 45.0}},
    )
    assert response.status_code == 401
    assert arm_device.transport.writes == []


@pytest.mark.asyncio
async def test_a_wrong_token_is_rejected(anon_client, arm_device) -> None:
    response = await anon_client.get(
        "/discover", headers={"Authorization": f"Bearer {WRONG_TOKEN}"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_malformed_authorization_header_is_rejected(anon_client) -> None:
    for header in [TEST_TOKEN, f"Basic {TEST_TOKEN}", "Bearer", "Bearer    "]:
        response = await anon_client.get("/discover", headers={"Authorization": header})
        assert response.status_code == 401, header


@pytest.mark.asyncio
async def test_health_stays_public_so_liveness_probes_work(anon_client) -> None:
    response = await anon_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok", "mhs_version": "0.2", "supported_spec_versions": ["0.1", "0.2"],
    }


# --------------------------------------------------------------------------------------
# Accepted credential forms
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_token_is_accepted(anon_client) -> None:
    response = await anon_client.get("/discover", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json()["count"] == 3


@pytest.mark.asyncio
async def test_x_api_key_header_is_accepted(anon_client) -> None:
    """Devices and agents rarely share an HTTP client; both header forms work."""
    response = await anon_client.get("/discover", headers={"x-api-key": TEST_TOKEN})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_an_authenticated_write_still_works(client, arm_device) -> None:
    body = (
        await client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 1, "method": "mhs.write",
                  "params": {"device_id": "arm-01", "target": "joint_1", "value": 45.0}},
        )
    ).json()
    assert body["result"]["accepted"] is True
    assert arm_device.transport.writes == [("joint_1", 45.0)]


@pytest.mark.asyncio
async def test_a_device_registers_itself_with_its_token(empty_client, temp_device) -> None:
    result = await temp_device.register(empty_client, registry_url="", token=TEST_TOKEN)
    assert result["registered"] is True


@pytest.mark.asyncio
async def test_a_device_cannot_publish_a_capability_tag_anonymously(
    anon_client, temp_device
) -> None:
    """A tag declares its own safety limits. Anonymous callers must not get to publish one."""
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await temp_device.register(anon_client, registry_url="", token="")
    assert exc.value.response.status_code == 401


# --------------------------------------------------------------------------------------
# The secret does not leak
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_rejection_never_echoes_the_presented_token(anon_client) -> None:
    """An error page that reflects the attempt hands it to whatever is reading the logs."""
    response = await anon_client.get(
        "/discover", headers={"Authorization": f"Bearer {WRONG_TOKEN}"}
    )
    assert WRONG_TOKEN not in response.text
    assert TEST_TOKEN not in response.text


@pytest.mark.asyncio
async def test_missing_and_wrong_credentials_are_indistinguishable(anon_client) -> None:
    """Otherwise the response becomes an oracle for probing which tokens exist."""
    missing = await anon_client.get("/discover")
    wrong = await anon_client.get(
        "/discover", headers={"Authorization": f"Bearer {WRONG_TOKEN}"}
    )
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json()


def test_the_configured_token_is_not_in_the_openapi_document() -> None:
    app = create_app(Registry(), load_mocks=False, auth_token=TEST_TOKEN)
    assert TEST_TOKEN not in str(app.openapi())
