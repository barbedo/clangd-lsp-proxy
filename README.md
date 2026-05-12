# clangd-lsp-proxy

**Note**: 🚧 Work in progress 🏗️

An LSP proxy for clangd that supports runtime backend switching.

## Why

Heterogeneous C/C++ projects, those with parts compiled natively and parts cross-compiled with custom toolchains, may require different clangd binaries for different `compile_commands.json` databases.
Most editors (and agentic coding tools) launch an LSP server once with a fixed command and cannot change it at runtime.

`clangd-lsp-proxy` sits between the editor and clangd.
It speaks LSP on both sides and exposes a separate control channel (CLI and MCP) that lets any tool switch the active backend while the editor session stays alive.

## Architecture

```text
Editor (Neovim / Claude Code)
         │  LSP over stdin/stdout
         ▼
  ┌──────────────────┐   Unix socket   ┌──────────────────────────┐
  │ clangd-lsp-proxy │◄───────────────►│ Control clients          │
  │                  │ (newline JSON)  │ • clangd-lsp-proxy-mcp   │
  └────────┬─────────┘                 │ • clangd-lsp-proxy-ctl   │
           │  LSP over subprocess      └──────────────────────────┘
           ▼
  clangd (any binary, any compile_commands.json)
```

A single proxy instance manages one clangd backend at a time.
On a switch request it:

1. Cancels in-flight LSP requests (returns errors to the editor).
2. Stops the old clangd process.
3. Starts a new clangd with the selected binary and `--compile-commands-dir`.
4. Replays `initialize` and all open `textDocument/didOpen` messages to the fresh backend.
5. Resumes normal proxying.

Each backend gets an isolated index storage directory (keyed by binary path and `compile_commands.json` path) so clangd indexes never corrupt each other.

## Installation

To make the binaries available in `PATH`, use `uv tool install`:

```sh
uv tool install git+https://github.com/barbedo/clangd-lsp-proxy.git
```

## Usage

### `clangd-lsp-proxy`

Start the proxy (the editor launches this instead of clangd directly):

```sh
clangd-lsp-proxy \
    [--compile-commands-dir DIR] [--control-socket PATH] \
    [--log-file FILE] [--log-level LEVEL] \
    [-- EXTRA_CLANGD_ARGS...]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--compile-commands-dir` | auto-detected | Directory containing `compile_commands.json` passed to clangd on startup |
| `--control-socket` | auto (see below) | Path for the Unix control socket |
| `--log-file` | stderr | Write log output to this file |
| `--log-level` | `warning` | `debug`, `info`, `warning`, `error` |

Any unrecognised arguments are forwarded verbatim to clangd on every start (e.g. `--background-index=0`).

The socket path is derived from a SHA-256 hash of the working directory and the process group ID (PGID), so each editor instance gets its own socket even when multiple tools open the same project simultaneously.
The socket is placed under `<runtime-dir>/clangd-lsp-proxy/<hash>.sock`, where `<runtime-dir>` is resolved in this order:

1. `$XDG_RUNTIME_DIR`: Set on systemd Linux to `/run/user/<uid>` (mode 700, user-private).
2. `$TMPDIR`: Set by macOS to a user-private directory under `/var/folders/`.
3. `/tmp`: Fallback for other platforms (world-readable, so prefer setting one of the above).

### `clangd-lsp-proxy-mcp`

Start the MCP companion server (connects to a running proxy):

```sh
clangd-lsp-proxy-mcp [--socket PATH] [--transport stdio|http|sse|streamable-http]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--socket` | auto-discovered | Path to the proxy control socket |
| `--transport` | `stdio` | MCP transport to use |

The socket is auto-discovered from `$CLANGD_PROXY_SOCKET` or the default path derived from the current working directory and the calling process's parent PID (see [Runtime directory][runtime-directory] above).
Because `clangd-lsp-proxy-mcp` and `clangd-lsp-proxy-ctl` share the same parent process as the proxy when launched by the same editor, they find the correct socket automatically with no extra configuration.

[runtime-directory]: #runtime-directory

### `clangd-lsp-proxy-ctl`

Query and control a running proxy from the command line or from editor scripts.
All subcommands print a JSON object to stdout and exit non-zero on error.

```sh
clangd-lsp-proxy-ctl status        [--socket PATH]
clangd-lsp-proxy-ctl list-configs  [--socket PATH]
clangd-lsp-proxy-ctl switch DIR    [--socket PATH]
```

