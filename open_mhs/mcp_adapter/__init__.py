"""MCP adapter: exposes an Open-MHS middleware instance as Model Context Protocol tools.

The adapter is a thin translation layer. It holds no device state, enforces no safety
limits of its own, and adds no capability the HTTP surface does not already have. Its one
job is to turn Open-MHS responses — successes and refusals alike — into text a language
model can act on.
"""

from open_mhs.mcp_adapter.client import (
    OpenMHSClient,
    OpenMHSUnreachable,
    RemoteRPCError,
    Unauthorized,
)

__all__ = ["OpenMHSClient", "OpenMHSUnreachable", "RemoteRPCError", "Unauthorized"]
