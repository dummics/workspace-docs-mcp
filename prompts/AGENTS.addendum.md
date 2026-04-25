# Workspace Docs MCP Agent Addendum

Use the Workspace Docs MCP as the primary documentation locator for this workspace.

## Default Flow

1. Call `find_docs` for document-level questions.
2. Call `locate_topic` when a heading/section citation is more useful.
3. Call `open_doc` only for citations returned by those tools.

## Do Not Fallback To Grep

If `find_docs` or `locate_topic` returns `search_mode="blocked"`:

- do not run broad `rg`, grep, or random file reads as a substitute;
- read `owner_action`;
- if background indexing is running, wait briefly and retry the same semantic query;
- if still blocked, tell the owner what is missing.

Use `search_exact` only when the user asks for an explicit symbol, path, config key, route id, error code, or manifest name.

## Confidence

- `high`: safe to open/read the cited doc.
- `medium`: usable, but mention ambiguity or stale index warning when relevant.
- `low`: do not rely on it as authoritative without owner action or a narrower query.

## Token Discipline

Use compact tool output by default. Use `verbosity="full"` or `explain_result` only to debug no-results, stale index, excluded docs, or ranking surprises.

