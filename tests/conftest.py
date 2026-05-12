"""Shared fixtures, stubs, and helpers for the clangd-lsp-proxy test suite."""

from __future__ import annotations

import asyncio
import io
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from clangd_lsp_proxy.document_store import DocumentStore
from clangd_lsp_proxy.jsonrpc import encode_message, read_message
from clangd_lsp_proxy.proxy import Proxy

# ---------------------------------------------------------------------------
# LSP message helpers
# ---------------------------------------------------------------------------


def make_lsp_message(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    id: int | str | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if id is not None:
        msg["id"] = id
    return msg


def make_response(
    id: int | str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result if result is not None else {}
    return msg


def make_lsp_reader(messages: list[dict[str, Any]]) -> asyncio.StreamReader:
    """Feed pre-encoded LSP messages into a StreamReader and return it."""
    reader = asyncio.StreamReader()
    for msg in messages:
        reader.feed_data(encode_message(msg))
    reader.feed_eof()
    return reader


def parse_lsp_messages(buf: io.BytesIO) -> list[dict[str, Any]]:
    """Parse all Content-Length-framed LSP messages from a BytesIO buffer."""
    buf.seek(0)
    results: list[dict[str, Any]] = []
    while True:
        # Read headers until blank line.
        content_length = 0
        while True:
            line = buf.readline()
            if not line:
                return results
            line = line.rstrip(b"\r\n")
            if not line:
                break
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":", 1)[1].strip())
        if content_length == 0:
            return results
        body = buf.read(content_length)
        if not body:
            return results
        results.append(json.loads(body))
    return results


# ---------------------------------------------------------------------------
# Fake clangd subprocess script
# ---------------------------------------------------------------------------

FAKE_CLANGD_SCRIPT = """\
import sys
import json

def read_msg():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.decode("ascii").rstrip("\\r\\n")
        if not line:
            break
        k, _, v = line.partition(": ")
        headers[k] = v
    n = int(headers.get("Content-Length", 0))
    body = sys.stdin.buffer.read(n)
    return json.loads(body) if body else None

def send_msg(obj):
    body = json.dumps(obj, separators=(",", ":")).encode()
    sys.stdout.buffer.write(
        b"Content-Length: " + str(len(body)).encode() + b"\\r\\n\\r\\n" + body
    )
    sys.stdout.buffer.flush()

while True:
    msg = read_msg()
    if msg is None:
        break
    if "id" not in msg:
        continue
    if msg.get("method") == "initialize":
        send_msg({
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {"capabilities": {"textDocumentSync": 2}},
        })
    elif "method" in msg:
        send_msg({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
"""


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class FakeBackendManager:
    """In-process stub for BackendManager."""

    def __init__(self, *, running: bool = True) -> None:
        self.is_running = running
        self.binary = "fake-clangd"
        self.compile_commands_dir: Path | None = None
        self.sent: list[dict[str, Any]] = []
        self._recv_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def feed(self, msg: dict[str, Any]) -> None:
        self._recv_queue.put_nowait(msg)

    def feed_eof(self) -> None:
        self._recv_queue.put_nowait(None)

    async def send(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)
        # Auto-respond to initialize so _replay_to_backend doesn't hang.
        if msg.get("method") == "initialize":
            init_id = msg.get("id")
            self._recv_queue.put_nowait(
                {"jsonrpc": "2.0", "id": init_id, "result": {"capabilities": {}}}
            )

    async def receive(self) -> dict[str, Any] | None:
        return await self._recv_queue.get()

    async def start(
        self, compile_commands_dir: Path, extra_args: list[str]
    ) -> None:
        self.compile_commands_dir = compile_commands_dir
        self.is_running = True

    async def stop(self) -> None:
        self.is_running = False


class FakeControlServer:
    """Minimal stub that records registered handlers without opening a socket."""

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register(self, method: str, handler: Any) -> None:
        self.handlers[method] = handler

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# make_proxy helper
# ---------------------------------------------------------------------------


def make_proxy(
    reader: asyncio.StreamReader,
    *,
    backend: FakeBackendManager | None = None,
    tmp_path: Path,
    stdout: io.BytesIO | None = None,
) -> tuple[Proxy, FakeBackendManager, DocumentStore]:
    if backend is None:
        backend = FakeBackendManager()
    control = FakeControlServer()
    docs = DocumentStore()
    proxy = Proxy(
        stdin_reader=reader,
        backend=backend,  # type: ignore[arg-type]
        control=control,  # type: ignore[arg-type]
        document_store=docs,
        extra_args=[],
        stdout=stdout,
    )
    return proxy, backend, docs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_socket(tmp_path: Path) -> Path:
    # macOS limits AF_UNIX paths to 104 chars. pytest tmp_path names can exceed
    # this when test function names are long. Use a short hash under /tmp instead.
    import hashlib

    digest = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:10]
    sock = Path(f"/tmp/pytest-{digest}.sock")
    yield sock  # type: ignore[misc]
    sock.unlink(missing_ok=True)


@pytest.fixture
def compile_commands_dir(tmp_path: Path) -> Path:
    cc = tmp_path / "compile_commands.json"
    cc.write_text(
        json.dumps([{"command": "/usr/bin/gcc -o main.o main.c", "file": "main.c"}])
    )
    return tmp_path


@pytest.fixture
def fake_clangd(tmp_path: Path) -> Path:
    script = tmp_path / "fake-clangd"
    script.write_text(f"#!{sys.executable}\n" + FAKE_CLANGD_SCRIPT)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture
def captured_stdout() -> io.BytesIO:
    return io.BytesIO()
