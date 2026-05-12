"""Tests for JSON-RPC 2.0 LSP framing."""

from __future__ import annotations

import asyncio
import json

import pytest

from clangd_lsp_proxy.jsonrpc import encode_message, read_message

from conftest import make_lsp_reader


def test_encode_message_content_length_header() -> None:
    data = encode_message({"a": 1})
    header, _, body = data.partition(b"\r\n\r\n")
    assert header == b"Content-Length: " + str(len(body)).encode()


def test_encode_message_body_is_compact_json() -> None:
    data = encode_message({"key": "value"})
    _, _, body = data.partition(b"\r\n\r\n")
    # No spaces around separators.
    assert b": " not in body
    assert b", " not in body
    # Valid JSON that round-trips.
    assert json.loads(body) == {"key": "value"}


async def test_encode_decode_roundtrip() -> None:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    reader = make_lsp_reader([msg])
    result = await read_message(reader)
    assert result == msg


async def test_read_message_eof_raises() -> None:
    reader = asyncio.StreamReader()
    reader.feed_eof()
    with pytest.raises(EOFError):
        await read_message(reader)


async def test_read_two_messages_in_sequence() -> None:
    msg1 = {"jsonrpc": "2.0", "method": "initialized", "params": {}}
    msg2 = {"jsonrpc": "2.0", "id": 2, "method": "shutdown"}
    reader = make_lsp_reader([msg1, msg2])
    r1 = await read_message(reader)
    r2 = await read_message(reader)
    assert r1 == msg1
    assert r2 == msg2


async def test_unicode_content_roundtrip() -> None:
    msg = {"jsonrpc": "2.0", "method": "test", "params": {"text": "héllo wörld 中文"}}
    reader = make_lsp_reader([msg])
    result = await read_message(reader)
    assert result["params"]["text"] == "héllo wörld 中文"
