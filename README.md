# Workspace Docs MCP

Local-first, read-only MCP server for helping coding agents find the right documentation faster than manual `rg`/grep spelunking.

It is intentionally not a generative RAG chat app. It locates authoritative docs, sections, definitions, and citations inside a workspace.

Given a query such as `where is the auth runbook?`, `definition of extractor`, or `PaymentWebhookHandler`, the MCP returns:

- workspace-relative path;
- title, status, type, and area;
- heading and line range;
- citation;
- confidence;
- compact ranking signals;
- warning/owner action when the index needs attention.

## Why

Agents often waste tokens and time reading random files or running broad text search. This tool gives them a simple pattern:

1. Ask `find_docs` or `locate_topic`.
2. Open only the returned citation with `open_doc`.
3. Escalate to the owner if the semantic index is blocked.

`search_exact` exists for explicit symbols/paths/config keys. It is not a semantic fallback, but it can stay available during a background semantic rebuild once the SQLite catalog has been committed.

## Features

- Read-only MCP tools.
- SQLite catalog for deterministic inspection.
- Qdrant vector/sparse cache.
- Local `BAAI/bge-m3` embeddings through `FlagEmbedding.BGEM3FlagModel`.
- Local `BAAI/bge-reranker-v2-m3` reranker through `FlagEmbedding.FlagReranker`.
- No fallback to alternate models.
- Document-first search for `find_docs`.
- Section-first search for `locate_topic`.
- Glossary/entity retrieval for definitions and naming/domain-model queries.
- Workspace file inventory plus lightweight code/config exact index.
- Code symbols and config keys can bridge queries back to authoritative docs without embedding full source files.
- Background indexing hints with explicit `owner_action` when blocked.
- Background index workers are parent-bound and have a runtime cap, so they do not keep GPU/CPU busy indefinitely after the agent runtime exits.
- SQLite catalog is committed before Qdrant rebuild, so explicit path/symbol lookup can work while vectors are still building.
- Existing Qdrant collections are updated in place during rebuild; they are not dropped first, so the previous semantic index remains queryable until fresh points replace it.
- Compact JSON output with scores rounded to `0.000..1.000`.
- Windows-friendly scripts plus normal Python entrypoints.

## Requirements

- Python 3.11 or newer.
- Git.
- Qdrant running locally on `http://localhost:6333`.
- Local model access/cache for:
  - `BAAI/bge-m3`
  - `BAAI/bge-reranker-v2-m3`
- Optional but recommended: NVIDIA GPU with CUDA PyTorch for practical performance.

## 5-Minute Windows Install

Recommended for Codex/Claude users on Windows:

```powershell
git clone https://github.com/dummics/workspace-docs-mcp.git "$env:USERPROFILE\.workspace-docs-mcp"
& "$env:USERPROFILE\.workspace-docs-mcp\scripts\install.ps1" -WithCuda -StartQdrant
& "$env:USERPROFILE\.workspace-docs-mcp\scripts\setup-workspace.ps1" -Workspace "C:\path\to\your\repo" -Preset generic -BuildIndex
```

Use `-CpuOnly` instead of `-WithCuda` if the machine has no NVIDIA/CUDA setup. CPU mode works, but first indexing and reranking can be slow on large workspaces.

The installer creates stable wrappers:

```text
%USERPROFILE%\.workspace-docs-mcp\bin\workspace-docs.cmd
%USERPROFILE%\.workspace-docs-mcp\bin\workspace-docs-mcp.cmd
```

Use those wrapper paths in MCP config so agents do not need an activated shell or a manual virtualenv.

## Manual Install

From source:

```powershell
git clone https://github.com/dummics/workspace-docs-mcp.git
cd workspace-docs-mcp
python -m pip install -e ".[all]"
```

CUDA PyTorch on Windows, before model checks:

```powershell
python -m pip install --user --force-reinstall "torch==2.7.1" "torchvision==0.22.1" "torchaudio==2.7.1" --index-url https://download.pytorch.org/whl/cu128
```

Lean install for CI or catalog-only development:

```powershell
python -m pip install -e ".[dev]"
```

## Quick Start

Inside the workspace you want agents to search:

```powershell
workspace-docs init --preset generic
docker run -p 6333:6333 -v ${PWD}/.rag/qdrant:/qdrant/storage qdrant/qdrant
workspace-docs models doctor
workspace-docs index build
workspace-docs search "where is the architecture overview?"
workspace-docs mcp
```

For an agent-managed setup, give the agent this compact instruction:

```text
Install workspace-docs-mcp from https://github.com/dummics/workspace-docs-mcp.
Ask me only if you cannot infer:
- target workspace path;
- CUDA/NVIDIA vs CPU-only;
- whether Docker/Qdrant may be started locally.
Use the Windows installer when on Windows. Configure the MCP server named workspaceDocs. Build the initial index. Do not add write tools or shell fallback. After setup, test only through MCP tools: index_status, find_docs, locate_topic, search_exact, open_doc.
```

Without installing, from a source checkout:

```powershell
$tool = "C:\path\to\workspace-docs-mcp"
$env:PYTHONPATH = $tool
python -m workspace_docs_mcp.cli --root "C:\path\to\workspace" init
python -m workspace_docs_mcp.cli --root "C:\path\to\workspace" models doctor
python -m workspace_docs_mcp.cli --root "C:\path\to\workspace" index build
python -m workspace_docs_mcp.cli --root "C:\path\to\workspace" mcp
```

