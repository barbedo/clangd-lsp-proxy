"""Core LSP proxy: routes messages between client and backend, handles switching."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path
from typing import IO, Any

from .backend import BackendManager
from .config_resolver import find_compile_commands_dirs, resolve_clangd_binary
from .control import ControlServer
from .document_store import DocumentStore
from .jsonrpc import encode_message, read_message

logger = logging.getLogger(__name__)

_ERR_BACKEND_SWITCHED = -32099


class _ClientWriter:
    """Serialised, lock-protected writer to the LSP client (stdout)."""

    def __init__(self, output: IO[bytes] | None = None) -> None:
        self._lock = asyncio.Lock()
        self._output = output

    async def write(self, msg: dict[str, Any]) -> None:
        data = encode_message(msg)
        async with self._lock:
            out = self._output if self._output is not None else sys.stdout.buffer
            out.write(data)
            out.flush()


class Proxy:
    def __init__(
        self,
        stdin_reader: asyncio.StreamReader,
        backend: BackendManager,
        control: ControlServer,
        document_store: DocumentStore,
        extra_args: list[str],
        stdout: IO[bytes] | None = None,
    ) -> None:
        self._reader = stdin_reader
        self._client = _ClientWriter(stdout)
        self._backend = backend
        self._docs = document_store
        self._extra_args = extra_args

        self._initialize_request: dict[str, Any] | None = None
        self._pending: dict[int | str, None] = {}
        self._switching = False
        self._switch_lock = asyncio.Lock()
        self._backend_loop_task: asyncio.Task[None] | None = None

        control.register("switch", self._handle_switch)
        control.register("status", self._handle_status)
        control.register("list_configs", self._handle_list_configs)

    async def run(self) -> None:
        self._backend_loop_task = asyncio.create_task(self._backend_loop())
        try:
            await self._client_loop()
        finally:
            if self._backend_loop_task and not self._backend_loop_task.done():
                self._backend_loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._backend_loop_task

    # ------------------------------------------------------------------
    # Client ← stdout loop
    # ------------------------------------------------------------------

    async def _backend_loop(self) -> None:
        while True:
            msg = await self._backend.receive()
            if msg is None:
                if self._switching:
                    # Backend was stopped deliberately; wait until a new one is ready.
                    while self._switching:
                        await asyncio.sleep(0.05)
                    continue
                logger.warning("Backend closed unexpectedly")
                await self._client.write(
                    {
                        "jsonrpc": "2.0",
                        "method": "window/showMessage",
                        "params": {
                            "type": 1,
                            "message": "clangd-lsp-proxy: backend closed unexpectedly",
                        },
                    }
                )
                break

            # Only pop pending for client-request responses. Server-initiated
            # requests (e.g. window/workDoneProgress/create) also carry an
            # "id", but in the server's own namespace — popping them would
            # silently discard valid client-initiated entries with the same
            # numeric ID.
            if "method" not in msg:
                msg_id = msg.get("id")
                if msg_id is not None:
                    self._pending.pop(msg_id, None)

            # Rewrite textDocumentSync to Full (1) in the initialize response.
            if "result" in msg and self._initialize_request is not None:
                result = msg.get("result")
                if isinstance(result, dict):
                    caps: Any = result.get("capabilities")
                    if isinstance(caps, dict):
                        sync = caps.get("textDocumentSync")
                        if isinstance(sync, dict):
                            sync["change"] = 1
                        elif isinstance(sync, int) and sync != 1:
                            caps["textDocumentSync"] = 1

            await self._client.write(msg)

    # ------------------------------------------------------------------
    # stdin → backend loop
    # ------------------------------------------------------------------

    async def _client_loop(self) -> None:
        while True:
            try:
                msg = await read_message(self._reader)
            except EOFError:
                logger.info("Client disconnected")
                break

            method: str = msg.get("method", "")
            msg_id: int | str | None = msg.get("id")

            if method == "initialize":
                self._initialize_request = msg
                # A new initialize means a new client session. Any IDs left
                # in _pending belong to the previous session whose callbacks
                # are already gone; clear them so _cancel_pending does not
                # send error responses for phantom IDs.
                self._pending.clear()
                await self._forward(msg, msg_id)

            elif method == "textDocument/didOpen":
                params: dict[str, Any] = msg.get("params") or {}
                self._docs.open(params)
                await self._forward(msg, None)

            elif method == "textDocument/didChange":
                params = msg.get("params") or {}
                self._docs.change(params)
                await self._forward(msg, None)

            elif method == "textDocument/didClose":
                params = msg.get("params") or {}
                uri: str = (params.get("textDocument") or {}).get("uri", "")
                if uri:
                    self._docs.close(uri)
                await self._forward(msg, None)

            elif method == "shutdown":
                self._switching = False  # Allow normal shutdown through.
                await self._forward(msg, msg_id)

            elif method == "exit":
                await self._forward(msg, None)
                break

            else:
                # Only track the ID for client-initiated requests (have a
                # method). Messages with an id but no method are client
                # responses to server-initiated requests (e.g. the response
                # to window/workDoneProgress/create). Those IDs live in the
                # server's namespace and must not be stored in _pending.
                await self._forward(msg, msg_id if method else None)

    async def _forward(self, msg: dict[str, Any], track_id: int | str | None) -> None:
        """Forward a message to the backend, tracking its id for cancellation."""
        if self._switching:
            if track_id is not None:
                await self._client.write(
                    {
                        "jsonrpc": "2.0",
                        "id": track_id,
                        "error": {
                            "code": _ERR_BACKEND_SWITCHED,
                            "message": "clangd-lsp-proxy: backend switching",
                        },
                    }
                )
            return
        if track_id is not None:
            self._pending[track_id] = None
        if self._backend.is_running:
            await self._backend.send(msg)

    # ------------------------------------------------------------------
    # Switch procedure
    # ------------------------------------------------------------------

    async def _cancel_pending(self) -> None:
        for req_id in list(self._pending):
            await self._client.write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": _ERR_BACKEND_SWITCHED,
                        "message": "clangd-lsp-proxy: backend switched",
                    },
                }
            )
        self._pending.clear()

    async def _replay_to_backend(self) -> None:
        if self._initialize_request is None:
            return

        init_id: int | str | None = self._initialize_request.get("id")
        await self._backend.send(self._initialize_request)

        # Consume messages until the initialize response arrives; discard others.
        while True:
            resp = await self._backend.receive()
            if resp is None:
                raise RuntimeError("Backend closed during replay")
            if resp.get("id") == init_id:
                break
            logger.debug(
                "Discarding backend message during replay: %s", resp.get("method")
            )

        await self._backend.send(
            {"jsonrpc": "2.0", "method": "initialized", "params": {}}
        )

        for doc in self._docs.all_open():
            await self._backend.send(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didOpen",
                    "params": {
                        "textDocument": {
                            "uri": doc.uri,
                            "languageId": doc.language_id,
                            "version": doc.version,
                            "text": doc.content,
                        },
                    },
                }
            )

    # ------------------------------------------------------------------
    # Control plane handlers
    # ------------------------------------------------------------------

    async def _handle_switch(self, params: dict[str, Any]) -> dict[str, Any]:
        async with self._switch_lock:
            dir_str: str = params.get("compile_commands_dir", "")
            if not dir_str:
                raise ValueError("compile_commands_dir is required")
            new_dir = Path(dir_str)
            if not (new_dir / "compile_commands.json").exists():
                raise FileNotFoundError(f"compile_commands.json not found in {new_dir}")

            self._switching = True

            # Pause the backend loop before draining the queue in _replay_to_backend.
            if self._backend_loop_task and not self._backend_loop_task.done():
                self._backend_loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._backend_loop_task

            await self._cancel_pending()
            await self._backend.stop()
            await self._backend.start(new_dir, self._extra_args)
            await self._replay_to_backend()

            self._switching = False
            self._backend_loop_task = asyncio.create_task(self._backend_loop())

            logger.info("Switched to %s using %s", new_dir, self._backend.binary)
            return {
                "binary": self._backend.binary,
                "compile_commands_dir": str(new_dir),
            }

    async def _handle_status(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "binary": self._backend.binary,
            "compile_commands_dir": str(self._backend.compile_commands_dir or ""),
            "open_documents": len(self._docs.all_open()),
            "pending_requests": len(self._pending),
            "backend_running": self._backend.is_running,
        }

    async def _handle_list_configs(self, _params: dict[str, Any]) -> dict[str, Any]:
        dirs = find_compile_commands_dirs(Path.cwd())
        configs = [{"dir": str(d), "binary": resolve_clangd_binary(d)} for d in dirs]
        return {
            "configs": configs,
            "current": str(self._backend.compile_commands_dir or ""),
        }
