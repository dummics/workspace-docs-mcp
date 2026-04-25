from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from workspace_docs_mcp.config import load_config
from workspace_docs_mcp.freshness import IndexFreshnessService
from workspace_docs_mcp.mcp_server import preflight_search
from workspace_docs_mcp.search import score


class IndexFreshnessTests(unittest.TestCase):
    def test_scores_keep_three_decimals(self) -> None:
        self.assertEqual(score(0.7556), 0.756)
        self.assertEqual(score(1.234), 1.0)

    def test_auto_index_disabled_skips_worker(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            (root / "project.json").write_text("{}", encoding="utf-8")
            (root / "catalog").mkdir()
            (root / "catalog" / "bootstrap.json").write_text("{}", encoding="utf-8")
            config = load_config(root)
            config.data["auto_index"]["enabled"] = False

            result = IndexFreshnessService(config).maybe_start_background_index("usable_stale", ["docs/a.md"], True)

            self.assertEqual(result["state"], "skipped")
            self.assertEqual(result["reason"], "auto_index_disabled")

    def test_auto_index_skips_large_delta(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            (root / "project.json").write_text("{}", encoding="utf-8")
            (root / "catalog").mkdir()
            (root / "catalog" / "bootstrap.json").write_text("{}", encoding="utf-8")
            config = load_config(root)
            config.data["auto_index"]["max_changed_files"] = 1

            result = IndexFreshnessService(config).maybe_start_background_index("usable_stale", ["docs/a.md", "docs/b.md"], True)

            self.assertEqual(result["state"], "skipped")
            self.assertEqual(result["reason"], "too_many_changed_files")

    def test_preflight_does_not_start_background_for_usable_stale(self) -> None:
        config = load_config(Path.cwd())
        stale = {"state": "usable_stale", "safe_to_use": True, "warnings": ["index_stale"], "background_index": {"state": "idle"}}

        with patch("workspace_docs_mcp.mcp_server.IndexFreshnessService.status", return_value=stale) as status:
            result = preflight_search(config, "architecture overview")

        self.assertIsNone(result)
        status.assert_called_once_with(allow_auto_start=False)

    def test_background_worker_is_parent_bound(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            config = load_config(root)
            config.data["auto_index"]["max_runtime_seconds"] = 123
            config.data["auto_index"]["orphan_check_seconds"] = 2
            process = Mock()
            process.pid = 4242

            with patch("workspace_docs_mcp.freshness.subprocess.Popen", return_value=process) as popen:
                result = IndexFreshnessService(config).start_background_index("usable_stale", ["docs/a.md"])

            self.assertEqual(result["state"], "started")
            command = popen.call_args.args[0]
            self.assertIn("--parent-pid", command)
            self.assertIn("--max-runtime-seconds", command)
            self.assertIn("123", command)
            self.assertIn("--orphan-check-seconds", command)
            self.assertIn("2", command)
            lock = json.loads((root / ".rag" / "index.lock").read_text(encoding="utf-8"))
            self.assertEqual(lock["pid"], 4242)
            self.assertIn("parent_pid", lock)


if __name__ == "__main__":
    unittest.main()

