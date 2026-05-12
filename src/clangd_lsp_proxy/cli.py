"""Entry points for clangd-lsp-proxy, clangd-lsp-proxy-mcp, and clangd-lsp-proxy-ctl."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

import cyclopts

from ._client import control_request
from .backend import BackendManager
from .control import ControlServer
from .document_store import DocumentStore
from .mcp_server import create_mcp_server
from .proxy import Proxy

# ---------------------------------------------------------------------------
# clangd-lsp-proxy
# ---------------------------------------------------------------------------

proxy_app = cyclopts.App(
    name="clangd-lsp-proxy",
    help="LSP proxy for clangd with runtime backend switching via a Unix socket.",
)


def _runtime_dir() -> Path:
    for var in ("XDG_RUNTIME_DIR", "TMPDIR"):
        val = os.environ.get(var)
        if val:
            return Path(val)
    return Path("/tmp")


# Processes that are transient launchers rather than persistent editor tools.
# When the PGID leader's name matches one of these, its PPID (the real tool)
# is used as the identifier. Otherwise the PGID leader itself is the tool.
_LAUNCHERS = frozenset({
    "uvx", "uv",
    "sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh",
    "python", "python3",
    "python3.10", "python3.11", "python3.12", "python3.13", "python3.14",
})


def _tool_pid() -> int:
    """Return the PID of the parent tool (editor) that launched us.

    Neovim uses jobstart() for the proxy (creates a new PGID, leader = uvx)
    but vim.system() for the ctl (inherits nvim's PGID, leader = nvim itself).
    We resolve this by inspecting the PGID leader's command name:
    - If it is a known launcher (uvx, sh, python…) its PPID is the real tool.
    - Otherwise the PGID leader itself is the tool (e.g. nvim, claude).
    Claude Code spawns children without changing process groups, so the PGID
    leader is always the tool → the "else" branch handles it.
    The result is the same PID for both proxy and ctl launched by the same
    tool instance, and different for distinct tool instances.
    """
    pgid = os.getpgrp()
    if pgid == os.getpid():
        return os.getppid()
    try:
        r = subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(pgid)],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().split(None, 1)
            ppid_of_pgid = int(parts[0])
            comm = parts[1].strip().rsplit("/", 1)[-1] if len(parts) > 1 else ""
            if comm in _LAUNCHERS:
                return ppid_of_pgid
            return pgid
    except Exception:
        pass
    return pgid


def _default_socket_path(cwd: Path) -> Path:
    digest = hashlib.sha256(f"{cwd}:{_tool_pid()}".encode()).hexdigest()[:8]
    return _runtime_dir() / "clangd-lsp-proxy" / f"{digest}.sock"


def _auto_compile_commands_dir(cwd: Path) -> Path | None:
    for candidate in (cwd / "build", cwd):
        if (candidate / "compile_commands.json").exists():
            return candidate
    return None


@proxy_app.default
def _proxy_command(
    *extra_clangd_args: Annotated[str, cyclopts.Parameter(show=False)],
    compile_commands_dir: Path | None = None,
    control_socket: Path | None = None,
    log_file: Path | None = None,
    log_level: str = "warning",
) -> None:
    """Start the LSP proxy.

    All unrecognised arguments are forwarded verbatim to the clangd backend
    on every start (e.g. --background-index=0).
    """
    handler: logging.Handler = (
        logging.FileHandler(log_file) if log_file else logging.StreamHandler(sys.stderr)
    )
    logging.basicConfig(
        level=log_level.upper(),
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cwd = Path.cwd()
    socket_path = control_socket or _default_socket_path(cwd)
    initial_dir = compile_commands_dir or _auto_compile_commands_dir(cwd) or cwd

    print(f"clangd-lsp-proxy: socket {socket_path}", file=sys.stderr, flush=True)

    asyncio.run(
        _run_proxy(
            socket_path=socket_path,
            initial_compile_commands_dir=initial_dir,
            extra_args=list(extra_clangd_args),
        )
    )


async def _setup_stdin() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    return reader


async def _run_proxy(
    socket_path: Path,
    initial_compile_commands_dir: Path,
    extra_args: list[str],
) -> None:
    backend = BackendManager()
    docs = DocumentStore()
    control = ControlServer(socket_path)

    stdin_reader = await _setup_stdin()

    proxy = Proxy(
        stdin_reader=stdin_reader,
        backend=backend,
        control=control,
        document_store=docs,
        extra_args=extra_args,
    )

    await control.start()
    await backend.start(initial_compile_commands_dir, extra_args)
    try:
        await proxy.run()
    finally:
        await control.stop()
        await backend.stop()


def proxy_main() -> None:
    proxy_app()


# ---------------------------------------------------------------------------
# clangd-lsp-proxy-mcp
# ---------------------------------------------------------------------------

mcp_app = cyclopts.App(
    name="clangd-lsp-proxy-mcp",
    help="MCP server companion for a running clangd-lsp-proxy instance.",
)


def _discover_socket(cwd: Path) -> Path | None:
    sock_dir = _runtime_dir() / "clangd-lsp-proxy"
    digest = hashlib.sha256(f"{cwd}:{_tool_pid()}".encode()).hexdigest()[:8]
    candidate = sock_dir / f"{digest}.sock"
    return candidate if candidate.exists() else None


@mcp_app.default
def _mcp_command(
    *,
    socket: Path | None = None,
    transport: Literal["stdio", "http", "sse", "streamable-http"] = "stdio",
) -> None:
    """Start the MCP companion server.

    Connects to a running clangd-lsp-proxy control socket and exposes its
    operations as MCP tools.
    """
    env_socket = os.environ.get("CLANGD_PROXY_SOCKET")
    # Fall back to the default path even if the socket file does not exist yet:
    # the proxy starts only when the first C/C++ file is opened, which can happen
    # after the MCP server is already running. Tools will report a clear error if
    # called before the proxy is up.
    socket_path = (
        socket
        or (Path(env_socket) if env_socket else None)
        or _default_socket_path(Path.cwd())
    )

    server = create_mcp_server(socket_path)
    server.run(transport=transport)


def mcp_main() -> None:
    mcp_app()


# ---------------------------------------------------------------------------
# clangd-lsp-proxy-ctl
# ---------------------------------------------------------------------------

ctl_app = cyclopts.App(
    name="clangd-lsp-proxy-ctl",
    help="Query and control a running clangd-lsp-proxy instance.",
)


def _resolve_socket(socket: Path | None) -> Path:
    env_socket = os.environ.get("CLANGD_PROXY_SOCKET")
    path = (
        socket
        or (Path(env_socket) if env_socket else None)
        or _discover_socket(Path.cwd())
    )
    if path is None:
        cwd = Path.cwd()
        tool = _tool_pid()
        sock_dir = _runtime_dir() / "clangd-lsp-proxy"
        existing = sorted(sock_dir.glob("*.sock")) if sock_dir.exists() else []
        print(
            f"clangd-lsp-proxy-ctl: no control socket found.\n"
            f"  cwd={cwd}\n"
            f"  pid={os.getpid()} pgid={os.getpgrp()} tool_pid={tool}\n"
            f"  looked for: {sock_dir / (hashlib.sha256(f'{cwd}:{tool}'.encode()).hexdigest()[:8] + '.sock')}\n"
            f"  sockets in {sock_dir}: {[s.name for s in existing]}\n"
            f"Set --socket or CLANGD_PROXY_SOCKET.",
            file=sys.stderr,
        )
        sys.exit(1)
    return path


def _ctl_call(socket: Path | None, method: str, params: dict[str, Any]) -> None:
    """Run a control request and print the result as JSON, or exit cleanly on error."""
    sock = _resolve_socket(socket)
    try:
        result = asyncio.run(control_request(sock, method, params))
    except FileNotFoundError:
        print(f"clangd-lsp-proxy-ctl: socket not found: {sock}", file=sys.stderr)
        sys.exit(1)
    except ConnectionRefusedError:
        print(
            f"clangd-lsp-proxy-ctl: connection refused: {sock} "
            "(is clangd-lsp-proxy running?)",
            file=sys.stderr,
        )
        sys.exit(1)
    except OSError as exc:
        print(f"clangd-lsp-proxy-ctl: {exc}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as exc:
        print(f"clangd-lsp-proxy-ctl: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result))


@ctl_app.command
def status(*, socket: Path | None = None) -> None:
    """Print proxy status as JSON."""
    _ctl_call(socket, "status", {})


@ctl_app.command
def list_configs(*, socket: Path | None = None) -> None:
    """List available compile_commands.json configs as JSON."""
    _ctl_call(socket, "list_configs", {})


@ctl_app.command
def switch(
    compile_commands_dir: Path,
    *,
    socket: Path | None = None,
) -> None:
    """Switch the active clangd backend to COMPILE_COMMANDS_DIR."""
    _ctl_call(socket, "switch", {"compile_commands_dir": str(compile_commands_dir)})


def ctl_main() -> None:
    ctl_app()
