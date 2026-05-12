"""MCP companion server: exposes clangd-lsp-proxy control plane as MCP tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from ._client import control_request

_NOT_RUNNING = (
    "clangd-lsp-proxy is not running. "
    "Open a C/C++ file first to start the LSP server, then retry."
)


async def _call(socket_path: Path, method: str, params: dict) -> dict:  # type: ignore[type-arg]
    try:
        return await control_request(socket_path, method, params)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as err:
        raise RuntimeError(_NOT_RUNNING) from err


def create_mcp_server(socket_path: Path) -> FastMCP:
    mcp: FastMCP = FastMCP(
        "clangd-lsp-proxy",
        instructions=(
            "Tools for switching the active clangd backend in a running "
            "clangd-lsp-proxy instance. Use list_clangd_configs to discover "
            "available build configurations, then switch_clangd_config to "
            "activate one."
        ),
    )

    @mcp.tool()
    async def switch_clangd_config(compile_commands_dir: str) -> dict[str, Any]:
        """Switch the active clangd backend to a different compile_commands.json dir.

        The proxy restarts clangd with the appropriate binary for that build
        configuration and replays all open documents to the new backend.
        Returns the resolved clangd binary and the active directory.
        """
        return await _call(
            socket_path,
            "switch",
            {"compile_commands_dir": compile_commands_dir},
        )

    @mcp.tool()
    async def get_clangd_status() -> dict[str, Any]:
        """Return the current clangd-lsp-proxy status.

        Includes the active binary, compile_commands directory, number of
        open documents tracked by the proxy, and whether the backend is running.
        """
        return await _call(socket_path, "status", {})

    @mcp.tool()
    async def list_clangd_configs() -> dict[str, Any]:
        """Discover all compile_commands.json files in the project.

        Returns each directory and the clangd binary that would be selected
        for it, plus the currently active directory.
        """
        return await _call(socket_path, "list_configs", {})

    return mcp
