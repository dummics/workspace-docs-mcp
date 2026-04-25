# Security Policy

Workspace Docs MCP is a local-first read-only documentation locator.

## Supported Versions

The project is pre-1.0. Security fixes target the current `main` branch.

## Security Model

- MCP tools must remain read-only.
- No MCP tool may execute arbitrary shell commands.
- `open_doc` must block path traversal and only open catalog-known workspace files.
- SQLite and Qdrant are rebuildable local caches.
- No cloud API is required by the base product.

## Reporting

Please open a private security advisory or contact the maintainer before publishing a vulnerability.

Useful details:

- OS and Python version.
- MCP client.
- Reproduction steps.
- Whether the issue allows reading files outside the configured workspace.
