# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

A stdio LSP proxy that sits between an editor (Neovim, Claude Code) and `clangd`, exposing a Unix-socket control plane that lets external tools switch the active clangd backend (binary + `compile_commands.json` directory) at runtime without restarting the editor's LSP client.
A separate FastMCP companion server (`clangd-lsp-proxy-mcp`) re-exposes the control plane as MCP tools.
See `README.md` for full protocol and integration details.

## Common commands

```sh
uv sync                              # install deps (incl. dev group)
uv run pytest                        # run full test suite
uv run pytest tests/test_proxy.py    # one file
uv run pytest tests/test_proxy.py::test_name   # one test
uv run ruff check src/               # lint
uv run ruff format src/              # format
uv run ty check src/                 # type check (Astral's `ty`, not mypy)
uv run rumdl check                   # markdown lint
```

Tests use `pytest-asyncio` in `asyncio_mode = "auto"`, so async test functions don't need decorators.
Requires Python ≥ 3.12.

## Architecture

Three entry points defined in `pyproject.toml` all live in `src/clangd_lsp_proxy/cli.py`:

- `clangd-lsp-proxy` → `proxy_main`: the LSP proxy itself (stdio in, stdio out, Unix socket for control).
- `clangd-lsp-proxy-mcp` → `mcp_main`: FastMCP companion that connects to a running proxy's socket.
- `clangd-lsp-proxy-ctl` → `ctl_main`: thin CLI client for the same socket.
  Subcommands: `status`, `list-configs`, `switch DIR`.

Core components (`src/clangd_lsp_proxy/`):

- `proxy.py`: `Proxy` class with two long-running tasks, `_client_loop` (stdin → backend) and `_backend_loop` (backend → stdout).
  Tracks pending request IDs for cancellation on switch, intercepts `initialize` / `didOpen` / `didChange` / `didClose` for state tracking and replay, and forces `textDocumentSync = Full (1)` in the initialize response so the document store can keep authoritative full-text copies cheaply.
- `backend.py`: `BackendManager` owns the clangd subprocess and a reader task that drains its stdout into an `asyncio.Queue`. `index_storage_path()` derives a per-(binary, compile_commands_dir) cache dir under `.cache/clangd-lsp-proxy/` so incompatible LLVM forks never share an index.
- `control.py`: `ControlServer` is a newline-delimited JSON server over `AF_UNIX`.
  On startup it probes for a live listener at its socket path and, if one answers, **runs without a control plane** rather than evicting it.
  This is how two proxy instances with the same cwd (in-tree + out-of-tree header navigation) coexist.
- `config_resolver.py`: picks the right clangd binary from the first entry's compiler in a `compile_commands.json`.
  See README "clangd binary resolution" for the rules.
- `document_store.py`: full-text snapshots keyed by URI, relies on the forced Full sync mode above.
- `jsonrpc.py`: LSP `Content-Length` framing read/write plus pydantic typed models.
- `_client.py`: async client used by both the CLI (`-ctl`) and the MCP server.
- `mcp_server.py`: `create_mcp_server(socket_path)` returns a FastMCP server exposing `switch_clangd_config`, `get_clangd_status`, `list_clangd_configs`.

## Critical behaviors to preserve

These are non-obvious and have explanatory comments in the source.
Read them before changing the surrounding code:

- **Switch atomicity** (`proxy.py::_handle_switch`): the backend loop is cancelled before the queue is drained in `_replay_to_backend`, in-flight client requests get a `-32099` error reply, then the new backend is started and the recorded `initialize` request + every open document is replayed before the loop resumes.
- **EOF sentinel in `BackendManager._read_loop`**: uses `put_nowait(None)` rather than `await queue.put(None)` to survive the cancellation flow. `stop()` also injects a sentinel directly because Python 3.12+ may skip a cancelled task's body entirely (including `finally`).
- **`_pending` tracks only client-initiated request IDs**: server→client requests (e.g. `window/workDoneProgress/create`) live in a separate ID namespace and must not be popped from `_pending`.
  See `proxy.py::_backend_loop` and `_client_loop`.
- **Socket path derivation**: SHA-256 of `"{cwd}:{tool_pid}"`, truncated to 8 hex chars, placed under `$XDG_RUNTIME_DIR` → `$TMPDIR` → `/tmp` (in that order).
  `tool_pid` is computed by `cli.py::_tool_pid` by inspecting the current process's PGID leader:
  - If the PGID leader's command name is a known launcher (`uvx`, `uv`, `sh`, `bash`, `python3`, etc.), its PPID is the real tool.
    This handles Neovim's `jobstart()`, which creates a new process group with uvx as leader.
  - Otherwise the PGID leader itself is the tool (e.g. nvim when `vim.system()` inherits nvim's group, or claude when Claude Code spawns children without changing groups).
  The critical Neovim subtlety: `jobstart()` (proxy) creates a new process group with uvx as leader (→ PPID = nvim PID), but `vim.system()` (ctl) inherits nvim's process group with nvim as leader (→ use PGID directly = nvim PID).
  Both arrive at the same nvim PID.
  `os.getpgrp()` alone fails because the proxy and ctl get different process groups; `os.getsid(0)` fails because macOS GUI and terminal apps share sessions unpredictably.
  The same `_tool_pid()` call must be used in both `_default_socket_path` and `_discover_socket` — keep them in sync.

## Tests

`tests/conftest.py` provides `FakeBackendManager` / `FakeControlServer` stubs and a `FAKE_CLANGD_SCRIPT` for integration-style tests that actually spawn a subprocess.
Use `make_proxy()` to wire a `Proxy` against the fakes.
Use `fake_clangd` + `tmp_socket` fixtures for end-to-end tests.
The `tmp_socket` fixture deliberately bypasses pytest's `tmp_path` because macOS caps `AF_UNIX` paths at 104 chars.

## Style notes (from `pyproject.toml`)

- Ruff: line length 88, target `py312`, lint selection `E, F, I, UP, B, SIM`.
- `from __future__ import annotations` is used throughout.
  Keep it.
