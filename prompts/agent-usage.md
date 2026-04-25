# Agent Usage Pattern

Workspace Docs MCP exists to reduce manual repository spelunking.

## Preferred Calls

- `find_docs`: "where is the doc/runbook/decision for X?"
- `locate_topic`: "where is the section that explains X?"
- `open_doc`: open only returned citations.
- `index_status`: inspect readiness when search warns or blocks.
- `explain_result`: debug no-results or surprising ranking.
- `search_exact`: explicit symbols/paths/config keys only.

## Blocked Index Behavior

When search returns:

```json
{
  "search_mode": "blocked",
  "confidence": "low",
  "results": [],
  "owner_action": "..."
}
```

Do not replace it with broad shell search. The correct action is to follow `owner_action` or ask the owner/operator to fix the local index/model/Qdrant blocker.

