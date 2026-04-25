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
- Background indexing hints with explicit `owner_action` when blocked.
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

## Install

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
