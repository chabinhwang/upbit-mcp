from __future__ import annotations

from unittest import TestCase

from upbit_mcp import main


class SnapshotTests(TestCase):
    def test_failed_page_keeps_last_known_good_chunks(self):
        failed_url = "https://docs.upbit.com/kr/docs/optional.md"
        manifest_urls = sorted(main.REQUIRED_URLS | {failed_url})
        documents = [
            {
                "source": "upbit",
                "url": url,
                "title": url.rsplit("/", 1)[-1],
                "content": f"# {url}",
            }
            for url in main.REQUIRED_URLS
        ]
        collected = {
            "upbit": {
                "manifest_count": len(manifest_urls),
                "canonical_count": len(manifest_urls),
                "manifest_urls": manifest_urls,
                "documents": documents,
                "failed_urls": [
                    {"url": failed_url, "status_code": 429, "error": "HTTP 429"}
                ],
            }
        }
        cached = [
            {
                "source": "upbit",
                "url": failed_url,
                "title": "optional",
                "header": "optional",
                "content": "old content",
            }
        ]

        candidate, status, valid = main._build_snapshot(collected, cached)

        self.assertTrue(valid)
        self.assertFalse(status["complete"])
        self.assertEqual(status["coverage"], 1.0)
        self.assertTrue(
            any(
                chunk["url"] == failed_url and chunk["content"] == "old content"
                for chunk in candidate
            )
        )

    def test_low_coverage_snapshot_is_rejected(self):
        manifest_urls = sorted(
            main.REQUIRED_URLS
            | {
                "https://docs.upbit.com/kr/docs/a.md",
                "https://docs.upbit.com/kr/docs/b.md",
            }
        )
        collected = {
            "upbit": {
                "manifest_count": len(manifest_urls),
                "canonical_count": len(manifest_urls),
                "manifest_urls": manifest_urls,
                "documents": [],
                "failed_urls": [
                    {"url": url, "status_code": 503, "error": "HTTP 503"}
                    for url in manifest_urls
                ],
            }
        }

        _, status, valid = main._build_snapshot(collected, [])

        self.assertFalse(valid)
        self.assertEqual(status["coverage"], 0.0)
