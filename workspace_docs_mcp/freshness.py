from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .config import LocatorConfig
from .markdown import git_commit
from .vector import VectorIndex


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class IndexFreshnessService:
    """Keeps the MCP search flow simple while index maintenance stays controlled."""

    def __init__(self, config: LocatorConfig):
        self.config = config
        auto = config.data.get("auto_index", {})
        self.lock_path = (config.root / auto.get("lock_path", ".rag/index.lock")).resolve()
        self.last_start_path = (config.root / auto.get("last_start_path", ".rag/index-worker-last-start.json")).resolve()
        self.log_path = (config.root / auto.get("log_path", ".rag/logs/index-worker.log")).resolve()

    def status(self, *, allow_auto_start: bool = False) -> dict[str, Any]:
        catalog = Catalog(self.config)
        stats = catalog.stats()
        last_run = stats.get("last_run")
        warnings: list[str] = []
        reasons: list[str] = []
        state = "fresh"
        safe_to_use = True

        current_commit = git_commit(self.config.root)
        qdrant_ok, qdrant_warning = VectorIndex(self.config).available()
        qdrant_counts = self.qdrant_counts() if qdrant_ok else {}
        changed_files = self.changed_files(last_run.get("git_commit") if last_run else None)

        if not last_run or stats.get("documents", 0) == 0 or stats.get("chunks", 0) == 0:
            state = "blocked"
            safe_to_use = False
            reasons.append("catalog_missing_or_empty")
            warnings.append("index_missing: run or allow background index build")
        if not qdrant_ok:
            state = "blocked"
            safe_to_use = False
            reasons.append("qdrant_unavailable")
            warnings.append(qdrant_warning or "qdrant_unavailable")
        elif qdrant_counts.get("chunks", 0) <= 0 or qdrant_counts.get("documents", 0) <= 0:
            state = "blocked"
            safe_to_use = False
            reasons.append("qdrant_collections_empty")
            warnings.append("qdrant_collections_empty: background index build needed")

        if last_run:
            for field, expected in [
                ("embedding_model", self.config.embedding_model),
                ("embedding_backend", self.config.embedding_backend),
                ("reranker_model", self.config.reranker_model),
                ("reranker_backend", self.config.reranker_backend),
                ("chunker_version", self.config.chunker_version),
            ]:
                if str(last_run.get(field)) != str(expected):
                    state = "blocked"
                    safe_to_use = False
                    reasons.append(f"{field}_changed")
                    warnings.append(f"index_incompatible: {field} changed")

        if changed_files:
            if state == "fresh":
                state = "usable_stale"
            reasons.append("workspace_docs_changed")
            warnings.append(f"index_stale: {len(changed_files)} tracked docs/catalog files changed since last index")

        background = self.background_state()
        if allow_auto_start and state != "fresh":
            background = self.maybe_start_background_index(state, changed_files, qdrant_ok)

        return {
            "state": state,
            "safe_to_use": safe_to_use,
            "reasons": reasons,
            "warnings": warnings,
            "current_git_commit": current_commit,
            "indexed_git_commit": last_run.get("git_commit") if last_run else None,
            "changed_files_count": len(changed_files),
            "changed_files_sample": changed_files[:10],
            "sqlite": str(self.config.sqlite_path),
            "catalog_documents": stats.get("documents", 0),
            "catalog_chunks": stats.get("chunks", 0),
            "qdrant_available": qdrant_ok,
            "qdrant_warning": qdrant_warning,
            "qdrant_counts": qdrant_counts,
            "background_index": background,
        }

    def qdrant_counts(self) -> dict[str, int]:
        try:
            from qdrant_client import QdrantClient  # type: ignore

            client = QdrantClient(url=self.config.data["index"]["qdrant_url"])
            return {
                "documents": int(client.count(self.config.data["index"]["qdrant_collection_docs"], exact=True).count),
                "chunks": int(client.count(self.config.data["index"]["qdrant_collection_chunks"], exact=True).count),
            }
        except Exception:
            return {}

    def changed_files(self, indexed_commit: str | None) -> list[str]:
        paths: set[str] = set()
        roots = tuple(str(p).replace("\\", "/").rstrip("/") for p in self.config.data["paths"]["docs_roots"])
        tracked_keys = ["manifest_files", "navigation_files", "generated_index_files", "route_files", "alias_files", "entity_sources"]
        manifests: set[str] = set()
        for key in tracked_keys:
            for value in self.config.data["paths"].get(key, []):
                norm = str(value).replace("\\", "/")
                if "*" not in norm:
                    manifests.add(norm)

        def include(path: str) -> bool:
            norm = path.replace("\\", "/")
            if "/.work/" in f"/{norm}/" or norm.startswith(".work/"):
                return False
            if norm in manifests:
                return True
            if not norm.lower().endswith((".md", ".json", ".jsonl", ".yml", ".yaml")):
                return False
            return any(norm == root or norm.startswith(root + "/") for root in roots)

        if indexed_commit:
            result = self.run_git(["diff", "--name-only", indexed_commit, "HEAD"])
            for line in result.splitlines():
                if include(line):
                    paths.add(line.replace("\\", "/"))
        result = self.run_git(["status", "--porcelain"])
        for line in result.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if include(path):
                paths.add(path.replace("\\", "/"))
        return sorted(paths)

    def run_git(self, args: list[str]) -> str:
        try:
            return subprocess.run(["git", *args], cwd=self.config.root, text=True, capture_output=True, check=False).stdout
        except Exception:
            return ""

    def background_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"state": "idle", "lock_path": str(self.lock_path), "log_path": str(self.log_path)}
        if self.lock_path.exists():
            state["state"] = "running"
            try:
                state["lock"] = json.loads(self.lock_path.read_text(encoding="utf-8"))
            except Exception:
                state["lock"] = {"raw": self.lock_path.read_text(encoding="utf-8", errors="ignore")}
            started_at = state.get("lock", {}).get("started_at") if isinstance(state.get("lock"), dict) else None
            started = parse_time(started_at)
            if started:
                state["started_at"] = started.isoformat()
                state["elapsed_seconds"] = max(0, int((utc_now() - started).total_seconds()))
            state["retry_after_seconds"] = int(self.config.data.get("auto_index", {}).get("retry_after_seconds", 15))
        if self.last_start_path.exists():
            try:
                state["last_start"] = json.loads(self.last_start_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return state

    def maybe_start_background_index(self, index_state: str, changed_files: list[str], qdrant_ok: bool) -> dict[str, Any]:
        auto = self.config.data.get("auto_index", {})
        if not auto.get("enabled", True):
            return {"state": "skipped", "reason": "auto_index_disabled"}
        if self.lock_path.exists():
            return self.background_state()
        if not qdrant_ok:
            return {"state": "skipped", "reason": "qdrant_unavailable"}
        max_changed = int(auto.get("max_changed_files", 20))
        if changed_files and len(changed_files) > max_changed:
            return {"state": "skipped", "reason": "too_many_changed_files", "changed_files_count": len(changed_files)}
        debounce = int(auto.get("debounce_seconds", 600))
        last = self.last_background_start()
        if last and (utc_now() - last).total_seconds() < debounce:
            return {"state": "skipped", "reason": "debounce", "last_start": last.isoformat()}
        return self.start_background_index(index_state, changed_files)

    def last_background_start(self) -> datetime | None:
        if not self.last_start_path.exists():
            return None
        try:
            data = json.loads(self.last_start_path.read_text(encoding="utf-8"))
            return parse_time(data.get("started_at"))
        except Exception:
            return None

    def start_background_index(self, index_state: str, changed_files: list[str]) -> dict[str, Any]:
        started = utc_now().isoformat()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.last_start_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"started_at": started, "reason": index_state, "changed_files_count": len(changed_files)}
        self.lock_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.last_start_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        env = os.environ.copy()
        command = [
            sys.executable,
            "-m",
            "workspace_docs_mcp.index_worker",
            "--root",
            str(self.config.root),
            "--lock",
            str(self.lock_path),
        ]
        log = self.log_path.open("a", encoding="utf-8")
        creationflags = 0
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        try:
            process = subprocess.Popen(
                command,
                cwd=self.config.root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=False,
            )
            payload["pid"] = process.pid
            self.lock_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self.last_start_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return {"state": "started", "pid": process.pid, "log_path": str(self.log_path), "reason": index_state, "started_at": started, "elapsed_seconds": 0, "retry_after_seconds": int(self.config.data.get("auto_index", {}).get("retry_after_seconds", 15))}
        except Exception as exc:
            try:
                self.lock_path.unlink(missing_ok=True)
            except Exception:
                pass
            return {"state": "failed_to_start", "error": str(exc)}