## MCP Tools

- `find_docs`: document-first locator for "where is the doc for X?"
- `locate_topic`: section-first locator for heading-level citations.
- `open_doc`: opens a catalog-known path/heading/line range, with traversal blocking and `max_chars`.
- `search_exact`: exact lookup for explicit symbols, paths, config keys, route IDs, and manifest names.
- `list_canonical`: lists canonical/runbook docs by area/topic.
- `doc_neighbors`: returns links and related docs for one path.
- `explain_result`: explains ranking or no-results; `path` may be null.
- `index_status`: read-only readiness report.

## MCP Config

### Codex

See [integrations/codex-config.example.toml](integrations/codex-config.example.toml).

```toml
[mcp_servers.workspaceDocs]
command = "workspace-docs-mcp"
args = ["--root", "C:\\path\\to\\workspace"]
enabled = true
startup_timeout_sec = 120
tool_timeout_sec = 300
```

### Claude Desktop

See [integrations/claude-desktop-config.example.json](integrations/claude-desktop-config.example.json).

```json
{
  "mcpServers": {
    "workspace-docs": {
      "command": "workspace-docs-mcp",
      "args": ["--root", "C:\\path\\to\\workspace"]
    }
  }
}
```

## Agent Instructions

Add this short policy to the target agent instructions:

- Use `find_docs` / `locate_topic` before reading files.
- Use `open_doc` only for returned citations.
- Do not use shell search or broad `rg` as fallback when the semantic index is blocked.
- If `search_mode=blocked`, follow `owner_action`.
- Use `search_exact` only for explicit symbol/path/config-key lookups.
- If `index_status.exact_available=true`, exact lookup is allowed only for those explicit terms; otherwise retry `find_docs` / `locate_topic` after `retry_after_seconds`.
- If the index is `usable_stale`, the MCP should still answer from the previous index, cap confidence at medium, and refresh in the background after the current search.

This is the intended usage pattern for agents:

1. Start with `index_status` only when checking readiness or debugging.
2. For docs, use `find_docs` or `locate_topic`.
3. Open only returned citations with `open_doc`.
4. For literal terms such as `PaymentWebhookHandler`, `SITE_GATE_PASSWORD`, `runner_flavor`, or a file path, use `search_exact`.
5. If blocked, follow `owner_action` instead of running `rg` or reading random files.

## Project Config

`workspace-docs init` creates:

```text
.workspace-docs/
  locator.config.yml
  topic-aliases.json
  eval-canonical-topics.json
```

First-class glossary/entity sources:

- `domain-definitions.json`
- `glossary.yml`
- `glossary.yaml`
- `docs/**/terms.md`
- `docs/**/standard-definitions.md`

The index cache lives under `.rag/` and should not be committed.

Code-aware indexing is intentionally lightweight:

- all configured text source files are inventoried in SQLite;
- file lines are indexed for exact/FTS lookup with secret-like values redacted;
- class/function/controller symbols and config/env keys are extracted from common source files;
- full source files are not embedded into Qdrant by default;
- docs, glossaries, and runbooks remain the primary semantic retrieval target.

Current code-aware exact extraction covers common patterns in:

- C# / .NET (`.cs`);
- TypeScript and JavaScript (`.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`);
- Python (`.py`);
- JSON/YAML/TOML/env/config-like files for config keys.

`search_exact` does not call an embedding model and does not shell out to `rg`. It queries the local SQLite catalog, FTS table, code symbol table, config key table, and catalog paths/titles. This keeps exact lookup deterministic and available even while the semantic Qdrant index is refreshing.

## Troubleshooting

Run:

```powershell
workspace-docs models doctor
workspace-docs index_status
```

Common blockers:

- Qdrant is not running.
- The BGE models are not downloaded or cannot load.
- CUDA PyTorch is missing or CPU-only.
- The catalog is empty because docs roots are wrong.
- The index is incompatible after model/backend/config changes.

When MCP search is blocked, the response includes `owner_action`. Agents should not invent fallback behavior.

During first indexing, `find_docs` and `locate_topic` can remain blocked until Qdrant document and section collections are complete. After a usable index exists, stale indexes should stay queryable: the MCP answers from the previous index, caps confidence at medium, and updates Qdrant in place in the background. The SQLite catalog is committed first; if `index_status.exact_available=true`, `search_exact` may resolve explicit paths, symbols, route IDs, or config keys while semantic retrieval finishes.

Background indexing is opportunistic, not a daemon. Workers are launched only when the MCP detects a blocked/stale index and auto-indexing is allowed. The worker receives the parent MCP process PID, exits if that parent disappears, and also stops after `auto_index.max_runtime_seconds` (default: `3600`). This prevents an agent session from leaving a long-running BGE/Qdrant process consuming GPU all day.

## Security Model

- MCP tools are read-only.
- No arbitrary shell execution through MCP.
- `open_doc` only opens catalog-known workspace-relative files.
- Path traversal is blocked with `Path.relative_to`.
- Qdrant and SQLite are rebuildable caches, not source of truth.

## Examples

The [examples/licensing-framework](examples/licensing-framework) folder is a real project adapter/fixture. It is intentionally small and kept separate from the core package.

## Development

```powershell
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
python -m build
```

Model/Qdrant smoke:

```powershell
workspace-docs models doctor
```

## License

MIT. See [LICENSE](LICENSE).
