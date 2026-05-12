"""clangd backend subprocess lifecycle management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from .config_resolver import resolve_clangd_binary
from .jsonrpc import read_message, write_message

logger = logging.getLogger(__name__)

_SENTINEL: dict[str, Any] = {}  # Unique object used as EOF marker in the queue.


class BackendManager:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._message_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._binary: str = "clangd"
        self._compile_commands_dir: Path | None = None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def binary(self) -> str:
        return self._binary

    @property
    def compile_commands_dir(self) -> Path | None:
        return self._compile_commands_dir

    async def start(
        self,
        compile_commands_dir: Path,
        extra_args: list[str],
    ) -> None:
        binary = resolve_clangd_binary(compile_commands_dir)
        self._binary = binary
        self._compile_commands_dir = compile_commands_dir

        cmd = [
            binary,
            f"--compile-commands-dir={compile_commands_dir}",
            *extra_args,
        ]
        logger.info("Starting backend: %s", " ".join(cmd))

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Fresh queue so no stale messages from a previous backend leak in.
        self._message_queue = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
            # Guarantee the sentinel is always present: if the task was
            # cancelled before its body ran even once (Python 3.12+
            # skips the coroutine entirely, including finally blocks),
            # the sentinel put inside _read_loop never executes.
            self._message_queue.put_nowait(None)

        if self._process is not None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError):
                self._process.kill()
            self._process = None

    async def send(self, msg: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Backend is not running")
        await write_message(self._process.stdin, msg)

    async def receive(self) -> dict[str, Any] | None:
        """Return the next message from the backend, or None on EOF."""
        return await self._message_queue.get()

    async def _read_loop(self) -> None:
        # Capture the queue reference now: if start() replaces self._message_queue
        # before this task's finally block runs, the None sentinel still lands in
        # the correct (old) queue rather than the fresh one.
        queue = self._message_queue
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                msg = await read_message(self._process.stdout)
                await queue.put(msg)
        except (EOFError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception:
            logger.exception("Unexpected error reading from backend")
        finally:
            # Use put_nowait: the queue is unbounded, so this never blocks.
            # await queue.put(None) would raise CancelledError on Python 3.11+
            # because the task's cancel counter remains > 0 after catching
            # CancelledError in the except clause above.
            queue.put_nowait(None)
