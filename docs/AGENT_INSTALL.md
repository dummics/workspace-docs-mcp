# Agent Install In 5 Minutes

This is the shortest safe setup path for coding agents.

## Ask The Owner Only If Needed

Ask only when you cannot infer:

- target workspace path;
- NVIDIA/CUDA vs CPU-only;
- whether Docker/Qdrant may be started locally.

## Windows

```powershell
git clone https://github.com/dummics/SemRAGent.git "$env:USERPROFILE\.semragent"
& "$env:USERPROFILE\.semragent\scripts\install.ps1" -WithCuda -StartQdrant
& "$env:USERPROFILE\.semragent\scripts\setup-workspace.ps1" -Workspace "C:\path\to\workspace" -Preset generic -BuildIndex
```

Use `-CpuOnly` instead of `-WithCuda` when there is no NVIDIA/CUDA setup.

## macOS / Linux

```sh
git clone https://github.com/dummics/SemRAGent.git "$HOME/.semragent"
sh "$HOME/.semragent/scripts/install.sh"
"$HOME/.semragent/bin/semragent" --root "/path/to/workspace" init --preset generic
"$HOME/.semragent/bin/semragent" --root "/path/to/workspace" qdrant status
"$HOME/.semragent/bin/semragent" --root "/path/to/workspace" models doctor
"$HOME/.semragent/bin/semragent" --root "/path/to/workspace" index build
```

## MCP Config

Codex / Claude-style config:

```json
{
  "mcpServers": {
    "semragent": {
      "command": "semragent",
      "args": ["--root", "/path/to/workspace", "mcp"]
    }
  }
}
```

## Validation

After the MCP runtime restarts, test only through MCP tools:

- `index_status`
- `find_docs`
- `locate_topic`
- `prepare_context`
- `search_exact`
- `open_doc`

Do not use `rg`, grep, broad directory scans, or manual random reads as a replacement for SemRAGent.

If `search_mode=degraded`, keep using the returned citations and let background indexing recover the semantic layer.

If `search_mode=blocked`, follow `owner_action`.
