"""Tests for the Unix socket control plane server."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from clangd_lsp_proxy.control import ControlServer


async def _send_request(sock: Path, method: str, params: dict | None = None) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(sock))
    request = json.dumps({"id": 1, "method": method, "params": params or {}}) + "\n"
    writer.write(request.encode())
    await writer.drain()
    raw = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return json.loads(raw)


async def test_server_starts_and_creates_socket(tmp_socket: Path) -> None:
    server = ControlServer(tmp_socket)
    await server.start()
    assert tmp_socket.exists()
    await server.stop()


async def test_server_stop_deletes_socket(tmp_socket: Path) -> None:
    server = ControlServer(tmp_socket)
    await server.start()
    await server.stop()
    assert not tmp_socket.exists()


async def test_registered_handler_returns_result(tmp_socket: Path) -> None:
    server = ControlServer(tmp_socket)
    server.register("ping", lambda _params: asyncio.coroutine(lambda: {"pong": True})())

    async def _handler(_params: dict) -> dict:
        return {"pong": True}

    server.register("ping", _handler)
    await server.start()
    try:
        response = await asyncio.wait_for(_send_request(tmp_socket, "ping"), timeout=2.0)
        assert response["result"] == {"pong": True}
        assert response["id"] == 1
    finally:
        await server.stop()


async def test_unknown_method_returns_error(tmp_socket: Path) -> None:
    server = ControlServer(tmp_socket)
    await server.start()
    try:
        response = await asyncio.wait_for(
            _send_request(tmp_socket, "unknown_method"), timeout=2.0
        )
        assert "error" in response
        assert "Unknown method" in response["error"]["message"]
    finally:
        await server.stop()


async def test_handler_exception_returns_error(tmp_socket: Path) -> None:
    server = ControlServer(tmp_socket)

    async def _bad(_params: dict) -> dict:
        raise ValueError("boom")

    async def _ok(_params: dict) -> dict:
        return {"status": "ok"}

    server.register("bad", _bad)
    server.register("ok", _ok)
    await server.start()
    try:
        resp_bad = await asyncio.wait_for(_send_request(tmp_socket, "bad"), timeout=2.0)
        assert "error" in resp_bad
        assert "boom" in resp_bad["error"]["message"]
        # Server must still handle subsequent requests.
        resp_ok = await asyncio.wait_for(_send_request(tmp_socket, "ok"), timeout=2.0)
        assert resp_ok["result"] == {"status": "ok"}
    finally:
        await server.stop()


async def test_second_start_with_live_socket_skips_bind(tmp_socket: Path) -> None:
    server_a = ControlServer(tmp_socket)
    await server_a.start()
    assert server_a._server is not None

    server_b = ControlServer(tmp_socket)
    await server_b.start()
    # B detected a live socket — it must NOT have created its own server.
    assert server_b._server is None

    # Stopping B must not delete A's socket.
    await server_b.stop()
    assert tmp_socket.exists()

    await server_a.stop()
    assert not tmp_socket.exists()


async def test_stale_socket_file_is_overwritten(tmp_socket: Path) -> None:
    # Create a plain file (not a real socket) at the socket path.
    tmp_socket.parent.mkdir(parents=True, exist_ok=True)
    tmp_socket.write_text("stale")

    server = ControlServer(tmp_socket)
    await server.start()
    assert server._server is not None
    await server.stop()


async def test_multiple_concurrent_clients(tmp_socket: Path) -> None:
    barrier = asyncio.Event()

    async def _slow(_params: dict) -> dict:
        await barrier.wait()
        return {"done": True}

    server = ControlServer(tmp_socket)
    server.register("slow", _slow)
    await server.start()
    try:
        t1 = asyncio.create_task(_send_request(tmp_socket, "slow"))
        t2 = asyncio.create_task(_send_request(tmp_socket, "slow"))
        await asyncio.sleep(0)  # Let both connections establish.
        barrier.set()
        r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=3.0)
        assert r1["result"] == {"done": True}
        assert r2["result"] == {"done": True}
    finally:
        await server.stop()
