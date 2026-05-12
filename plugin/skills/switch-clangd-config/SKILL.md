---
description: Switch the active clangd backend configuration (compile_commands.json directory). Use when asked to change the active clangd config, switch build targets, or when working on files that belong to a different build configuration than the currently active one.
---

# Switch clangd config

List available clangd configurations and let the user pick one, then switch to it.

## Steps

1. Run `clangd-lsp-proxy-ctl list-configs` to retrieve the available configurations and the currently active one.
2. Present the list to the user, highlighting the current selection.
3. Once the user chooses a configuration, run `clangd-lsp-proxy-ctl switch <dir>` with the selected directory.
4. Confirm the switch by reporting the new active binary and compile_commands.json path from the command output.

## Running the tool

Use $ARGUMENTS to pass an explicit compile_commands.json directory when provided (skip step 2-3 in that case):

```bash
clangd-lsp-proxy-ctl list-configs
clangd-lsp-proxy-ctl switch <dir>
```

Both commands auto-discover the proxy socket from the parent process.
The `--socket` flag is not needed in normal use.
