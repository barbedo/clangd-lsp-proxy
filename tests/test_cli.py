"""Tests for CLI helper functions."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from clangd_lsp_proxy.cli import (
    _auto_compile_commands_dir,
    _default_socket_path,
    _discover_socket,
    _runtime_dir,
)

# ---------------------------------------------------------------------------
# _runtime_dir
# ---------------------------------------------------------------------------


def test_runtime_dir_uses_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.delenv("TMPDIR", raising=False)
    assert _runtime_dir() == Path("/run/user/1000")


def test_runtime_dir_uses_tmpdir_when_no_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", "/var/tmp/user")
    assert _runtime_dir() == Path("/var/tmp/user")


def test_runtime_dir_falls_back_to_slash_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    assert _runtime_dir() == Path("/tmp")


# ---------------------------------------------------------------------------
# _default_socket_path
# ---------------------------------------------------------------------------


def test_default_socket_path_stability() -> None:
    cwd = Path("/some/project")
    assert _default_socket_path(cwd) == _default_socket_path(cwd)


def test_default_socket_path_uses_runtime_dir_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("TMPDIR", raising=False)
    result = _default_socket_path(Path("/my/project"))
    assert str(result).startswith(str(tmp_path))


def test_default_socket_path_different_cwds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    p1 = _default_socket_path(Path("/project/a"))
    p2 = _default_socket_path(Path("/project/b"))
    assert p1 != p2


def test_default_socket_path_contains_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    cwd = Path("/test/project")
    expected_hash = hashlib.sha256(f"{cwd}:{os.getpgrp()}".encode()).hexdigest()[:8]
    result = _default_socket_path(cwd)
    assert expected_hash in result.name


# ---------------------------------------------------------------------------
# _auto_compile_commands_dir
# ---------------------------------------------------------------------------


def test_auto_finds_build_subdirectory(tmp_path: Path) -> None:
    build = tmp_path / "build"
    build.mkdir()
    (build / "compile_commands.json").write_text("[]")
    assert _auto_compile_commands_dir(tmp_path) == build


def test_auto_falls_back_to_root(tmp_path: Path) -> None:
    (tmp_path / "compile_commands.json").write_text("[]")
    assert _auto_compile_commands_dir(tmp_path) == tmp_path


def test_auto_prefers_build_over_root(tmp_path: Path) -> None:
    (tmp_path / "compile_commands.json").write_text("[]")
    build = tmp_path / "build"
    build.mkdir()
    (build / "compile_commands.json").write_text("[]")
    assert _auto_compile_commands_dir(tmp_path) == build


def test_auto_returns_none_when_missing(tmp_path: Path) -> None:
    assert _auto_compile_commands_dir(tmp_path) is None


# ---------------------------------------------------------------------------
# _discover_socket
# ---------------------------------------------------------------------------


def test_discover_socket_returns_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("TMPDIR", raising=False)
    cwd = Path("/some/nonexistent/project")
    assert _discover_socket(cwd) is None


def test_discover_socket_returns_path_when_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("TMPDIR", raising=False)
    cwd = Path("/my/project")
    digest = hashlib.sha256(f"{cwd}:{os.getpgrp()}".encode()).hexdigest()[:8]
    sock_dir = tmp_path / "clangd-lsp-proxy"
    sock_dir.mkdir(parents=True)
    sock_file = sock_dir / f"{digest}.sock"
    sock_file.touch()
    result = _discover_socket(cwd)
    assert result == sock_file
