"""Tests for the control socket async client."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from clangd_lsp_proxy._client import control_request


async def test_control_request_returns_result(tmp_socket: Path) -> None:
    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.readline()
        req = json.loads(raw)
        resp = json.dumps({"id": req["id"], "result": {"ok": True}}) + "\n"
        writer.write(resp.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(_handler, path=str(tmp_socket))
    async with server:
        result = await asyncio.wait_for(
            control_request(tmp_socket, "ping", {"x": 1}), timeout=2.0
        )
    assert result == {"ok": True}


async def test_control_request_sends_correct_fields(tmp_socket: Path) -> None:
    received: dict = {}

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.readline()
        received.update(json.loads(raw))
        resp = json.dumps({"id": received["id"], "result": {}}) + "\n"
        writer.write(resp.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(_handler, path=str(tmp_socket))
    async with server:
        await asyncio.wait_for(
            control_request(tmp_socket, "mymethod", {"key": "val"}), timeout=2.0
        )
    assert received["method"] == "mymethod"
    assert received["params"] == {"key": "val"}
    assert "id" in received


async def test_control_request_raises_on_error_response(tmp_socket: Path) -> None:
    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.readline()
        req = json.loads(raw)
        resp = json.dumps({"id": req["id"], "error": {"message": "oops"}}) + "\n"
        writer.write(resp.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(_handler, path=str(tmp_socket))
    async with server:
        with pytest.raises(RuntimeError, match="oops"):
            await asyncio.wait_for(
                control_request(tmp_socket, "fail", {}), timeout=2.0
            )
