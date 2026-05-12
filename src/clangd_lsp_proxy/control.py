"""Unix socket control plane server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ControlServer:
    """Accepts newline-delimited JSON requests on a Unix domain socket.

    Multiple clients (Neovim, MCP server) may connect concurrently.
    Each request/response pair is a single JSON object per line.
    """

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path
        self._handlers: dict[str, Handler] = {}
        self._server: asyncio.Server | None = None

    def register(self, method: str, handler: Handler) -> None:
        self._handlers[method] = handler

    async def start(self) -> None:
        # If a live proxy already owns this socket (same cwd → same hash),
        # run without a control plane rather than evicting it.
        if self._socket_path.exists():
            try:
                _r, writer = await asyncio.open_unix_connection(str(self._socket_path))
                writer.close()
                await writer.wait_closed()
                logger.info(
                    "Control socket %s already has a live listener; "
                    "running without control plane",
                    self._socket_path,
                )
                return
            except OSError:
                pass  # Stale socket — safe to overwrite.

        self._socket_path.unlink(missing_ok=True)
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self._socket_path),
        )
        logger.info("Control plane listening at %s", self._socket_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            # Only remove the socket file we created.
            self._socket_path.unlink(missing_ok=True)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    request: dict[str, Any] = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                req_id = request.get("id")
                method: str = request.get("method", "")
                params: dict[str, Any] = request.get("params") or {}

                handler = self._handlers.get(method)
                if handler is None:
                    response: dict[str, Any] = {
                        "id": req_id,
                        "error": {"message": f"Unknown method: {method!r}"},
                    }
                else:
                    try:
                        result = await handler(params)
                        response = {"id": req_id, "result": result}
                    except Exception as exc:
                        logger.exception("Handler %r raised", method)
                        response = {"id": req_id, "error": {"message": str(exc)}}

                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, asyncio.CancelledError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
