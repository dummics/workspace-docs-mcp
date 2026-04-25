from __future__ import annotations

import json
import sys
from typing import Any, Callable

from .config import LocatorConfig
from .freshness import IndexFreshnessService
from .runtime import RuntimeContext
from .search import Retriever


def tool_schema() -> list[dict[str, Any]]:
    return [
        {"name": "find_docs", "description": "Document-first authoritative workspace docs locator. Compact output by default; use verbosity=full only for debugging.", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"query": {"type": "string"}, "repo_area": {"type": ["string", "null"]}, "doc_type": {"type": ["string", "null"]}, "include_historical": {"type": "boolean", "default": False}, "max_results": {"type": "integer", "default": 8, "minimum": 1, "maximum": 20}, "rerank": {"type": "boolean", "default": True}, "verbosity": {"type": "string", "enum": ["compact", "full"], "default": "compact"}}, "required": ["query"]}},
        {"name": "locate_topic", "description": "Section-first topic locator with citations. Compact output by default; use verbosity=full only for debugging.", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"query": {"type": "string"}, "repo_area": {"type": ["string", "null"]}, "include_historical": {"type": "boolean", "default": False}, "max_sections": {"type": "integer", "default": 8, "minimum": 1, "maximum": 20}, "rerank": {"type": "boolean", "default": True}, "verbosity": {"type": "string", "enum": ["compact", "full"], "default": "compact"}}, "required": ["query"]}},
        {"name": "open_doc", "description": "Open a catalog-known workspace markdown document or line range. Blocks traversal outside workspace.", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"path": {"type": "string"}, "heading": {"type": ["string", "null"]}, "line_start": {"type": ["integer", "null"], "minimum": 1}, "line_end": {"type": ["integer", "null"], "minimum": 1}, "max_chars": {"type": "integer", "default": 12000, "minimum": 1, "maximum": 50000}}, "required": ["path"]}},
        {"name": "search_exact", "description": "Exact lookup for explicit symbols, paths, config keys, route IDs, or manifest names. Do not use as fallback after find_docs/locate_topic returns blocked or empty.", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"term": {"type": "string"}, "repo_area": {"type": ["string", "null"]}, "include_historical": {"type": "boolean", "default": False}, "max_results": {"type": "integer", "default": 20, "minimum": 1, "maximum": 50}}, "required": ["term"]}},
        {"name": "list_canonical", "description": "List canonical/runbook docs by area/topic.", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"repo_area": {"type": ["string", "null"]}, "topic": {"type": ["string", "null"]}}}},
        {"name": "doc_neighbors", "description": "Show links and nearby canonical docs for a document.", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"path": {"type": "string"}, "include_historical": {"type": "boolean", "default": False}}, "required": ["path"]}},
        {"name": "explain_result", "description": "Explain why a path was or was not selected for a query; path may be null for no-results diagnostics.", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"query": {"type": "string"}, "path": {"type": ["string", "null"]}}, "required": ["query"]}},
        {"name": "index_status", "description": "Read-only readiness report for the local locator index, Qdrant, exact local models, and fallback-disabled policy.", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}},
    ]


