# Contributing

Thanks for helping improve Workspace Docs MCP.

## Development Setup

```powershell
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
```

For model/Qdrant integration work:

```powershell
python -m pip install -e ".[all]"
workspace-docs models doctor
```

## Design Constraints

- Keep MCP tools read-only.
- Do not add arbitrary shell execution through MCP.
- Do not add mandatory cloud APIs.
- Do not replace `BAAI/bge-m3` or `BAAI/bge-reranker-v2-m3` with fallback models.
- Keep source of truth in Git/Markdown/manifests; SQLite and Qdrant are caches.
- Prefer small, testable MVP changes.

## Pull Request Checklist

- Add or update tests for behavior changes.
- Keep output compact and agent-friendly.
- Preserve clear `owner_action` messages for blocked states.
- Document new setup steps in `README.md`.
- Avoid project-specific assumptions in core code; put examples under `examples/`.
