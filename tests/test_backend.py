"""Tests for the clangd backend subprocess lifecycle."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from clangd_lsp_proxy.backend import BackendManager


# ---------------------------------------------------------------------------
# BackendManager (async, using fake_clangd fixture)
# ---------------------------------------------------------------------------


@pytest.fixture
def backend_with_fake_clangd(
    fake_clangd: Path, compile_commands_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> BackendManager:
    monkeypatch.setattr(
        "clangd_lsp_proxy.backend.resolve_clangd_binary",
        lambda _dir: str(fake_clangd),
    )
    return BackendManager()


async def test_backend_starts(
    backend_with_fake_clangd: BackendManager,
    compile_commands_dir: Path,
    tmp_path: Path,
) -> None:
    backend = backend_with_fake_clangd
    await backend.start(compile_commands_dir, [])
    assert backend.is_running
    await backend.stop()


async def test_backend_stop_terminates(
    backend_with_fake_clangd: BackendManager,
    compile_commands_dir: Path,
    tmp_path: Path,
) -> None:
    backend = backend_with_fake_clangd
    await backend.start(compile_commands_dir, [])
    await backend.stop()
    assert not backend.is_running


async def test_backend_send_and_receive(
    backend_with_fake_clangd: BackendManager,
    compile_commands_dir: Path,
    tmp_path: Path,
) -> None:
    backend = backend_with_fake_clangd
    await backend.start(compile_commands_dir, [])
    init_msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    await backend.send(init_msg)
    response = await asyncio.wait_for(backend.receive(), timeout=3.0)
    assert response is not None
    assert "result" in response
    assert "capabilities" in response["result"]
    await backend.stop()


async def test_backend_receive_returns_none_after_stop(
    backend_with_fake_clangd: BackendManager,
    compile_commands_dir: Path,
    tmp_path: Path,
) -> None:
    backend = backend_with_fake_clangd
    await backend.start(compile_commands_dir, [])
    await backend.stop()
    sentinel = await asyncio.wait_for(backend.receive(), timeout=3.0)
    assert sentinel is None


async def test_backend_compile_commands_dir_set_after_start(
    backend_with_fake_clangd: BackendManager,
    compile_commands_dir: Path,
    tmp_path: Path,
) -> None:
    backend = backend_with_fake_clangd
    await backend.start(compile_commands_dir, [])
    assert backend.compile_commands_dir == compile_commands_dir
    await backend.stop()


async def test_backend_binary_set_after_start(
    backend_with_fake_clangd: BackendManager,
    fake_clangd: Path,
    compile_commands_dir: Path,
    tmp_path: Path,
) -> None:
    backend = backend_with_fake_clangd
    await backend.start(compile_commands_dir, [])
    assert backend.binary == str(fake_clangd)
    await backend.stop()
