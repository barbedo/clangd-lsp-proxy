"""Tests for compile_commands.json discovery and clangd binary resolution."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from clangd_lsp_proxy.config_resolver import find_compile_commands_dirs, resolve_clangd_binary


def _write_cc(directory: Path, command: str) -> None:
    (directory / "compile_commands.json").write_text(
        json.dumps([{"command": command, "file": "main.c"}])
    )


def _make_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# resolve_clangd_binary
# ---------------------------------------------------------------------------


def test_usr_bin_gcc_no_prefix_returns_clangd(tmp_path: Path) -> None:
    _write_cc(tmp_path, "/usr/bin/gcc -c main.c")
    assert resolve_clangd_binary(tmp_path) == "clangd"


def test_absolute_path_sibling_clangd_exists(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_executable(bin_dir / "clangd")
    _write_cc(tmp_path, f"{bin_dir}/clang -c main.c")
    assert resolve_clangd_binary(tmp_path) == str(bin_dir / "clangd")


def test_absolute_path_sibling_clangd_missing(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_cc(tmp_path, f"{bin_dir}/clang -c main.c")
    assert resolve_clangd_binary(tmp_path) == "clangd"


def test_cross_compiler_prefix_absolute_sibling_exists(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_executable(bin_dir / "arm-none-eabi-clangd")
    _write_cc(tmp_path, f"{bin_dir}/arm-none-eabi-gcc -c main.c")
    assert resolve_clangd_binary(tmp_path) == str(bin_dir / "arm-none-eabi-clangd")


def test_cross_compiler_relative_no_sibling(tmp_path: Path) -> None:
    _write_cc(tmp_path, "arm-none-eabi-gcc -c main.c")
    assert resolve_clangd_binary(tmp_path) == "clangd"


def test_missing_compile_commands_json(tmp_path: Path) -> None:
    assert resolve_clangd_binary(tmp_path) == "clangd"


def test_empty_compile_commands_json(tmp_path: Path) -> None:
    (tmp_path / "compile_commands.json").write_text("[]")
    assert resolve_clangd_binary(tmp_path) == "clangd"


def test_malformed_json_returns_clangd(tmp_path: Path) -> None:
    (tmp_path / "compile_commands.json").write_text("not valid json {")
    assert resolve_clangd_binary(tmp_path) == "clangd"


def test_arguments_array_instead_of_command(tmp_path: Path) -> None:
    (tmp_path / "compile_commands.json").write_text(
        json.dumps([{"arguments": ["/usr/bin/g++", "-c", "main.c"], "file": "main.c"}])
    )
    assert resolve_clangd_binary(tmp_path) == "clangd"


# ---------------------------------------------------------------------------
# find_compile_commands_dirs
# ---------------------------------------------------------------------------


def test_find_single_dir(tmp_path: Path) -> None:
    build = tmp_path / "build"
    build.mkdir()
    (build / "compile_commands.json").write_text("[]")
    assert find_compile_commands_dirs(tmp_path) == [build]


def test_find_multiple_dirs_sorted(tmp_path: Path) -> None:
    dirs = [tmp_path / name for name in ("zzz", "aaa", "mmm")]
    for d in dirs:
        d.mkdir()
        (d / "compile_commands.json").write_text("[]")
    result = find_compile_commands_dirs(tmp_path)
    assert result == sorted(dirs)


def test_find_no_dirs_returns_empty(tmp_path: Path) -> None:
    assert find_compile_commands_dirs(tmp_path) == []