def call_tool(config_or_context: LocatorConfig | RuntimeContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    context = config_or_context if isinstance(config_or_context, RuntimeContext) else RuntimeContext(config_or_context)
    config = context.config
    retriever = context.retriever
    if name == "find_docs":
        preflight = preflight_search(config, str(args["query"]))
        if preflight:
            return preflight
        index_status = IndexFreshnessService(config).status(allow_auto_start=False)
        confidence_cap = "medium" if index_status.get("state") == "usable_stale" else None
        return attach_index_status(config, retriever.search(args["query"], args.get("repo_area"), args.get("doc_type"), bool(args.get("include_historical", False)), int(args.get("max_results", 8)), bool(args.get("rerank", True)), verbosity=str(args.get("verbosity", "compact")), mode="documents", confidence_cap=confidence_cap), index_status)
    if name == "locate_topic":
        preflight = preflight_search(config, str(args["query"]))
        if preflight:
            return preflight
        index_status = IndexFreshnessService(config).status(allow_auto_start=False)
        confidence_cap = "medium" if index_status.get("state") == "usable_stale" else None
        return attach_index_status(config, retriever.search(args["query"], args.get("repo_area"), None, bool(args.get("include_historical", False)), int(args.get("max_sections", 8)), bool(args.get("rerank", True)), dedupe_documents=False, verbosity=str(args.get("verbosity", "compact")), mode="sections", confidence_cap=confidence_cap), index_status)
    if name == "open_doc":
        return retriever.open_doc(args["path"], args.get("heading"), args.get("line_start"), args.get("line_end"), int(args.get("max_chars", 12000)))
    if name == "search_exact":
        return retriever.exact(args["term"], args.get("repo_area"), bool(args.get("include_historical", False)), int(args.get("max_results", 20)))
    if name == "list_canonical":
        return retriever.list_canonical(args.get("repo_area"), args.get("topic"))
    if name == "doc_neighbors":
        return retriever.neighbors(args["path"], bool(args.get("include_historical", False)))
    if name == "explain_result":
        return retriever.explain(args["query"], args.get("path"))
    if name == "index_status":
        from .catalog import Catalog
        from .vector import VectorIndex

        qdrant_ok, qdrant_warning = VectorIndex(config).available()
        index_status = IndexFreshnessService(config).status(allow_auto_start=False)
        return {
            "server": "workspace-docs-mcp",
            "mode": "read-only",
            "agent_pattern": "Use find_docs or locate_topic first, then open_doc only for returned citations.",
            "sqlite": str(config.sqlite_path),
            "catalog": compact_catalog_stats(Catalog(config).stats()),
            "index_status": compact_index_status(index_status),
            "qdrant_available": qdrant_ok,
            "qdrant_warning": qdrant_warning,
            "owner_action": owner_action(index_status) if index_status.get("state") == "blocked" else None,
            "embedding_model": config.embedding_model,
            "embedding_backend": config.embedding_backend,
            "embedding_dim": config.data["models"].get("require_embedding_dimension"),
            "reranker_model": config.reranker_model,
            "reranker_backend": config.reranker_backend,
            "require_reranker": config.data["models"].get("require_reranker"),
            "allow_model_fallback": config.data["models"].get("allow_model_fallback"),
            "tools": [tool["name"] for tool in tool_schema()],
        }
    raise ValueError(f"unknown tool: {name}")


def preflight_search(config: LocatorConfig, query: str) -> dict[str, Any] | None:
    index_status = IndexFreshnessService(config).status(allow_auto_start=True)
    if index_status.get("state") != "blocked":
        return None
    return {
        "query": query,
        "intent": "locate_doc",
        "search_mode": "blocked",
        "confidence": "low",
        "confidence_reasons": ["semantic index is blocked"],
        "warnings": index_status.get("warnings", []),
        "results": [],
        "suggested_next_queries": [],
        "owner_action": owner_action(index_status),
        "index_status": compact_index_status(index_status),
    }


def owner_action(index_status: dict[str, Any]) -> str:
    background = index_status.get("background_index") or {}
    if background.get("state") in {"started", "running"}:
        detail = []
        if background.get("pid"):
            detail.append(f"pid={background['pid']}")
        if background.get("elapsed_seconds") is not None:
            detail.append(f"elapsed={background['elapsed_seconds']}s")
        if background.get("log_path"):
            detail.append(f"log={background['log_path']}")
        suffix = f" ({', '.join(detail)})" if detail else ""
        return f"Background indexing is running; retry the same find_docs/locate_topic query after retry_after_seconds.{suffix}"
    if "qdrant_unavailable" in index_status.get("reasons", []):
        return "Start Qdrant at the configured URL, then run workspace-docs index build."
    return "Run workspace-docs index build or fix the reported model/index blocker. No fallback model is allowed."


def attach_index_status(config: LocatorConfig, result: dict[str, Any], index_status: dict[str, Any]) -> dict[str, Any]:
    result["index_status"] = compact_index_status(index_status)
    result.setdefault("warnings", [])
    for warning in index_status.get("warnings", []):
        if warning not in result["warnings"]:
            result["warnings"].append(warning)
    background = index_status.get("background_index", {})
    if background.get("state") == "started":
        result["warnings"].append("background_index_started")
    elif background.get("state") == "running":
        result["warnings"].append("background_index_running")
    elif background.get("state") == "skipped" and background.get("reason") not in {"debounce"}:
        result["warnings"].append(f"background_index_skipped:{background.get('reason')}")
    if index_status.get("state") == "usable_stale":
        result["warnings"].append("index_usable_stale: confidence capped at medium")
    return result


def compact_catalog_stats(stats: dict[str, Any]) -> dict[str, Any]:
    last_run = stats.get("last_run") or {}
    return {
        "documents": stats.get("documents", 0),
        "chunks": stats.get("chunks", 0),
        "symbols": stats.get("symbols", 0),
        "last_run": {
            "completed_at": last_run.get("completed_at"),
            "git_commit": last_run.get("git_commit"),
            "embedding_backend": last_run.get("embedding_backend"),
            "reranker_backend": last_run.get("reranker_backend"),
            "chunker_version": last_run.get("chunker_version"),
        }
        if last_run
        else None,
    }


def compact_index_status(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": status.get("state"),
        "safe_to_use": status.get("safe_to_use"),
        "warnings": status.get("warnings", [])[:5],
        "changed_files_count": status.get("changed_files_count", 0),
        "qdrant_counts": status.get("qdrant_counts", {}),
        "background_index": {
            "state": (status.get("background_index") or {}).get("state"),
            "reason": (status.get("background_index") or {}).get("reason"),
            "pid": (status.get("background_index") or {}).get("pid"),
            "started_at": (status.get("background_index") or {}).get("started_at"),
            "elapsed_seconds": (status.get("background_index") or {}).get("elapsed_seconds"),
            "retry_after_seconds": (status.get("background_index") or {}).get("retry_after_seconds"),
            "log_path": (status.get("background_index") or {}).get("log_path"),
        },
    }


def response(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def run_stdio(config: LocatorConfig) -> None:
    context = RuntimeContext(config)
    for line in sys.stdin:
        req_id = None
        try:
            req = json.loads(line)
            method = req.get("method")
            req_id = req.get("id")
            params = req.get("params") or {}
            if method == "initialize":
                out = response(req_id, {"protocolVersion": "2024-11-05", "serverInfo": {"name": "workspace-docs-mcp", "version": "0.1.0"}, "capabilities": {"tools": {}}})
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                out = response(req_id, {"tools": tool_schema()})
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                result = call_tool(context, name, args)
                out = response(req_id, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]})
            else:
                out = error(req_id, -32601, f"method not found: {method}")
        except Exception as exc:
            out = error(req_id, -32000, str(exc))
        print(json.dumps(out, ensure_ascii=False), flush=True)

