"""Tests for Proxy message routing, document tracking, and backend switching."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest

from clangd_lsp_proxy.backend import BackendManager
from clangd_lsp_proxy.document_store import DocumentStore
from clangd_lsp_proxy.jsonrpc import encode_message, read_message
from clangd_lsp_proxy.proxy import Proxy

from conftest import (
    FakeBackendManager,
    FakeControlServer,
    make_lsp_message,
    make_lsp_reader,
    make_proxy,
    parse_lsp_messages,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ERR_BACKEND_SWITCHED = -32099


def _open_params(uri: str, text: str = "int x;") -> dict[str, Any]:
    return {
        "textDocument": {
            "uri": uri,
            "languageId": "cpp",
            "version": 1,
            "text": text,
        }
    }


def _change_params(uri: str, text: str, version: int = 2) -> dict[str, Any]:
    return {
        "textDocument": {"uri": uri, "version": version},
        "contentChanges": [{"text": text}],
    }


async def _run_loop(coro: Any, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        await coro


# ---------------------------------------------------------------------------
# _client_loop: document tracking
# ---------------------------------------------------------------------------


async def test_did_open_adds_to_document_store(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    reader = make_lsp_reader([
        make_lsp_message("textDocument/didOpen", _open_params("file:///a.cpp")),
    ])
    proxy, backend, docs = make_proxy(reader, tmp_path=tmp_path)
    await _run_loop(proxy._client_loop())
    assert len(docs.all_open()) == 1
    assert docs.all_open()[0].uri == "file:///a.cpp"


async def test_did_change_updates_document(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    reader = make_lsp_reader([
        make_lsp_message("textDocument/didOpen", _open_params("file:///a.cpp", "old")),
        make_lsp_message("textDocument/didChange", _change_params("file:///a.cpp", "new")),
    ])
    proxy, _, docs = make_proxy(reader, tmp_path=tmp_path)
    await _run_loop(proxy._client_loop())
    assert docs.all_open()[0].content == "new"


async def test_did_close_removes_document(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    reader = make_lsp_reader([
        make_lsp_message("textDocument/didOpen", _open_params("file:///a.cpp")),
        make_lsp_message("textDocument/didClose", {"textDocument": {"uri": "file:///a.cpp"}}),
    ])
    proxy, _, docs = make_proxy(reader, tmp_path=tmp_path)
    await _run_loop(proxy._client_loop())
    assert docs.all_open() == []


# ---------------------------------------------------------------------------
# _client_loop: initialize handling
# ---------------------------------------------------------------------------


async def test_initialize_stores_request(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    init_msg = make_lsp_message("initialize", {"capabilities": {}}, id=1)
    reader = make_lsp_reader([init_msg])
    proxy, _, _ = make_proxy(reader, tmp_path=tmp_path)
    await _run_loop(proxy._client_loop())
    assert proxy._initialize_request is not None
    assert proxy._initialize_request["method"] == "initialize"


async def test_initialize_clears_pending(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    init_msg = make_lsp_message("initialize", {"capabilities": {}}, id=1)
    reader = make_lsp_reader([init_msg])
    proxy, _, _ = make_proxy(reader, tmp_path=tmp_path)
    proxy._pending[99] = None  # Stale ID from a previous session.
    await _run_loop(proxy._client_loop())
    assert 99 not in proxy._pending


# ---------------------------------------------------------------------------
# _client_loop: pending request tracking
# ---------------------------------------------------------------------------


async def test_request_with_id_added_to_pending(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    backend = FakeBackendManager()
    # Use a backend that never delivers a response so the ID stays in _pending.
    req = make_lsp_message("textDocument/definition", {"textDocument": {"uri": "file:///a.cpp"}, "position": {"line": 0, "character": 0}}, id=5)
    reader = make_lsp_reader([req])
    proxy, _, _ = make_proxy(reader, backend=backend, tmp_path=tmp_path)
    await _run_loop(proxy._client_loop())
    assert 5 in proxy._pending


async def test_client_response_to_server_not_tracked(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    # Message with id but NO method: a client response to a server-initiated request.
    client_response = {"jsonrpc": "2.0", "id": 7, "result": {}}
    reader = make_lsp_reader([client_response])
    proxy, _, _ = make_proxy(reader, tmp_path=tmp_path)
    await _run_loop(proxy._client_loop())
    assert 7 not in proxy._pending


# ---------------------------------------------------------------------------
# _backend_loop: pending ID management
# ---------------------------------------------------------------------------


async def test_response_removes_id_from_pending(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    backend = FakeBackendManager()
    # Pre-load a backend response, then EOF.
    backend.feed({"jsonrpc": "2.0", "id": 3, "result": {"answer": 42}})
    backend.feed_eof()

    proxy, _, _ = make_proxy(make_lsp_reader([]), backend=backend, tmp_path=tmp_path)
    proxy._pending[3] = None
    proxy._initialize_request = make_lsp_message("initialize", {}, id=0)

    await _run_loop(proxy._backend_loop())
    assert 3 not in proxy._pending


async def test_server_initiated_request_does_not_pop_pending(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    backend = FakeBackendManager()
    # Server-initiated request: has BOTH method and id.
    backend.feed({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "window/workDoneProgress/create",
        "params": {},
    })
    backend.feed_eof()

    proxy, _, _ = make_proxy(make_lsp_reader([]), backend=backend, tmp_path=tmp_path)
    proxy._pending[3] = None
    proxy._initialize_request = make_lsp_message("initialize", {}, id=0)

    await _run_loop(proxy._backend_loop())
    assert 3 in proxy._pending  # Must NOT have been popped.


# ---------------------------------------------------------------------------
# _backend_loop: textDocumentSync rewriting
# ---------------------------------------------------------------------------


async def test_initialize_response_rewrites_sync_dict(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    backend = FakeBackendManager()
    backend.feed({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"capabilities": {"textDocumentSync": {"change": 3, "openClose": True}}},
    })
    backend.feed_eof()

    proxy, _, _ = make_proxy(make_lsp_reader([]), backend=backend, tmp_path=tmp_path, stdout=captured_stdout)
    proxy._initialize_request = make_lsp_message("initialize", {}, id=1)

    await _run_loop(proxy._backend_loop())
    msgs = parse_lsp_messages(captured_stdout)
    init_responses = [m for m in msgs if m.get("id") == 1]
    assert len(init_responses) == 1
    sync = init_responses[0]["result"]["capabilities"]["textDocumentSync"]
    assert sync["change"] == 1


async def test_initialize_response_rewrites_sync_int(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    backend = FakeBackendManager()
    backend.feed({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"capabilities": {"textDocumentSync": 2}},
    })
    backend.feed_eof()

    proxy, _, _ = make_proxy(make_lsp_reader([]), backend=backend, tmp_path=tmp_path, stdout=captured_stdout)
    proxy._initialize_request = make_lsp_message("initialize", {}, id=1)

    await _run_loop(proxy._backend_loop())
    msgs = parse_lsp_messages(captured_stdout)
    assert msgs[0]["result"]["capabilities"]["textDocumentSync"] == 1


# ---------------------------------------------------------------------------
# _cancel_pending
# ---------------------------------------------------------------------------


async def test_cancel_pending_sends_errors_and_clears(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    proxy, _, _ = make_proxy(make_lsp_reader([]), tmp_path=tmp_path, stdout=captured_stdout)
    proxy._pending[1] = None
    proxy._pending[2] = None
    proxy._pending["str-id"] = None

    await proxy._cancel_pending()

    assert proxy._pending == {}
    msgs = parse_lsp_messages(captured_stdout)
    assert len(msgs) == 3
    ids = {m["id"] for m in msgs}
    assert ids == {1, 2, "str-id"}
    for m in msgs:
        assert m["error"]["code"] == _ERR_BACKEND_SWITCHED


# ---------------------------------------------------------------------------
# _forward during switching
# ---------------------------------------------------------------------------


async def test_switching_drops_notification(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    backend = FakeBackendManager()
    proxy, _, _ = make_proxy(make_lsp_reader([]), backend=backend, tmp_path=tmp_path, stdout=captured_stdout)
    proxy._switching = True
    notification = make_lsp_message("textDocument/didOpen", _open_params("file:///a.cpp"))
    await proxy._forward(notification, None)
    assert backend.sent == []
    assert captured_stdout.tell() == 0


async def test_switching_returns_error_for_request(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    backend = FakeBackendManager()
    proxy, _, _ = make_proxy(make_lsp_reader([]), backend=backend, tmp_path=tmp_path, stdout=captured_stdout)
    proxy._switching = True
    request = make_lsp_message("textDocument/hover", {}, id=9)
    await proxy._forward(request, 9)
    assert backend.sent == []
    msgs = parse_lsp_messages(captured_stdout)
    assert len(msgs) == 1
    assert msgs[0]["id"] == 9
    assert msgs[0]["error"]["code"] == _ERR_BACKEND_SWITCHED


# ---------------------------------------------------------------------------
# _replay_to_backend
# ---------------------------------------------------------------------------


async def test_replay_sends_initialize_initialized_and_docs(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    backend = FakeBackendManager()
    docs = DocumentStore()
    docs.open({"textDocument": {"uri": "file:///a.cpp", "languageId": "cpp", "version": 1, "text": "int a;"}})
    docs.open({"textDocument": {"uri": "file:///b.cpp", "languageId": "cpp", "version": 1, "text": "int b;"}})

    control = FakeControlServer()
    init_msg = make_lsp_message("initialize", {"capabilities": {}}, id=42)
    proxy = Proxy(
        stdin_reader=make_lsp_reader([]),
        backend=backend,  # type: ignore[arg-type]
        control=control,  # type: ignore[arg-type]
        document_store=docs,
        extra_args=[],
    )
    proxy._initialize_request = init_msg

    await asyncio.wait_for(proxy._replay_to_backend(), timeout=2.0)

    methods = [m.get("method") for m in backend.sent]
    assert methods[0] == "initialize"
    assert methods[1] == "initialized"
    # Remaining entries are didOpen messages.
    did_opens = [m for m in backend.sent if m.get("method") == "textDocument/didOpen"]
    uris = {m["params"]["textDocument"]["uri"] for m in did_opens}
    assert uris == {"file:///a.cpp", "file:///b.cpp"}


async def test_replay_discards_spurious_messages(
    captured_stdout: io.BytesIO, tmp_path: Path
) -> None:
    """A message arriving before the initialize response must be discarded."""
    backend = FakeBackendManager()
    # Override auto-respond so we control what arrives.
    backend._recv_queue = asyncio.Queue()
    backend._recv_queue.put_nowait({"jsonrpc": "2.0", "method": "$/progress", "params": {}})
    backend._recv_queue.put_nowait({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}})

    control = FakeControlServer()
    init_msg = make_lsp_message("initialize", {"capabilities": {}}, id=1)
    proxy = Proxy(
        stdin_reader=make_lsp_reader([]),
        backend=backend,  # type: ignore[arg-type]
        control=control,  # type: ignore[arg-type]
        document_store=DocumentStore(),
        extra_args=[],
    )
    proxy._initialize_request = init_msg

    # The spurious progress message must be discarded; replay must complete.
    await asyncio.wait_for(proxy._replay_to_backend(), timeout=2.0)
    # Only the initialize was sent (no open docs).
    assert backend.sent[0]["method"] == "initialize"
    assert backend.sent[1]["method"] == "initialized"


# ---------------------------------------------------------------------------
# Integration: end-to-end with fake_clangd subprocess
# ---------------------------------------------------------------------------


async def test_initialize_forwarded_and_rewritten(
    captured_stdout: io.BytesIO,
    fake_clangd: Path,
    compile_commands_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "clangd_lsp_proxy.backend.resolve_clangd_binary",
        lambda _dir: str(fake_clangd),
    )
    backend = BackendManager()
    await backend.start(compile_commands_dir, [])

    init_msg = make_lsp_message("initialize", {"capabilities": {}}, id=1)
    shutdown = make_lsp_message("shutdown", id=2)
    exit_msg = make_lsp_message("exit")
    reader = make_lsp_reader([init_msg, shutdown, exit_msg])

    control = FakeControlServer()
    docs = DocumentStore()
    proxy = Proxy(
        stdin_reader=reader,
        backend=backend,
        control=control,  # type: ignore[arg-type]
        document_store=docs,
        extra_args=[],
        stdout=captured_stdout,
    )

    async with asyncio.timeout(5.0):
        # Run backend loop as a separate task so it stays alive after client loop exits.
        proxy._backend_loop_task = asyncio.create_task(proxy._backend_loop())
        await proxy._client_loop()
        # Wait for all pending requests (initialize id=1, shutdown id=2) to get responses.
        while proxy._pending:
            await asyncio.sleep(0.01)

    # Stop backend subprocess — puts sentinel in queue so backend loop can exit cleanly.
    await backend.stop()
    await asyncio.wait_for(proxy._backend_loop_task, timeout=2.0)

    msgs = parse_lsp_messages(captured_stdout)
    init_responses = [m for m in msgs if m.get("id") == 1]
    assert init_responses, "No initialize response found in stdout"
    caps = init_responses[0]["result"]["capabilities"]
    # Proxy must have rewritten textDocumentSync to Full (1).
    sync = caps.get("textDocumentSync")
    assert sync == 1 or (isinstance(sync, dict) and sync.get("change") == 1)


async def test_switch_replays_open_documents(
    fake_clangd: Path,
    compile_commands_dir: Path,
    captured_stdout: io.BytesIO,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "clangd_lsp_proxy.backend.resolve_clangd_binary",
        lambda _dir: str(fake_clangd),
    )
    backend = BackendManager()
    await backend.start(compile_commands_dir, [])

    docs = DocumentStore()
    docs.open({"textDocument": {"uri": "file:///main.cpp", "languageId": "cpp", "version": 1, "text": "int main() {}"}})

    control = FakeControlServer()
    proxy = Proxy(
        stdin_reader=make_lsp_reader([]),
        backend=backend,
        control=control,  # type: ignore[arg-type]
        document_store=docs,
        extra_args=[],
    )
    proxy._initialize_request = make_lsp_message("initialize", {"capabilities": {}}, id=1)

    # A second compile_commands.json dir for the switch target.
    new_dir = tmp_path / "new_build"
    new_dir.mkdir()
    (new_dir / "compile_commands.json").write_text(
        '[{"command": "/usr/bin/gcc -c main.c", "file": "main.c"}]'
    )

    async with asyncio.timeout(5.0):
        result = await proxy._handle_switch({"compile_commands_dir": str(new_dir)})

    assert result["compile_commands_dir"] == str(new_dir)
    assert backend.compile_commands_dir == new_dir

    await backend.stop()
