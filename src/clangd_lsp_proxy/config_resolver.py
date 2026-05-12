"""compile_commands.json discovery and clangd binary resolution."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def resolve_clangd_binary(compile_commands_dir: Path) -> str:
    """Return the appropriate clangd binary for the given compile_commands.json dir.

    Mirrors the logic in find_clangd_cmd() in lsp.lua:
    - /usr/bin compiler with no cross-compiler prefix  → PATH clangd
    - Custom absolute path or cross-compiler prefix    → try sibling clangd binary
    - Fallback                                         → PATH clangd
    """
    compile_commands = compile_commands_dir / "compile_commands.json"
    try:
        with compile_commands.open() as f:
            entries: list[dict[str, Any]] = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return "clangd"

    if not entries:
        return "clangd"

    first = entries[0]
    # Entries carry either "command" (string) or "arguments" (list).
    command_str: str = first.get("command") or " ".join(first.get("arguments", []))
    if not command_str:
        return "clangd"

    compiler_path = command_str.split()[0]
    if not compiler_path:
        return "clangd"

    is_abspath = compiler_path.startswith("/")
    compiler_path_obj = Path(compiler_path)
    has_dir = compiler_path_obj.parent != Path(".")
    compiler_dir = str(compiler_path_obj.parent) + "/" if has_dir else None
    compiler_name = compiler_path_obj.name

    # Detect cross-compiler prefix by searching for dashes in the filename only.
    last_dash = compiler_name.rfind("-")
    prefix = compiler_name[:last_dash] if last_dash != -1 else None

    # /usr/bin compiler with no cross-compiler prefix: use PATH clangd.
    if compiler_dir == "/usr/bin/" and prefix is None:
        return "clangd"

    # Custom absolute path or cross-compiler prefix: try sibling clangd.
    if is_abspath or prefix:
        if prefix:
            # /path/to/arm-none-eabi-gcc → /path/to/arm-none-eabi-clangd
            # arm-none-eabi-gcc          → arm-none-eabi-clangd
            candidate = (compiler_dir or "") + prefix + "-clangd"
        else:
            # /path/to/clang → /path/to/clangd
            candidate = (compiler_dir or "") + "clangd"

        if os.access(candidate, os.X_OK):
            return candidate

    return "clangd"


def find_compile_commands_dirs(root: Path) -> list[Path]:
    """Return directories containing compile_commands.json under root, sorted."""
    return sorted(p.parent for p in root.rglob("compile_commands.json"))