| Subcommand | Output |
|------------|--------|
| `status` | `binary`, `compile_commands_dir`, `open_documents`, `pending_requests`, `backend_running` |
| `list-configs` | `configs` (list of `{dir, binary}`), `current` (active directory) |
| `switch DIR` | `binary`, `compile_commands_dir` of the newly active backend |

The `--socket` option and `$CLANGD_PROXY_SOCKET` environment variable work the same way as for `clangd-lsp-proxy-mcp`.
Socket auto-discovery follows the same runtime directory and PPID rules.

#### Neovim integration

Because all logic lives in the binary, editor scripts stay thin.
The example below replaces the local `compile_commands.json` discovery with a call to `list-configs`.

A subtlety: if `autochdir` is set (or any other mechanism changes Neovim's working directory), the `cwd` seen by `clangd-lsp-proxy-ctl` may differ from the project root the proxy was started in.
Capture the project root once at init time and pass it explicitly as `cwd` to every `vim.system` call:

```lua
-- Capture at init time, before any buffer navigation can change cwd.
local project_root = vim.fn.getcwd()

local function select_clangd_config()
  vim.system(
    { "clangd-lsp-proxy-ctl", "list-configs" },
    { text = true, cwd = project_root },
    function(r)
      if r.code ~= 0 then
        vim.schedule(function()
          vim.notify(r.stderr or "clangd-lsp-proxy-ctl failed", vim.log.levels.ERROR)
        end)
        return
      end
      local ok, data = pcall(vim.json.decode, r.stdout)
      if not ok then return end
      local labels = {}
      for _, cfg in ipairs(data.configs) do
        table.insert(labels, vim.fn.fnamemodify(cfg.dir, ":~:."))
      end
      vim.schedule(function()
        vim.ui.select(labels, { prompt = "clangd config" }, function(choice)
          if not choice then return end
          for _, cfg in ipairs(data.configs) do
            if vim.fn.fnamemodify(cfg.dir, ":~:.") == choice then
              vim.system(
                { "clangd-lsp-proxy-ctl", "switch", cfg.dir },
                { text = true, cwd = project_root }
              )
              return
            end
          end
        end)
      end)
    end
  )
end

vim.api.nvim_create_user_command("LspClangdSelectConfig", select_clangd_config, {})
```

#### Out-of-tree header navigation

When following a definition into a system header or any file outside the project root, Neovim starts a **second** `clangd-lsp-proxy` instance for that file.
Because both instances derive their socket path from a hash of their working directory, and both inherit the same Neovim cwd, the second instance would overwrite the first proxy's socket on startup.

`clangd-lsp-proxy` handles this automatically: at startup it attempts to connect to the computed socket path.
If a live proxy is already listening there, the new instance skips creating its own control plane and runs as a plain LSP proxy without touching the socket file.

On the editor side, add an `LspAttach` autocmd that redirects out-of-tree files back to the existing in-tree clangd client:

```lua
-- project_root must be the same variable captured above.
vim.api.nvim_create_autocmd("LspAttach", {
  group = vim.api.nvim_create_augroup("lsp-clangd-out-of-tree", { clear = true }),
  callback = function(args)
    local bufnr = args.buf
    local client = vim.lsp.get_client_by_id(args.data.client_id)
    if not client or client.name ~= "clangd" then return end

    local file_path = vim.api.nvim_buf_get_name(bufnr)
    if vim.startswith(file_path, project_root .. "/") or file_path == project_root then
      return
    end

    vim.schedule(function()
      -- Identify the project client as the one with in-tree buffers attached.
      local project_client = nil
      for _, c in ipairs(vim.lsp.get_clients({ name = "clangd" })) do
        for attached_bufnr in pairs(c.attached_buffers) do
          local path = vim.api.nvim_buf_get_name(attached_bufnr)
          if vim.startswith(path, project_root .. "/") then
            project_client = c
            break
          end
        end
      end

      -- If this IS the project client re-attaching (re-entry guard), or there
      -- is no project client to fall back to, leave things as they are.
      if not project_client or project_client.id == client.id then return end

      -- Guard: verify the spurious client is still attached before detaching.
      if not client.attached_buffers[bufnr] then return end

      vim.lsp.buf_detach_client(bufnr, client.id)
      client:stop()
      vim.lsp.buf_attach_client(bufnr, project_client.id)
    end)
  end,
})
```

The autocmd detects when a second clangd client attaches to an out-of-tree buffer, stops it, and re-attaches the in-tree client.
The in-tree client (via the proxy) then receives `textDocument/didOpen` for the header and provides hover, go-to-definition, and diagnostics through the same binary and index already loaded for the project.

## Control plane protocol

The Unix socket accepts newline-delimited JSON requests and returns newline-delimited JSON responses.
Each request has `id`, `method`, and `params` fields; each response has `id` and either `result` or `error`.

### Methods

**`switch`** — switch the active clangd backend:

```json
{"id": 1, "method": "switch", "params": {"compile_commands_dir": "/path/to/build"}}
```

Response:

```json
{"id": 1, "result": {"binary": "/opt/toolchain/bin/clangd", "compile_commands_dir": "/path/to/build"}}
```

**`status`** — query proxy state:

```json
{"id": 2, "method": "status", "params": {}}
```

Response fields: `binary`, `compile_commands_dir`, `open_documents`, `pending_requests`, `backend_running`.

**`list_configs`** — discover available `compile_commands.json` files:

```json
{"id": 3, "method": "list_configs", "params": {}}
```

Response: `configs` (list of `{dir, binary}`) and `current` (active directory).

## MCP tools

When the MCP server is running, three tools are available to AI assistants:

- **`list_clangd_configs`** — discover all `compile_commands.json` files under the project root and the clangd binary each would use.
- **`get_clangd_status`** — check which binary and config are currently active.
- **`switch_clangd_config`** — switch to a different `compile_commands.json` directory.

### Claude Code integration

See the [Claude Code plugin](#claude-code-plugin) section below.
The plugin wires up both the LSP server and the MCP tools together.

## clangd binary resolution

The proxy (and MCP `list_clangd_configs`) automatically selects the appropriate clangd binary for each `compile_commands.json` by inspecting the compiler used in the first entry:

- `/usr/bin` compiler with no cross-compiler prefix → `clangd` from `PATH`
- Custom absolute path (e.g. `/opt/toolchain/bin/clang`) → sibling `clangd` in the same directory
- Cross-compiler prefix (e.g. `arm-none-eabi-gcc`) → `arm-none-eabi-clangd` from the same directory or `PATH`
- Fallback → `clangd` from `PATH`

## Concurrent editors

Each proxy instance derives its control socket from a hash of the project directory **and the process group ID (PGID)**.
This means Neovim, Claude Code, and any other editor can each run their own proxy against the same project simultaneously — they get independent sockets, independent clangd processes, and independent control planes without any extra configuration.

clangd already isolates its index storage under `.cache/clangd/` relative to each `compile_commands.json` directory, so separate build configurations never share an index regardless of how many proxy instances are running.

The one intentional collision: when the same Neovim instance spawns a second proxy for an out-of-tree header (a side-effect of root-marker logic), both proxies share the same PPID and therefore the same socket path.
The second proxy detects the live listener and runs as a plain pass-through without touching the socket — this is what makes the out-of-tree header autocmd in the [Neovim integration](#neovim-integration) work correctly.

## Claude Code plugin

The `plugin/` directory is a self-contained Claude Code plugin that configures both the LSP server and the MCP companion in one step.

Load it for a session with:

```sh
claude --plugin-dir /path/to/clangd-lsp-proxy/plugin
```

**Note**: Currently, the only way to add custom LSP support to Claude Code is through plugins.
So either a clone the project to access the `plugin` directory, or copy the plugin to a [local marketplace][local_marketplace].

[local_marketplace]: https://code.claude.com/docs/en/plugin-marketplaces#walkthrough-create-a-local-marketplace

The plugin provides:

- **LSP** (`.lsp.json`): launches `clangd-lsp-proxy` for C/C++ files.
- **MCP tools** (`.mcp.json`): starts `clangd-lsp-proxy-mcp` so Claude can call `list_clangd_configs`, `get_clangd_status`, and `switch_clangd_config` autonomously.
- **`switch-clangd-config` skill**: a named entry point Claude (or the user via `/switch-clangd-config`) can invoke to list and switch configurations interactively.

Because all three components are spawned by Claude Code, they share the same PPID and auto-discover the correct proxy socket with no extra configuration.

## Development

```sh
uv sync
uv run ruff check src/
uv run ruff format src/
uv run ty check src/
uv run rumdl check
```

To test a local checkout in place of the GitHub source, reinstall with the following:

```sh
uv tool install --project /path/to/clangd-lsp-proxy-clone clangd-lsp-proxy
```
