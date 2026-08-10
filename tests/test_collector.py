from __future__ import annotations

from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import httpx

from upbit_mcp import collector

SAMPLE_LLMS = """# Upbit

## Guides
- [인증](https://docs.upbit.com/kr/reference/auth.md): 인증 안내

## Recipes
- [[Python] RSI 산출](https://docs.upbit.com/kr/recipes/python-rsi-산출.md)

## Pages
- [기술 지원](https://docs.upbit.com/kr/upbit_open_api_support.md)
- [기술 지원 구주소](https://docs.upbit.com/kr/업비트-open-api-기술-지원-안내.md)

## Changelog
- [[안내] 기능 출시](https://docs.upbit.com/kr/changelog/feature-release.md)
"""


class ParseLinksTests(TestCase):
    def test_nested_titles_and_page_urls_are_normalized(self):
        parsed = collector.parse_links(SAMPLE_LLMS)

        self.assertEqual(len(parsed), 5)
        self.assertEqual(parsed[1]["title"], "[Python] RSI 산출")
        self.assertEqual(parsed[4]["title"], "[안내] 기능 출시")

        canonical = collector.canonicalize_links(parsed)

        self.assertEqual(len(canonical), 4)
        self.assertEqual(
            canonical[2]["url"],
            "https://docs.upbit.com/kr/page/upbit_open_api_support.md",
        )

    def test_off_origin_links_are_ignored(self):
        parsed = collector.parse_links(
            "## Guides\n- [외부](https://example.com/docs/page.md)"
        )
        self.assertEqual(collector.canonicalize_links(parsed), [])


class FetchTests(IsolatedAsyncioTestCase):
    async def test_429_is_retried_and_markdown_is_returned(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, request=request)
            return httpx.Response(
                200,
                headers={"content-type": "text/markdown; charset=utf-8"},
                text="# ok",
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch("upbit_mcp.collector.asyncio.sleep", new=AsyncMock()) as sleep:
                result = await collector.fetch_url(
                    client, "https://docs.upbit.com/kr/docs/example.md"
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "# ok")
        self.assertEqual(calls, 2)
        sleep.assert_awaited_once()

    async def test_html_challenge_is_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html>challenge</html>",
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await collector.fetch_url(
                client, "https://docs.upbit.com/kr/docs/example.md"
            )

        self.assertFalse(result.ok)
        self.assertIn("unexpected content type", result.error or "")

    async def test_collect_all_handles_nested_links_and_page_aliases(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/kr/llms.txt":
                return httpx.Response(
                    200,
                    headers={
                        "content-type": "text/plain",
                        "etag": 'W/"seed"',
                    },
                    text=SAMPLE_LLMS,
                    request=request,
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/markdown"},
                text=f"# {path}",
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await collector.collect_all(client)

        source = result["upbit"]
        self.assertEqual(source["manifest_count"], 5)
        self.assertEqual(source["canonical_count"], 4)
        self.assertEqual(len(source["documents"]), 4)
        self.assertEqual(source["failed_urls"], [])
        self.assertEqual(source["etag"], 'W/"seed"')

    async def test_etag_failure_keeps_stored_value(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch("upbit_mcp.collector.asyncio.sleep", new=AsyncMock()):
                etags, changed = await collector.check_source_etags(
                    {"upbit": 'W/"old"'}, client
                )

        self.assertFalse(changed)
        self.assertEqual(etags, {"upbit": 'W/"old"'})
