# GPT Pro Review Prompt

I uploaded a ZIP of `workspace-docs-mcp`, a local-first MCP server and CLI for locating authoritative documentation inside arbitrary software workspaces.

Please review it as a senior software architect plus pragmatic retrieval/MCP implementer.

## Goal

Help turn this into a strong MVP for agents that should use the MCP as the primary way to locate docs instead of manually grepping or reading random files.

This is not meant to be a generative RAG chat system. The core job is:

> Given a query, topic, symbol, concept, or path, return the right document locations with path, heading, line range, status/authority, confidence, citations, and a concise ranking explanation.

## Current Direction

- Generic product name: `workspace-docs-mcp`.
- Project-specific knowledge should live in adapters/config, not in the core.
- Licensing Framework is only an example fixture, not the product boundary.
- Source of truth is Git plus Markdown plus manifests/config.
- SQLite/Qdrant/vector indexes are rebuildable caches.
- MCP tools must stay read-only.
- No arbitrary shell execution through MCP.
- No required cloud dependencies.
- Embeddings and reranking should be local.
- Retrieval should be hybrid and explainable, not cosine-only.

## What I Want You To Evaluate

1. Architecture gaps for a robust MVP.
2. Whether the generic core is clean enough or still too project-specific.
3. MCP tool design: names, schemas, token efficiency, useful defaults, and agent usability.
4. Retrieval ranking: intent detection, entity/alias resolution, structured manifest lookup, BM25/sparse, vector, reranker, authority policy, and confidence.
5. Glossaries as first-class sources for definition/naming/domain-model queries.
6. Background indexing behavior: when to warn, when to self-index, when to block.
7. Debuggability: how to explain no-results, stale index, excluded docs, missing aliases, or bad authority rules.
8. Eval strategy and regression tests, especially for real project fixtures.
9. Security boundaries: read-only MCP, path traversal, no arbitrary command execution, local-only model behavior.
10. Practical packaging and setup flow for Codex-style agents.

## Constraints

- Prefer a small, useful MVP over an enterprise platform.
- Do not recommend mandatory cloud APIs.
- Do not recommend a full web dashboard as MVP.
- Do not make the agent workflow multi-step or manual-heavy.
- Do not make exact search a fallback that replaces semantic/hybrid retrieval.
- If the index is blocked, the MCP should say exactly what is missing and what the owner or background worker must do.
- Scores should be compact and comparable, ideally decimals from `0.000` to `1.000`.

## Desired Output

Please return:

1. Findings by severity.
2. The smallest final-MVP change set you recommend.
3. Concrete file/module-level implementation suggestions.
4. MCP tool/schema changes, if any.
5. Retrieval and ranking improvements.
6. Eval cases that should be added.
7. Packaging/setup improvements.
8. A short "Codex continuation prompt" that I can paste back into Codex to implement your recommended next tranche.

