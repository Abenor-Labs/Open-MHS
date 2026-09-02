"""Open-MHS middleware application factory.

Registration and wiring only — no business logic lives here. Run it with:

    uvicorn server.main:app --reload

The default app self-registers the bundled mock devices so `/discover` and `/rpc` are
useful immediately with no hardware attached. Pass an explicit registry (as the tests do)
to get a clean instance with nothing loaded.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Sequence

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from open_mhs.server.audit import AuditLog
from open_mhs.server.auth import AuthPolicy, load_tokens, require_auth
from open_mhs.server.errors import (
    DEVICE_NOT_FOUND,
    HARDWARE_EXECUTION_ERROR,
    INVALID_PARAMS,
    SAFETY_LIMIT_VIOLATION,
    STATE_DESYNC,
    MHSError,
)
from open_mhs.server.models import LATEST_SPEC_VERSION, SUPPORTED_SPEC_VERSIONS, HealthResponse
from open_mhs.server.registry import Registry
from open_mhs.server.routers import discovery, rpc
from open_mhs.server.watchdog import Watchdog

log = logging.getLogger("open_mhs")

#: MHSError codes -> HTTP status, for the REST surface only. The JSON-RPC surface always
#: answers 200 with an error object, exactly as the JSON-RPC 2.0 specification requires.
HTTP_STATUS_FOR_CODE: dict[int, int] = {
    DEVICE_NOT_FOUND: 404,
    SAFETY_LIMIT_VIOLATION: 409,
    HARDWARE_EXECUTION_ERROR: 502,
    STATE_DESYNC: 409,
    INVALID_PARAMS: 400,
}


def load_mock_devices(registry: Registry) -> None:
    """Bind the bundled reference drivers so a fresh checkout has something to talk to."""
    from open_mhs.drivers.mock_pump import MockPump
    from open_mhs.drivers.mock_robotic_arm import MockRoboticArm
    from open_mhs.drivers.mock_temp_sensor import MockTempSensor

    for driver in (MockTempSensor(), MockRoboticArm(), MockPump()):
        registry.register(driver.tag, driver)
        log.info("registered mock device %s", driver.device_id)


def create_app(
    registry: Registry | None = None,
    *,
    load_mocks: bool | None = None,
    auth_token: str | Sequence[str] | None = None,
    audit_log: AuditLog | None = None,
) -> FastAPI:
    """Build an app around a registry.

    Passing a registry means the caller owns device lifecycle, so mocks are not loaded
    unless asked for explicitly. That is what keeps tests isolated from each other.

    Args:
        auth_token: accepted token(s). Falls back to `$OPEN_MHS_AUTH_TOKEN`. There is no
            way to disable authentication: with neither source configured this raises
            `AuthNotConfigured` and the app never exists. A server that can move a robotic
            arm must not come up unauthenticated because a variable was forgotten.
        audit_log: where commands and refusals are recorded. Defaults to
            `$OPEN_MHS_AUDIT_LOG`, then `open-mhs-audit.jsonl` in the working directory.
            Tests pass a temporary path.

    Raises:
        AuthNotConfigured: no token configured, or a token too short to be a secret.
    """
    own_registry = registry is None
    registry = registry or Registry()
    tokens = load_tokens(auth_token)
    if load_mocks is None:
        load_mocks = own_registry and os.getenv("OPEN_MHS_LOAD_MOCKS", "1") == "1"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if load_mocks:
            load_mock_devices(app.state.registry)
        yield
        app.state.watchdog.shutdown()
        app.state.registry.clear()

    app = FastAPI(
        title="Open-MHS Middleware",
        version=LATEST_SPEC_VERSION,
        summary="Vendor-neutral discovery and safe execution for AI-operated hardware.",
        description=(
            "Two primitives, one mutating surface. `read` observes; `write` commands an "
            "actuator and is checked against the device's declared `safety_limits` twice "
            "before any byte reaches hardware."
        ),
        lifespan=lifespan,
    )
    app.state.registry = registry
    app.state.auth = AuthPolicy(tokens=tokens)
    app.state.audit = audit_log if audit_log is not None else AuditLog.from_env()
    app.state.watchdog = Watchdog(app.state.audit)

    @app.exception_handler(MHSError)
    async def _mhs_error_handler(request: Request, exc: MHSError) -> JSONResponse:
        """Give the REST surface the same error objects the RPC surface uses."""
        return JSONResponse(
            status_code=HTTP_STATUS_FOR_CODE.get(exc.code, 500),
            content={"error": exc.to_rpc()},
        )

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    async def health() -> HealthResponse:
        """Liveness probe. The only unauthenticated endpoint.

        Deliberately says nothing about what is connected: an anonymous caller learns that
        the middleware is up and nothing else. Device counts live behind `/discover`.
        """
        return HealthResponse(
            status="ok",
            mhs_version=LATEST_SPEC_VERSION,
            supported_spec_versions=list(SUPPORTED_SPEC_VERSIONS),
        )

    # Everything that can see or touch hardware sits behind the token. Applied at the
    # router level rather than per-endpoint so a new route cannot be added unprotected by
    # forgetting a decorator.
    guard = [Depends(require_auth)]
    app.include_router(discovery.router, dependencies=guard)
    app.include_router(rpc.router, dependencies=guard)
    return app


def __getattr__(name: str) -> FastAPI:
    """Build the default app on first attribute access, not at import.

    `uvicorn server.main:app` still works and still refuses to start without a token. But
    importing this module — which a test, a driver, or a linter does routinely — must not
    require the deployment's secret to be present.
    """
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
