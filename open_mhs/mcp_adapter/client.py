"""HTTP client for a running Open-MHS middleware instance.

Kept separate from the MCP tool definitions so the adapter can be pointed at a real server
over the network in production, and at an in-process ASGI app in tests, without either
knowing about the other.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from open_mhs.server.errors import INVALID_PARAMS


def _describe(params: dict[str, Any]) -> dict[str, Any]:
    """Params rendered safely for an error message, with unencodable values named."""
    out: dict[str, Any] = {}
    for key, value in params.items():
        try:
            json.dumps(value, allow_nan=False)
            out[key] = value
        except (ValueError, TypeError):
            out[key] = f"<unencodable {type(value).__name__}: {value!r}>"
    return out


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_S = 10.0
TOKEN_ENV_VAR = "OPEN_MHS_AUTH_TOKEN"


class OpenMHSUnreachable(Exception):
    """The middleware is not answering. Distinct from a device or safety failure."""

    def __init__(self, base_url: str, detail: str) -> None:
        self.base_url = base_url
        self.detail = detail
        super().__init__(f"{base_url}: {detail}")


class Unauthorized(Exception):
    """The middleware rejected our credentials.

    Separate from `OpenMHSUnreachable`: the server is up and answering, we simply are not
    allowed in. Conflating the two sends the operator to restart a healthy service.
    """

    def __init__(self, base_url: str, had_token: bool) -> None:
        self.base_url = base_url
        self.had_token = had_token
        super().__init__(f"{base_url}: 401 Unauthorized")


class RemoteRPCError(Exception):
    """The middleware answered with a JSON-RPC error object.

    Carries the full object, not just a message: `data` is where the violated bound, the
    rationale, and the list of legal alternatives live, and those are exactly what an agent
    needs in order to retry correctly.
    """

    def __init__(self, error: dict[str, Any]) -> None:
        self.code: int = error.get("code", 0)
        self.message: str = error.get("message", "")
        self.data: dict[str, Any] = error.get("data") or {}
        super().__init__(f"[{self.code}] {self.message}")


class OpenMHSClient:
    """Talks to one Open-MHS middleware instance.

    Args:
        base_url: middleware root. Defaults to `$OPEN_MHS_URL`, then localhost:8000.
        client: an existing httpx client to borrow. When supplied, this class never
            closes it — ownership stays with the caller.
    """

    def __init__(
        self,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        token: str | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("OPEN_MHS_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._client = client
        self._owned = client is None
        self._token = token if token is not None else os.getenv(TOKEN_ENV_VAR)

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def _headers(self) -> dict[str, str]:
        """Credentials for one request. Never logged, never echoed into tool output."""
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def __aenter__(self) -> OpenMHSClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
            self._owned = True
        return self._client

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def discover(self, device_type: str | None = None) -> dict[str, Any]:
        """GET /discover — devices with their capability tags inline."""
        params = {"type": device_type} if device_type else None
        try:
            response = await self._http().get(
                self._url("/discover"), params=params, headers=self._headers()
            )
            if response.status_code == 401:
                raise Unauthorized(self.base_url, self.has_token)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise OpenMHSUnreachable(self.base_url, str(exc)) from exc

    async def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """POST /rpc — returns the `result` member, or raises `RemoteRPCError`."""
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        # Serialised here rather than by the transport so that a value which cannot be
        # encoded — a NaN, an infinity, anything exotic a caller passed through — fails
        # as a clear error naming the value, instead of surfacing from inside the HTTP
        # client. An agent that sends `inf` deserves to be told that, not a traceback.
        try:
            encoded = json.dumps(body, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise RemoteRPCError({
                "code": INVALID_PARAMS,
                "message": f"{method}: the request could not be encoded as JSON ({exc})",
                "data": {"method": method, "params": _describe(params)},
            }) from exc

        try:
            response = await self._http().post(
                self._url("/rpc"),
                content=encoded,
                headers={**self._headers(), "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OpenMHSUnreachable(self.base_url, str(exc)) from exc

        if response.status_code == 401:
            raise Unauthorized(self.base_url, self.has_token)
        try:
            payload = response.json()
        except ValueError as exc:  # non-JSON body from something that is not Open-MHS
            raise OpenMHSUnreachable(
                self.base_url, f"expected a JSON-RPC response, got {response.text[:200]!r}"
            ) from exc

        if isinstance(payload, dict) and "error" in payload:
            raise RemoteRPCError(payload["error"])
        if not isinstance(payload, dict) or "result" not in payload:
            raise OpenMHSUnreachable(self.base_url, f"malformed JSON-RPC response: {payload!r}")
        return payload["result"]
