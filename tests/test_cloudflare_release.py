import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "prepare_cloudflare_release", ROOT / "scripts" / "prepare_cloudflare_release.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load the Cloudflare release preparer")
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)


class CloudflareReleaseTests(unittest.TestCase):
    def _prepare_with_mocks(self, *, skip_sync, database_path=None):
        snapshot = {
            "generated_at": "2026-08-02T12:00:00Z",
            "articles": [{"id": 1}],
            "sources": [{"slug": "example"}],
            "render_llm_tokens_used": 0,
            "llm_tokens_used": 0,
        }
        with (
            mock.patch.object(RELEASE, "_ensure_clean_source") as clean,
            mock.patch.object(RELEASE, "_run") as run,
            mock.patch.object(RELEASE, "_validate_outputs", return_value=snapshot),
            mock.patch.object(RELEASE, "_copy_snapshot") as copy_snapshot,
            mock.patch.object(
                RELEASE,
                "_publication_hashes",
                return_value={"site/data/latest.json": "safe"},
            ) as publication_hashes,
            mock.patch.object(RELEASE, "_verify_build") as verify_build,
            mock.patch.object(
                RELEASE,
                "_commit_snapshot",
                return_value="a" * 40,
            ) as commit_snapshot,
        ):
            result = RELEASE.prepare(
                skip_sync=skip_sync,
                database_path=database_path,
            )
        return {
            "result": result,
            "commands": [call.args[0] for call in run.call_args_list],
            "clean": clean,
            "copy_snapshot": copy_snapshot,
            "publication_hashes": publication_hashes,
            "verify_build": verify_build,
            "commit_snapshot": commit_snapshot,
        }

    def test_git_porcelain_leading_status_column_is_preserved(self):
        status = RELEASE._run(
            ["/bin/echo", " M site/data/latest.json"], capture=True
        )

        self.assertTrue(status.startswith(" M "))
        self.assertEqual([], RELEASE._unexpected_changes(status.splitlines()))

    def test_unrelated_source_change_is_rejected(self):
        self.assertEqual(
            [" M site/app/reader.tsx"],
            RELEASE._unexpected_changes([" M site/app/reader.tsx"]),
        )

    def test_cloud_handoffs_are_allowed_publication_changes(self):
        self.assertEqual(
            [],
            RELEASE._unexpected_changes(
                [" M crawler/latest.json", " M cloud/ai-cache.json"]
            ),
        )

    def test_public_projection_removes_private_reader_state(self):
        original = {
            "counts": {"total": 1, "unread": 1, "starred": 1},
            "cached_ai_artifact_count": 2,
            "cached_ai_report_count": 1,
            "sources": [
                {
                    "slug": "example",
                    "unread_count": 1,
                    "pending_count": 2,
                    "last_error": "/Users/private/error",
                }
            ],
            "articles": [
                {
                    "id": 7,
                    "unread": True,
                    "starred": True,
                    "ai_artifacts": [
                        {
                            "task": "summary",
                            "provider": "private-provenance",
                            "model": "private-model",
                            "output": "legacy summary",
                        },
                        {
                            "task": "translation",
                            "provider": "private-provenance",
                            "model": "private-model",
                            "output": "public translation",
                        },
                    ],
                }
            ],
            "ai_reports": [{
                "period": "daily",
                "provider": "private-provenance",
                "model": "private-model",
                "output": {"headline": "public"},
            }],
        }

        public = RELEASE._public_snapshot(original)

        self.assertEqual({"total": 1}, public["counts"])
        self.assertEqual({"slug": "example"}, public["sources"][0])
        self.assertEqual(
            {
                "id": 7,
                "ai_artifacts": [
                    {"task": "translation", "output": "public translation"}
                ],
            },
            public["articles"][0],
        )
        self.assertEqual(1, public["cached_ai_artifact_count"])
        self.assertNotIn("ai_reports", public)
        self.assertNotIn("cached_ai_report_count", public)
        self.assertIn("unread", original["counts"])

    def test_release_rejects_removed_ai_brief_fields(self):
        base_snapshot = {
            "articles": [],
            "sources": [],
            "render_llm_tokens_used": 0,
            "llm_tokens_used": 0,
        }
        base_cache = {
            "protocol": "aaron-reader-public-ai-cache-v3",
            "artifacts": [],
        }
        cases = (
            ({**base_snapshot, "ai_reports": []}, base_cache),
            ({**base_snapshot, "cached_ai_report_count": 0}, base_cache),
            (base_snapshot, {**base_cache, "reports": []}),
        )
        for snapshot, cache in cases:
            with self.subTest(snapshot=snapshot, cache=cache), mock.patch.object(
                RELEASE,
                "_validate_regular_file",
                side_effect=[
                    json.dumps(snapshot).encode("utf-8"),
                    b"<rss version='2.0'><channel /></rss>",
                    b"# digest\n",
                    json.dumps(cache).encode("utf-8"),
                ],
            ):
                with self.assertRaisesRegex(RELEASE.ReleaseError, "removed AI brief"):
                    RELEASE._validate_outputs()

    def test_public_digest_uses_all_public_articles_and_escapes_markdown(self):
        digest = RELEASE._public_digest(
            {
                "articles": [
                    {
                        "title": "[Breaking] *item*\nnext",
                        "source_name": "Source_One",
                        "published_at": "2026-08-02T12:00:00Z",
                        "url": "https://example.com/read?id=1",
                        "unread": True,
                    },
                    {
                        "title": "Unsafe URL",
                        "source_name": "Source Two",
                        "published_at": "2026-08-01T12:00:00Z",
                        "url": "javascript:alert(1)",
                    },
                ]
            }
        )

        self.assertTrue(digest.startswith("# Aaron Reader Public Digest\n"))
        self.assertIn("latest 2 of 2 public articles", digest)
        self.assertNotIn("unread articles", digest.lower())
        self.assertIn(r"\[Breaking\] \*item\*next", digest)
        self.assertNotIn("javascript:", digest)

    def test_only_explicit_github_remotes_are_accepted(self):
        self.assertEqual(
            "aaron-he-zhu/aaron-reader",
            RELEASE._github_repo_slug(
                "https://github.com/aaron-he-zhu/aaron-reader.git"
            ),
        )
        self.assertEqual(
            "aaron-he-zhu/aaron-reader",
            RELEASE._github_repo_slug("git@github.com:aaron-he-zhu/aaron-reader.git"),
        )
        with self.assertRaises(RELEASE.ReleaseError):
            RELEASE._github_repo_slug("https://example.com/repo.git")

    def test_skip_sync_renders_existing_state_without_running_sync(self):
        prepared = self._prepare_with_mocks(skip_sync=True)
        reader = str(RELEASE.ROOT / "aaron-reader")

        self.assertIn([reader, "render"], prepared["commands"])
        self.assertIn([reader, "status", "--strict"], prepared["commands"])
        self.assertNotIn([reader, "sync"], prepared["commands"])
        self.assertTrue(prepared["result"]["sync_skipped"])
        self.assertEqual(2, prepared["clean"].call_count)
        prepared["copy_snapshot"].assert_called_once()
        prepared["verify_build"].assert_called_once_with()
        prepared["commit_snapshot"].assert_called_once()
        self.assertEqual(2, prepared["publication_hashes"].call_count)

    def test_skip_sync_can_render_only_from_a_fresh_validation_database(self):
        validation = RELEASE.ROOT / "data" / "cloud-validation.sqlite3"
        prepared = self._prepare_with_mocks(
            skip_sync=True,
            database_path=validation,
        )
        reader = str(RELEASE.ROOT / "aaron-reader")

        self.assertIn(
            [reader, "--database", str(validation), "render"],
            prepared["commands"],
        )
        self.assertIn(
            [reader, "--database", str(validation), "status", "--strict"],
            prepared["commands"],
        )

    def test_database_override_is_rejected_for_a_network_sync(self):
        with self.assertRaisesRegex(RELEASE.ReleaseError, "requires --skip-sync"):
            self._prepare_with_mocks(
                skip_sync=False,
                database_path=RELEASE.ROOT / "data" / "unexpected.sqlite3",
            )

    def test_default_release_still_runs_deterministic_sync(self):
        prepared = self._prepare_with_mocks(skip_sync=False)
        reader = str(RELEASE.ROOT / "aaron-reader")

        self.assertIn([reader, "sync"], prepared["commands"])
        self.assertIn([reader, "status", "--strict"], prepared["commands"])
        self.assertNotIn([reader, "render"], prepared["commands"])
        self.assertFalse(prepared["result"]["sync_skipped"])

    def test_release_refuses_a_publication_file_changed_by_build_code(self):
        snapshot = {
            "generated_at": "2026-08-02T12:00:00Z",
            "articles": [],
            "sources": [],
            "render_llm_tokens_used": 0,
            "llm_tokens_used": 0,
        }
        with (
            mock.patch.object(RELEASE, "_ensure_clean_source"),
            mock.patch.object(RELEASE, "_run"),
            mock.patch.object(RELEASE, "_validate_outputs", return_value=snapshot),
            mock.patch.object(RELEASE, "_copy_snapshot"),
            mock.patch.object(RELEASE, "_verify_build"),
            mock.patch.object(
                RELEASE,
                "_publication_hashes",
                side_effect=[{"crawler/latest.json": "before"}, {"crawler/latest.json": "after"}],
            ),
            mock.patch.object(RELEASE, "_commit_snapshot") as commit_snapshot,
        ):
            with self.assertRaisesRegex(RELEASE.ReleaseError, "build or test changed"):
                RELEASE.prepare(skip_sync=True)
        commit_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
