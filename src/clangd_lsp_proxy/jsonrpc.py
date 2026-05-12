"""LSP JSON-RPC 2.0 framing and typed message models."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from pydantic import BaseModel


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one LSP message from the stream."""
    headers: dict[str, str] = {}
    while True:
        raw = await reader.readline()
        if not raw:
            raise EOFError("Connection closed while reading headers")
        line = raw.decode("ascii").rstrip("\r\n")
        if not line:
            break
        key, _, value = line.partition(": ")
        headers[key] = value

    content_length = int(headers["Content-Length"])
    body = await reader.readexactly(content_length)
    return json.loads(body.decode("utf-8"))  # type: ignore[no-any-return]


def encode_message(msg: dict[str, Any]) -> bytes:
    """Encode a message with LSP Content-Length framing."""
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


async def write_message(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    """Write one LSP message to an asyncio StreamWriter."""
    writer.write(encode_message(msg))
    await writer.drain()


class ResponseError(BaseModel):
    code: int
    message: str
    data: Any = None


class RequestMessage(BaseModel):
    jsonrpc: Literal["2.0"]
    id: int | str
    method: str
    params: dict[str, Any] | list[Any] | None = None


class ResponseMessage(BaseModel):
    jsonrpc: Literal["2.0"]
    id: int | str | None
    result: Any = None
    error: ResponseError | None = None


class NotificationMessage(BaseModel):
    jsonrpc: Literal["2.0"]
    method: str
    params: dict[str, Any] | list[Any] | None = None
