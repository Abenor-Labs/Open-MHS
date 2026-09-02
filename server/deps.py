"""FastAPI dependencies.

Shared state is reached through `Depends`, never by importing a module-level global. That
is what lets a test build an app around its own isolated registry.
"""

from __future__ import annotations

from fastapi import Request

from server.audit import AuditLog
from server.registry import Registry


def get_registry(request: Request) -> Registry:
    """The registry bound to this app instance at creation time."""
    return request.app.state.registry


def get_audit(request: Request) -> AuditLog:
    """The audit log bound to this app instance."""
    return request.app.state.audit
