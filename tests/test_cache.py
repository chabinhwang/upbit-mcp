from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from upbit_mcp import cache


class CacheTests(TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        cache_dir = Path(self.temp_dir.name)
        self.patcher = patch.multiple(
            cache,
            CACHE_DIR=cache_dir,
            CHUNKS_FILE=cache_dir / "chunks.json",
            HASHES_FILE=cache_dir / "hashes.json",
            ETAGS_FILE=cache_dir / "etags.json",
            META_FILE=cache_dir / "meta.json",
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_chunks_and_metadata_round_trip(self):
        chunks = [{"source": "upbit", "url": "https://example", "content": "ok"}]
        meta = {
            "schema_version": cache.CACHE_SCHEMA_VERSION,
            "parser_version": cache.PARSER_VERSION,
            "complete": True,
        }

        cache.save_chunks(chunks)
        cache.save_cache_meta(meta)

        self.assertEqual(cache.load_chunks(), chunks)
        self.assertEqual(cache.load_cache_meta(), meta)
        self.assertTrue(cache.cache_is_current(meta))
        self.assertEqual(list(cache.CACHE_DIR.glob("*.tmp")), [])

    def test_corrupt_chunks_are_ignored(self):
        cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.CHUNKS_FILE.write_text("{broken", encoding="utf-8")

        self.assertIsNone(cache.load_chunks())
