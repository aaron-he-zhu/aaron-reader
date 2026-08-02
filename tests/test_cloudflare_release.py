import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "prepare_cloudflare_release", ROOT / "scripts" / "prepare_cloudflare_release.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load the Cloudflare release preparer")
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)


class CloudflareReleaseTests(unittest.TestCase):
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

    def test_public_projection_removes_private_reader_state(self):
        original = {
            "counts": {"total": 1, "unread": 1, "starred": 1},
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
                    "ai_artifacts": [{"task": "summary", "output": "public"}],
                }
            ],
        }

        public = RELEASE._public_snapshot(original)

        self.assertEqual({"total": 1}, public["counts"])
        self.assertEqual({"slug": "example"}, public["sources"][0])
        self.assertEqual(
            {"id": 7, "ai_artifacts": [{"task": "summary", "output": "public"}]},
            public["articles"][0],
        )
        self.assertIn("unread", original["counts"])

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


if __name__ == "__main__":
    unittest.main()
