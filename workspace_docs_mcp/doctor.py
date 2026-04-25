from __future__ import annotations

from typing import Any

from .authority_lint import lint_authority
from .catalog import Catalog
from .config import LocatorConfig
from .freshness import IndexFreshnessService
from .qdrant_cli import qdrant_status


def run_doctor(config: LocatorConfig, *, check_models: bool = True) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    owner_commands: list[str] = []

    def add(status: str, message: str) -> None:
        checks.append({"status": status, "message": message})

    if (config.root / ".workspace-docs" / "locator.config.yml").exists():
        add("OK", "SemRAGent config found")
    else:
        add("WARN", "SemRAGent config not found; defaults are in use")
        owner_commands.append("semragent init")
    add("OK", f"workspace root detected: {config.root}")

    catalog = Catalog(config)
    sqlite_existed = config.sqlite_path.exists()
    stats = catalog.stats()
    if sqlite_existed:
        add("OK", "SQLite catalog exists")
    else:
        add("FAIL", "SQLite catalog missing")
        owner_commands.append("semragent index build")
    if int(stats.get("documents", 0) or 0) > 0 and int(stats.get("chunks", 0) or 0) > 0:
        add("OK", f"SQLite catalog ready: {stats.get('documents')} docs / {stats.get('chunks')} chunks")
    else:
        add("FAIL", "SQLite catalog empty")
        owner_commands.append("semragent index build")

    qstatus = qdrant_status(config)
    if qstatus.get("ok"):
        add("OK", "Qdrant reachable")
        names = {item["name"]: item["points"] for item in qstatus.get("collections", [])}
        for collection in [config.data["index"]["qdrant_collection_docs"], config.data["index"]["qdrant_collection_chunks"]]:
            if collection in names:
                points = int(names[collection] or 0)
                add("OK" if points > 0 else "WARN", f"{collection} ready: {points} points")
                if points <= 0:
                    owner_commands.append("semragent index build")
            else:
                add("WARN" if int(stats.get("documents", 0) or 0) > 0 else "FAIL", f"{collection} missing")
                owner_commands.append("semragent index build")
    else:
        if int(stats.get("documents", 0) or 0) > 0 and int(stats.get("chunks", 0) or 0) > 0:
            add("WARN", f"Qdrant unreachable; catalog/FTS remains usable: {qstatus.get('error')}")
        else:
            add("FAIL", f"Qdrant unreachable: {qstatus.get('error')}")
        owner_commands.append("semragent qdrant start")

    freshness = IndexFreshnessService(config).status(allow_auto_start=False)
    if freshness.get("state") == "fresh":
        add("OK", "index compatibility fresh")
    elif freshness.get("state") == "usable_stale":
        add("WARN", "index is usable but stale")
    elif freshness.get("state") == "degraded":
        add("WARN", f"semantic index degraded but catalog is usable: {', '.join(freshness.get('reasons', []))}")
        owner_commands.append("semragent index build")
    else:
        add("FAIL", f"index blocked: {', '.join(freshness.get('reasons', []))}")
        owner_commands.append("semragent index build")

    if config.data["models"].get("allow_model_fallback"):
        add("FAIL", "model fallback is enabled")
    else:
        add("OK", "fallback disabled")
    if check_models:
        try:
            from .cli import models_doctor

            result = models_doctor(config)
            for item in result.get("checks", []):
                add(str(item["status"]), str(item["message"]))
            if not result.get("ok"):
                owner_commands.append("semragent models fetch")
        except Exception as exc:
            add("FAIL", f"models doctor failed: {exc}")
            owner_commands.append("semragent models fetch")

    lint = lint_authority(config)
    inferred = sum(1 for item in lint["warnings"] if item.get("code") == "inferred_status")
    no_alias = sum(1 for item in lint["warnings"] if item.get("code") == "canonical_without_aliases")
    if inferred:
        add("WARN", f"{inferred} docs have inferred status")
    if no_alias:
        add("WARN", f"{no_alias} canonical docs have no aliases")
    for failure in lint.get("failures", []):
        add("FAIL", f"{failure.get('code')}: {failure.get('topic') or failure.get('path')}")

    deduped_commands = list(dict.fromkeys(owner_commands))
    return {"ok": not any(c["status"] == "FAIL" for c in checks), "checks": checks, "owner_action": {"commands": deduped_commands, "safe_for_agent": False} if deduped_commands else None, "index_status": freshness}
