"""Async client for the clangd-lsp-proxy control socket."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


async def control_request(
    socket_path: Path, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Send one request to the proxy control socket and return the result."""
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    request = json.dumps({"id": 1, "method": method, "params": params}) + "\n"
    writer.write(request.encode())
    await writer.drain()
    raw = await reader.readline()
    writer.close()
    await writer.wait_closed()
    response: dict[str, Any] = json.loads(raw)
    if "error" in response:
        raise RuntimeError(response["error"]["message"])
    result: dict[str, Any] = response["result"]
    return result
