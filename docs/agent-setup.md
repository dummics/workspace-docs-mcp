# Agent Setup Prompt

Use this when asking Codex, Claude, or another coding agent to install the tool for a workspace.

```text
Install and configure SemRAGent for this local workspace.

Repository:
https://github.com/dummics/SemRAGent

Goal:
Make the MCP server available as semragent and build the initial index. After setup, test through MCP tools only. Do not use rg/grep/manual file scanning as a replacement for the locator.

Ask me only for these decisions if you cannot infer them:
- target workspace path;
- whether this machine has NVIDIA CUDA and should use GPU setup;
- whether Docker/Qdrant may be started locally.

On Windows, prefer:
1. Clone or update the repo into %USERPROFILE%\.semragent.
2. Run scripts\install.ps1 with -WithCuda if CUDA is available, otherwise -CpuOnly. Use -StartQdrant if Docker is allowed.
3. Run scripts\setup-workspace.ps1 -Workspace "<target workspace>" -Preset generic -BuildIndex.
4. Add the printed semragent MCP config to Codex/Claude.
5. Restart the agent runtime so the MCP server is loaded.

Validation:
- Call index_status.
- Call find_docs for an architecture/runbook query.
- Call locate_topic for a definition/topic query.
- Call prepare_context for one coding task.
- Call search_exact for one explicit symbol/path/config key.
- Call open_doc only on citations returned by the locator.

Success criteria:
- index_status is fresh, usable_stale, or degraded with safe_to_use=true.
- find_docs/locate_topic return cited results.
- search_exact resolves explicit symbols/paths/config keys without exposing secret values.
- No broad shell search is used as fallback.
```
