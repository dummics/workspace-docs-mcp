from __future__ import annotations

import argparse
import os
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

from .catalog import Catalog
from .config import load_config
from .freshness import process_alive


def start_lifetime_guard(parent_pid: int | None, orphan_check_seconds: int, max_runtime_seconds: int) -> None:
    started = monotonic()

    def guard() -> None:
        while True:
            sleep(max(1, orphan_check_seconds))
            if parent_pid and not process_alive(parent_pid):
                os._exit(3)
            if max_runtime_seconds > 0 and monotonic() - started > max_runtime_seconds:
                os._exit(4)

    Thread(target=guard, name="workspace-docs-index-worker-lifetime", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workspace-docs-index-worker")
    parser.add_argument("--root", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--orphan-check-seconds", type=int, default=5)
    parser.add_argument("--max-runtime-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    lock = Path(args.lock).resolve()
    result_path = root / ".rag" / "index-worker-last-result.json"
    started = datetime.now(timezone.utc).isoformat()
    start_lifetime_guard(args.parent_pid, args.orphan_check_seconds, args.max_runtime_seconds)
    try:
        config = load_config(root)
        result = Catalog(config).update()
        payload = {
            "ok": not bool(result.get("errors")),
            "started_at": started,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "docs": result.get("docs"),
            "chunks": result.get("chunks"),
            "errors": result.get("errors", [])[:20],
            "warnings_count": len(result.get("warnings", [])),
        }
        result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return 0 if payload["ok"] else 1
    except Exception as exc:
        payload = {
            "ok": False,
            "started_at": started,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return 1
    finally:
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

