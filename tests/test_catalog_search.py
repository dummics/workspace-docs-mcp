from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_docs_mcp.catalog import Catalog
from workspace_docs_mcp.config import load_config
from workspace_docs_mcp.freshness import IndexFreshnessService
from workspace_docs_mcp.mcp_server import call_tool, tool_schema
from workspace_docs_mcp.search import Retriever
from workspace_docs_mcp.vector import VectorIndex


class CatalogSearchTests(unittest.TestCase):
    def build_basic_catalog(self, root: Path) -> None:
        docs = root / "docs"
        (docs / "server").mkdir(parents=True)
        (docs / "archive").mkdir(parents=True)
        (docs / "generated").mkdir(parents=True)
        (docs / "tests" / "server" / "areas").mkdir(parents=True)
        (root / "server-repo" / "src" / "Controllers").mkdir(parents=True)
        (root / "server-repo" / "config").mkdir(parents=True)
        (root / "server-repo" / "frontend" / ".next" / "server").mkdir(parents=True)
        (root / "catalog").mkdir(exist_ok=True)
        (root / ".workspace-docs").mkdir(exist_ok=True)
        (root / ".workspace-docs" / "locator.config.yml").write_text("version: 1\n", encoding="utf-8")
        (docs / "server" / "canonical.md").write_text(
            "---\nstatus: canonical\ntitle: Canonical Activation\naliases:\n  - server activation\n---\n# Canonical Activation\n\nLicense activation validates a client request on the server.\n## Server validation\n\nLicenseActivationHandler lives here.\n",
            encoding="utf-8",
        )
        (docs / "server" / "observability.md").write_text(
            "---\nstatus: canonical\ntitle: Server Observability\nrepo_area: server\ndoc_type: architecture\naliases:\n  - health monitoring\n---\n# Server Observability\n\nHealth checks and monitoring cover server component observability.\n",
            encoding="utf-8",
        )
        (docs / "archive" / "old.md").write_text("# Old Activation\n\nLicense activation old note.\n", encoding="utf-8")
        (docs / "generated" / "activation.md").write_text("# Generated Activation\n\nLicense activation generated note.\n", encoding="utf-8")
        (docs / "tests" / "server" / "areas" / "Auth_SystemControllerTests.cs.md").write_text(
            "# Auth SystemControllerTests\n\nGenerated test documentation for SystemController health monitoring observability.\n",
            encoding="utf-8",
        )
        (root / "server-repo" / "src" / "Controllers" / "SystemController.cs").write_text(
            "namespace Demo.Controllers;\npublic sealed class SystemController\n{\n    public string Health() => \"ok\";\n}\n",
            encoding="utf-8",
        )
        (root / "server-repo" / "config" / ".env.example").write_text("SITE_GATE_PASSWORD=change-me\nrunner_flavor=self-hosted\n", encoding="utf-8")
        (root / "server-repo" / "frontend" / ".next" / "server" / "generated.js").write_text("const SITE_GATE_PASSWORD = 'leak';\n", encoding="utf-8")
        with patch("workspace_docs_mcp.catalog.VectorIndex.rebuild_from_sqlite", return_value={"enabled": False, "reason": "unit-test"}):
            Catalog(load_config(root)).rebuild()

    def test_exact_search_and_historical_filter(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            docs = root / "docs"
            (docs / "server").mkdir(parents=True)
            (docs / "archive").mkdir(parents=True)
            (root / "catalog").mkdir()
            (root / "project.json").write_text("{}", encoding="utf-8")
            (root / "catalog" / "bootstrap.json").write_text("{}", encoding="utf-8")
            (docs / "navigation.json").write_text('{"docs":[{"path":"server/canonical.md"}]}', encoding="utf-8")
            (docs / "server" / "canonical.md").write_text("# Canonical Activation\n\nLicenseActivationHandler lives here.\n", encoding="utf-8")
            (docs / "archive" / "old.md").write_text("# Old Activation\n\nLicenseActivationHandler old note.\n", encoding="utf-8")

            config = load_config(root)
            with patch("workspace_docs_mcp.catalog.VectorIndex.rebuild_from_sqlite", return_value={"enabled": False, "reason": "unit-test"}):
                Catalog(config).rebuild()
            retriever = Retriever(config)
            exact = retriever.exact("LicenseActivationHandler", max_results=10)
            paths = [r["path"] for r in exact["results"]]
            self.assertIn("docs/server/canonical.md", paths)
            self.assertNotIn("docs/archive/old.md", paths)

            exact_with_history = retriever.exact("LicenseActivationHandler", include_historical=True, max_results=10)
            historical_paths = [r["path"] for r in exact_with_history["results"]]
            self.assertIn("docs/archive/old.md", historical_paths)

    def test_exact_search_finds_catalog_path_and_lowercase_config_key(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            config = load_config(root)
            path_result = Retriever(config).exact("docs/server/canonical.md")
            config_key_result = Retriever(config).exact("LicenseActivationHandler")

            self.assertEqual(path_result["confidence"], "high")
            self.assertEqual(path_result["results"][0]["path"], "docs/server/canonical.md")
            self.assertEqual(path_result["results"][0]["source_kind"], "catalog_path")
            self.assertTrue(any(item["path"] == "docs/server/canonical.md" for item in config_key_result["results"]))

    def test_exact_search_finds_code_symbol_and_env_key(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            retriever = Retriever(load_config(root))

            symbol = retriever.exact("SystemController", repo_area="server")
            secret = retriever.exact("SITE_GATE_PASSWORD", repo_area="server")

            self.assertEqual(symbol["confidence"], "high")
            self.assertEqual(symbol["results"][0]["source_kind"], "code_symbol")
            self.assertTrue(symbol["results"][0]["path"].endswith("SystemController.cs"))
            self.assertTrue(any(item["source_kind"] == "code_symbol" and item["path"].endswith("SystemController.cs") for item in symbol["results"]))
            self.assertEqual(secret["confidence"], "high")
            self.assertEqual(secret["results"][0]["source_kind"], "config_key")
            self.assertTrue(any(item["source_kind"] == "config_key" and item["path"].endswith(".env.example") for item in secret["results"]))
            self.assertTrue(all("change-me" not in item["snippet"] for item in secret["results"]))
            self.assertTrue(all(".next" not in item["path"] for item in secret["results"]))

    def test_overview_query_promotes_overview_doc(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            (root / "docs" / "README.md").write_text("# Workspace Overview\n\nGeneral architecture overview for server and client components.\n", encoding="utf-8")
            (root / "docs" / "reference").mkdir(parents=True)
            (root / "docs" / "reference" / "package-format.md").write_text("# Package Format\n\nSpecific package reference architecture details.\n", encoding="utf-8")
            with patch("workspace_docs_mcp.catalog.VectorIndex.rebuild_from_sqlite", return_value={"enabled": False, "reason": "unit-test"}):
                Catalog(load_config(root)).rebuild()
            with patch.object(VectorIndex, "search_documents", return_value=[]):
                result = Retriever(load_config(root)).search("architettura generale overview componenti server client", max_results=3, rerank=False, mode="documents")

            self.assertTrue(result["results"])
            self.assertEqual(result["results"][0]["path"], "docs/README.md")
            self.assertIn("overview intent", result["results"][0]["why"])

    def test_source_inventory_counts_files_symbols_and_config_keys(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            stats = Catalog(load_config(root)).stats()

            self.assertGreaterEqual(stats["source_files"], 1)
            self.assertGreaterEqual(stats["code_symbols"], 1)
            self.assertGreaterEqual(stats["config_keys"], 2)

    def test_code_symbol_bridge_boosts_related_docs(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            with patch.object(VectorIndex, "search_chunks", return_value=[]):
                result = Retriever(load_config(root)).search("SystemController health monitoring", repo_area="server", max_results=3, rerank=False)

            self.assertTrue(result["results"])
            self.assertEqual(result["results"][0]["path"], "docs/server/observability.md")
            self.assertIn("code symbol/config bridge", result["results"][0]["why"])

    def test_locate_topic_handles_colon_terms_without_fts_column_error(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            with patch.object(VectorIndex, "search_chunks", return_value=[]):
                result = Retriever(load_config(root)).search("health/readiness after: deployment", repo_area="server", max_results=3, rerank=False)

            self.assertNotIn("no such column", " ".join(result["warnings"]))
            self.assertIn(result["confidence"], {"low", "medium", "high"})

    def test_broad_queries_suppress_generated_test_docs(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            generated_hit = {
                "payload": {
                    "chunk_id": "docs/tests/server/areas/Auth_SystemControllerTests.cs.md#auth-systemcontrollertests:1-3",
                    "status": "inferred",
                    "repo_area": "server",
                    "doc_type": "doc",
                },
                "dense_score": 0.95,
                "sparse_score": 0.95,
                "generator_ranks": {"dense": 1, "sparse": 1},
            }
            canonical_hit = {
                "payload": {
                    "chunk_id": "docs/server/observability.md#server-observability:1-3",
                    "status": "canonical",
                    "repo_area": "server",
                    "doc_type": "architecture",
                },
                "dense_score": 0.75,
                "sparse_score": 0.70,
                "generator_ranks": {"dense": 2, "sparse": 2},
            }
            with patch.object(VectorIndex, "search_chunks", return_value=[generated_hit, canonical_hit]):
                result = Retriever(load_config(root)).search("SystemController health monitoring observability server component", repo_area="server", max_results=3, rerank=False, verbosity="full")

            self.assertTrue(result["results"])
            self.assertEqual(result["results"][0]["path"], "docs/server/observability.md")
            test_hits = [item for item in result["results"] if item["path"].startswith("docs/tests/")]
            self.assertTrue(test_hits)
            self.assertIn("generated_or_test_doc_penalty", test_hits[0]["why"] + test_hits[0].get("policy_adjustments", []))

    def test_rebuild_commits_catalog_before_vector_rebuild(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            docs = root / "docs" / "server"
            docs.mkdir(parents=True)
            (root / ".workspace-docs").mkdir()
            (root / ".workspace-docs" / "locator.config.yml").write_text("version: 1\n", encoding="utf-8")
            (docs / "canonical.md").write_text("# Canonical Activation\n\nLicenseActivationHandler lives here.\n", encoding="utf-8")
            config = load_config(root)

            def vector_rebuild_reads_from_fresh_connection(_conn):
                stats = Catalog(config).stats()
                exact = Retriever(config).exact("docs/server/canonical.md")
                self.assertGreater(stats["documents"], 0)
                self.assertGreater(stats["chunks"], 0)
                self.assertEqual(exact["confidence"], "high")
                return {"enabled": False, "reason": "unit-test"}

            with patch("workspace_docs_mcp.catalog.VectorIndex.rebuild_from_sqlite", side_effect=vector_rebuild_reads_from_fresh_connection):
                Catalog(config).rebuild()

    def test_status_marks_exact_available_when_semantic_build_not_completed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            config = load_config(root)
            with Catalog(config).connect() as conn:
                conn.execute("DELETE FROM index_runs")

            with patch.object(VectorIndex, "available", return_value=(True, None)), patch.object(IndexFreshnessService, "qdrant_counts", return_value={"documents": 10, "chunks": 0}):
                status = IndexFreshnessService(config).status(allow_auto_start=False)

            self.assertEqual(status["state"], "degraded")
            self.assertTrue(status["safe_to_use"])
            self.assertTrue(status["catalog_available_for_exact"])
            self.assertTrue(status["exact_available"])
            self.assertFalse(status["semantic_available"])
            self.assertIn("semantic_index_not_completed", status["reasons"])
            self.assertEqual(status["indexed_root"], str(root.resolve()))

    def test_open_blocks_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            (root / "project.json").write_text("{}", encoding="utf-8")
            (root / "catalog").mkdir()
            (root / "catalog" / "bootstrap.json").write_text("{}", encoding="utf-8")
            config = load_config(root)
            with self.assertRaises(ValueError):
                Retriever(config).open_doc("../outside.md")

    def test_open_truncates_large_catalog_content(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            result = Retriever(load_config(root)).open_doc("docs/server/canonical.md", max_chars=20)

            self.assertTrue(result["truncated"])
            self.assertLessEqual(len(result["content"]), 20)

    def test_glossary_definition_query_returns_glossary_source(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            (root / "domain-definitions.json").write_text(
                '{"definitions":[{"term":"Extractor","aliases":["extractor"],"definition":"Extractor packages workspace docs into a canonical artifact.","canonical_docs":["docs/server/canonical.md"]}]}',
                encoding="utf-8",
            )
            self.build_basic_catalog(root)
            with patch.object(VectorIndex, "search_chunks", return_value=[]):
                result = Retriever(load_config(root)).search("definition of extractor", max_results=3, rerank=False, verbosity="full")

            self.assertTrue(result["results"])
            self.assertEqual(result["results"][0]["source_type"], "glossary")
            self.assertEqual(result["results"][0]["citation"], "domain-definitions.json#L1-L1")

    def test_canonical_beats_historical_and_generated(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            with patch.object(VectorIndex, "search_chunks", return_value=[]):
                result = Retriever(load_config(root)).search("license activation", max_results=3, rerank=False)

            self.assertTrue(result["results"])
            self.assertEqual(result["results"][0]["path"], "docs/server/canonical.md")
            self.assertNotEqual(result["results"][0]["status"], "generated")

    def test_find_docs_uses_document_card_collection(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            hit = {
                "payload": {
                    "document_id": "workspace.docs.server.canonical.md",
                    "path": "docs/server/canonical.md",
                    "status": "canonical",
                    "repo_area": "framework",
                    "doc_type": "doc",
                },
                "dense_score": 0.8,
                "sparse_score": 0.7,
                "generator_ranks": {"dense": 1},
            }
            with patch.object(VectorIndex, "search_documents", return_value=[hit]) as docs_search, patch.object(VectorIndex, "search_chunks", return_value=[]) as chunk_search:
                result = Retriever(load_config(root)).search("server activation", max_results=3, rerank=False, mode="documents")

            docs_search.assert_called_once()
            chunk_search.assert_not_called()
            self.assertEqual(result["results"][0]["path"], "docs/server/canonical.md")
            self.assertIn("best_sections", result["results"][0])

    def test_locate_topic_uses_section_collection(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            chunk_id = "docs/server/canonical.md#server-validation:8-10"
            hit = {"payload": {"chunk_id": chunk_id, "status": "canonical", "repo_area": "framework", "doc_type": "doc"}, "dense_score": 0.8, "sparse_score": 0.7, "generator_ranks": {"dense": 1}}
            with patch.object(VectorIndex, "search_chunks", return_value=[hit]) as chunk_search, patch.object(VectorIndex, "search_documents", return_value=[]) as docs_search:
                result = Retriever(load_config(root)).search("server validation", max_results=3, rerank=False, mode="sections", dedupe_documents=False)

            chunk_search.assert_called_once()
            docs_search.assert_not_called()
            self.assertTrue(result["results"])

    def test_blocked_index_returns_low_confidence_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            blocked = {"state": "blocked", "safe_to_use": False, "warnings": ["index_missing"], "reasons": ["catalog_missing_or_empty"], "background_index": {"state": "skipped", "reason": "unit-test"}}
            with patch("workspace_docs_mcp.mcp_server.IndexFreshnessService.status", return_value=blocked):
                result = call_tool(load_config(root), "find_docs", {"query": "server activation"})

            self.assertEqual(result["search_mode"], "blocked")
            self.assertEqual(result["confidence"], "low")
            self.assertEqual(result["results"], [])
            self.assertIn("catalog_missing_or_empty", result["blocked_by"])
            self.assertFalse(result["owner_action"]["safe_for_agent"])
            self.assertIn("semragent index build", result["owner_action"]["commands"])
            self.assertIn("owner_action", result)

    def test_blocked_index_running_reports_retry_and_log(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            blocked = {
                "state": "blocked",
                "safe_to_use": False,
                "warnings": ["background_index_running"],
                "reasons": ["workspace_docs_changed"],
                "background_index": {"state": "running", "pid": 123, "elapsed_seconds": 7, "retry_after_seconds": 15, "log_path": "x.log"},
            }
            with patch("workspace_docs_mcp.mcp_server.IndexFreshnessService.status", return_value=blocked):
                result = call_tool(load_config(root), "find_docs", {"query": "server activation"})

            self.assertIn("retry_after_seconds", result["owner_action"])
            self.assertFalse(result["owner_action"]["safe_for_agent"])
            self.assertEqual(result["index_status"]["background_index"]["retry_after_seconds"], 15)
            self.assertEqual(result["index_status"]["background_index"]["log_path"], "x.log")
            self.assertIn("indexed_root", result["index_status"])

    def test_usable_stale_caps_confidence_at_medium(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            stale = {"state": "usable_stale", "safe_to_use": True, "warnings": ["index_stale"], "reasons": ["workspace_docs_changed"], "background_index": {"state": "skipped", "reason": "unit-test"}, "semantic_available": True, "exact_available": True}
            with patch("workspace_docs_mcp.mcp_server.IndexFreshnessService.status", return_value=stale), patch.object(VectorIndex, "search_documents", return_value=[]):
                result = call_tool(load_config(root), "find_docs", {"query": "Canonical Activation", "rerank": False})

            self.assertLessEqual({"low": 0, "medium": 1, "high": 2}[result["confidence"]], 1)
            self.assertEqual(result["search_mode"], "degraded")
            self.assertIn("index_usable_stale: confidence capped at medium", result["warnings"])

    def test_qdrant_empty_uses_catalog_degraded_instead_of_blocking(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            config = load_config(root)
            degraded = {
                "state": "degraded",
                "safe_to_use": True,
                "warnings": ["qdrant_collections_empty: using SQLite catalog while background index builds"],
                "reasons": ["qdrant_collections_empty"],
                "background_index": {"state": "started", "retry_after_seconds": 15, "log_path": "x.log"},
                "exact_available": True,
                "semantic_available": False,
            }
            with patch("workspace_docs_mcp.mcp_server.IndexFreshnessService.status", return_value=degraded), patch.object(VectorIndex, "search_documents", return_value=[]):
                result = call_tool(config, "find_docs", {"query": "Canonical Activation", "rerank": False})

            self.assertNotEqual(result["search_mode"], "blocked")
            self.assertEqual(result["search_mode"], "degraded")
            self.assertTrue(result["results"])
            self.assertLessEqual({"low": 0, "medium": 1, "high": 2}[result["confidence"]], 1)
            self.assertFalse(result["semantic_available"])
            self.assertTrue(result["exact_available"])
            self.assertIn("background_index_started", result["warnings"])

    def test_prepare_context_schema_and_results(self) -> None:
        tool_names = [tool["name"] for tool in tool_schema()]
        self.assertNotIn("qdrant_start", tool_names)
        self.assertNotIn("qdrant_stop", tool_names)
        schema = next(tool for tool in tool_schema() if tool["name"] == "prepare_context")
        self.assertFalse(schema["inputSchema"]["additionalProperties"])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            fresh = {"state": "fresh", "safe_to_use": True, "warnings": [], "reasons": [], "background_index": {"state": "idle"}, "exact_available": True}
            with patch("workspace_docs_mcp.mcp_server.IndexFreshnessService.status", return_value=fresh), patch.object(VectorIndex, "search_documents", return_value=[]), patch.object(VectorIndex, "search_chunks", return_value=[]), patch.object(Retriever, "try_rerank", return_value=None):
                result = call_tool(load_config(root), "prepare_context", {"task": "Update SystemController health monitoring docs", "repo_area": "server", "max_docs": 3, "max_sections": 3, "max_symbols": 5})

            self.assertIn("read_first", result)
            self.assertIn("related_symbols", result)
            self.assertTrue(any(item["symbol"] == "SystemController" for item in result["related_symbols"]))

    def test_prepare_context_blocked_mode(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            blocked = {"state": "blocked", "safe_to_use": False, "warnings": ["index_missing"], "reasons": ["qdrant_unavailable"], "background_index": {"state": "skipped", "reason": "unit-test"}, "exact_available": True}
            with patch("workspace_docs_mcp.mcp_server.IndexFreshnessService.status", return_value=blocked):
                result = call_tool(load_config(root), "prepare_context", {"task": "Update activation docs"})

            self.assertEqual(result["search_mode"], "blocked")
            self.assertEqual(result["read_first"], [])
            self.assertFalse(result["owner_action"]["safe_for_agent"])

    def test_prepare_context_degraded_mode_returns_read_targets(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.build_basic_catalog(root)
            degraded = {
                "state": "degraded",
                "safe_to_use": True,
                "warnings": ["qdrant_collections_empty"],
                "reasons": ["qdrant_collections_empty"],
                "background_index": {"state": "skipped", "reason": "unit-test"},
                "exact_available": True,
                "semantic_available": False,
            }
            with patch("workspace_docs_mcp.mcp_server.IndexFreshnessService.status", return_value=degraded), patch.object(VectorIndex, "search_documents", return_value=[]), patch.object(VectorIndex, "search_chunks", return_value=[]), patch.object(Retriever, "try_rerank", return_value=None):
                result = call_tool(load_config(root), "prepare_context", {"task": "Update activation docs", "max_docs": 3, "max_sections": 3})

            self.assertEqual(result["search_mode"], "degraded")
            self.assertTrue(result["read_first"])
            self.assertLessEqual({"low": 0, "medium": 1, "high": 2}[result["confidence"]], 1)
            self.assertFalse(result["index_status"]["semantic_available"])


if __name__ == "__main__":
    unittest.main()

