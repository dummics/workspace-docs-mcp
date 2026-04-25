# SemRAGent

Local-first semantic RAG routing for coding agents.

SemRAGent helps agents find authoritative workspace docs before they grep random files: path, heading, line range, authority, confidence, citations, and ranking explanation.

Tagline: **Semantic RAG routing for coding agents.**

SemRAGent is intentionally not a generative RAG chat app. It does not generate long answers from your repository. It is an agent-facing doc locator: given a query, topic, symbol, or path, it routes the agent to the right workspace docs and sections.

Source of truth stays in Git, Markdown, and project manifests/config. SQLite, Qdrant, dense/sparse vectors, and eval reports are rebuildable local caches.

Given a query such as `where is the auth runbook?`, `definition of extractor`, or `PaymentWebhookHandler`, the MCP returns:

- workspace-relative path;
- title, status, type, and area;
- heading and line range;
- citation;
- confidence;
- compact ranking signals;
- warning/owner action when the index needs attention.

## Why SemRAGent?

- Agents waste context when they grep manually.
- Semantic search finds concepts, not just strings.
- Hybrid ranking combines semantic, lexical, entity, route, exact, authority, and freshness signals.
- Authority policy prevents historical/generated docs from beating canonical docs.
- Generated/test-derived docs are suppressed for broad documentation queries unless the query explicitly asks for tests/code.
- Citations make results inspectable.
- Index health prevents silent stale retrieval.

The simple agent pattern is:

1. Ask `find_docs` or `locate_topic`.
2. Open only the returned citation with `open_doc`.
3. Escalate to the owner if the semantic index is blocked.

`search_exact` exists for explicit symbols/paths/config keys. It integrates with semantic retrieval but is not a high-confidence replacement when the semantic index is blocked.

When an agent works from a feature worktree, SemRAGent should still point at the canonical workspace/root index unless the owner explicitly asks to rebuild or add an overlay. The locator's job is to find authoritative docs and sections quickly; agents should then open only the returned citations.

See [Trust Contract](docs/TRUST_CONTRACT.md) for the instruction-safety model.
See [Agent Install In 5 Minutes](docs/AGENT_INSTALL.md) for a compact setup prompt/runbook.

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
git clone https://github.com/dummics/SemRAGent.git "$env:USERPROFILE\.semragent"
& "$env:USERPROFILE\.semragent\scripts\install.ps1" -WithCuda -StartQdrant
& "$env:USERPROFILE\.semragent\scripts\setup-workspace.ps1" -Workspace "C:\path\to\your\repo" -Preset generic -BuildIndex
```

Use `-CpuOnly` instead of `-WithCuda` if the machine has no NVIDIA/CUDA setup. CPU mode works, but first indexing and reranking can be slow on large workspaces.

The installer creates stable wrappers, including the new SemRAGent CLI and legacy aliases:

```text
%USERPROFILE%\.semragent\bin\semragent.cmd
%USERPROFILE%\.semragent\bin\workspace-docs.cmd
%USERPROFILE%\.semragent\bin\workspace-docs-mcp.cmd
```

Use those wrapper paths in MCP config so agents do not need an activated shell or a manual virtualenv.

## Manual Install

From source:

```powershell
git clone https://github.com/dummics/SemRAGent.git
cd SemRAGent
python -m pip install -e ".[vector,models,yaml,mcp]"
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
semragent init --preset generic
semragent qdrant start
semragent models fetch
semragent models doctor
semragent index build
semragent doctor
semragent search "where is the architecture overview?"
semragent mcp
```

Compatibility note: the public product/repository name is SemRAGent, but the Python package is still `workspace-docs-mcp` for this MVP. Legacy commands remain available while the package migrates:

- `semragent` is the preferred CLI.
- `workspace-docs` remains a legacy CLI alias.
- `workspace-docs-mcp` remains a legacy MCP entrypoint.
- TODO: consider a non-breaking Python package rename after external testing.

For an agent-managed setup, give the agent this compact instruction:

```text
Install SemRAGent from https://github.com/dummics/SemRAGent.
Ask me only if you cannot infer:
- target workspace path;
- CUDA/NVIDIA vs CPU-only;
- whether Docker/Qdrant may be started locally.
Use the Windows installer when on Windows. Configure the MCP server named semragent. Build the initial index. Do not add write tools or shell fallback. After setup, test only through MCP tools: index_status, find_docs, locate_topic, prepare_context, search_exact, open_doc.
```

Without installing, from a source checkout:

```powershell
$tool = "C:\path\to\SemRAGent"
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
- `prepare_context`: task-first context router; returns docs/sections/symbols to read before coding.
- `index_status`: read-only readiness report.

## MCP Config

### Codex

See [integrations/codex-config.example.toml](integrations/codex-config.example.toml).

```toml
[mcp_servers.semragent]
command = "semragent"
args = ["mcp"]
enabled = true
startup_timeout_sec = 120
tool_timeout_sec = 300
```

### Claude Desktop

See [integrations/claude-desktop-config.example.json](integrations/claude-desktop-config.example.json).

```json
{
  "mcpServers": {
    "semragent": {
      "command": "semragent",
      "args": ["mcp"]
    }
  }
}
```

Legacy `workspace-docs` / `workspace-docs-mcp` commands remain supported for existing setups.

```json
{
  "mcpServers": {
    "workspace-docs": {
      "command": "workspace-docs",
      "args": ["mcp"]
    }
  }
}
```

## Agent Instructions

Add this short policy to the target agent instructions:

- Use `find_docs` / `locate_topic` before reading files.
- Use `open_doc` only for returned citations.
- Do not use shell search or broad `rg` as fallback when the semantic index is blocked.
- If `search_mode=degraded`, keep using the returned citations; confidence is capped while SemRAGent refreshes in the background.
- If `search_mode=blocked`, follow `owner_action`; this should usually mean missing/empty catalog or too many changed docs, not a normal restart.
- Use `search_exact` only for explicit symbol/path/config-key lookups.
- If `index_status.exact_available=true`, exact lookup is allowed only for those explicit terms; otherwise retry `find_docs` / `locate_topic` after `retry_after_seconds`.
- If the index is `usable_stale` or `degraded`, the MCP should still answer from the catalog/previous index, cap confidence at medium, and refresh in the background after the current search when possible.

This is the intended usage pattern for agents:

1. Start with `index_status` only when checking readiness or debugging.
2. For docs, use `find_docs` or `locate_topic`.
3. Open only returned citations with `open_doc`.
4. For literal terms such as `PaymentWebhookHandler`, `SITE_GATE_PASSWORD`, `runner_flavor`, or a file path, use `search_exact`.
5. If blocked, follow `owner_action` instead of running `rg` or reading random files.

## Project Config

`semragent init` creates:

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
semragent doctor
semragent qdrant status
semragent models doctor
semragent index-status
```

Common blockers:

- Qdrant is not running.
- The BGE models are not downloaded or cannot load.
- CUDA PyTorch is missing or CPU-only.
- The catalog is empty because docs roots are wrong.
- Too many tracked docs/catalog files changed for the old index to be safe.
- The semantic index is incompatible after model/backend/config changes.

When MCP search is blocked, the response includes `owner_action`. Agents should not invent fallback behavior.

During first indexing or after a restart, `find_docs`, `locate_topic`, and `prepare_context` should stay usable as long as the SQLite catalog exists. If Qdrant is empty, unavailable, or rebuilding, SemRAGent returns `search_mode=degraded`, caps confidence at medium, uses catalog/FTS/entity/alias signals, and starts background indexing when safe. It returns `search_mode=blocked` only when the catalog is missing/empty or too many tracked docs/catalog files changed for the old index to be trustworthy.

Background indexing is opportunistic, not a daemon. Workers are launched only when the MCP detects a stale/degraded index and auto-indexing is allowed. The worker receives the parent MCP process PID, exits if that parent disappears, and also stops after `auto_index.max_runtime_seconds` (default: `3600`). This prevents an agent session from leaving a long-running BGE/Qdrant process consuming GPU all day.

## Local Model Commands

SemRAGent is strict about local models:

- embedding model: `BAAI/bge-m3`;
- embedding backend: `FlagEmbedding.BGEM3FlagModel`;
- reranker model: `BAAI/bge-reranker-v2-m3`;
- reranker backend: `FlagEmbedding.FlagReranker`;
- dense embedding dimension: `1024`;
- no fallback model.

```powershell
semragent models fetch
semragent models doctor
semragent models bench
```

Set `models.offline_runtime: true` only after the models are cached locally. In that mode SemRAGent sets `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` at runtime.

## Eval And Authority Lint

```powershell
semragent eval bootstrap
semragent eval run
semragent eval report
semragent lint-authority
```

`eval bootstrap` creates candidate cases only. Review them into `.workspace-docs/eval-golden.json` before treating failures as product regressions.

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

Clean install smoke:

```powershell
.\scripts\smoke-install.ps1
```

```sh
sh scripts/smoke-install.sh
```

Release artifacts are produced by GitHub Actions and attached to tag pushes matching `v*`.

Model/Qdrant smoke:

```powershell
workspace-docs models doctor
```

## License

MIT. See [LICENSE](LICENSE).
