"""업비트 API 문서 수집기."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

SOURCES = {
    "upbit": {
        "name": "업비트 개발자 센터",
        "llms_url": "https://docs.upbit.com/kr/llms.txt",
        "type": "seed",
    },
}

TIMEOUT = 30
CONCURRENCY = 2
MAX_RETRIES = 3
MAX_RETRY_DELAY = 10.0
USER_AGENT = "upbit-mcp/2.2 (+https://github.com/chabinhwang/upbit-mcp)"
ALLOWED_CONTENT_TYPES = {"text/markdown", "text/plain"}
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

SECTION_PATTERN = re.compile(r"^##\s+(?P<section>.+?)\s*$")

# ReadMe가 같은 기술지원 페이지를 과거 한글 slug로 한 번 더 노출한다.
PAGE_ALIASES = {
    "/kr/page/업비트-open-api-기술-지원-안내.md": (
        "/kr/page/upbit_open_api_support.md"
    ),
}


@dataclass(slots=True)
class FetchResult:
    """단일 HTTP 수집 결과."""

    requested_url: str
    final_url: str
    text: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.text is not None and self.error is None


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT})


@asynccontextmanager
async def _client_scope(client: httpx.AsyncClient | None):
    if client is not None:
        yield client
        return

    async with _new_client() as owned_client:
        yield owned_client


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), MAX_RETRY_DELAY)
            except ValueError:
                pass

    ceiling = min(0.5 * (2**attempt), MAX_RETRY_DELAY)
    return random.uniform(0.0, ceiling)


async def _get_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response | None, str | None]:
    last_error: str | None = None

    for attempt in range(MAX_RETRIES + 1):
        response: httpx.Response | None = None
        try:
            response = await client.get(
                url,
                headers=headers,
                timeout=TIMEOUT,
                follow_redirects=True,
            )
            if (
                response.status_code not in RETRYABLE_STATUS_CODES
                or attempt == MAX_RETRIES
            ):
                return response, None
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
            if attempt == MAX_RETRIES:
                return None, last_error

        delay = _retry_delay(response, attempt)
        logger.warning(
            "fetch retry %d/%d in %.2fs: %s (%s)",
            attempt + 1,
            MAX_RETRIES,
            delay,
            url,
            last_error,
        )
        await asyncio.sleep(delay)

    return None, last_error


async def fetch_url(client: httpx.AsyncClient, url: str) -> FetchResult:
    """URL을 수집하고 Markdown/text 응답만 허용한다."""
    response, request_error = await _get_with_retries(client, url)
    if response is None:
        logger.warning("fetch failed: %s -> %s", url, request_error)
        return FetchResult(
            requested_url=url,
            final_url=url,
            error=request_error or "request failed",
        )

    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    result = FetchResult(
        requested_url=url,
        final_url=str(response.url),
        status_code=response.status_code,
        content_type=content_type or None,
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
    )

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        result.error = f"HTTP {response.status_code}: {exc}"
        logger.warning("fetch failed: %s -> HTTP %s", url, response.status_code)
        return result

    if content_type not in ALLOWED_CONTENT_TYPES:
        result.error = f"unexpected content type: {content_type or '(missing)'}"
        logger.warning("fetch rejected: %s -> %s", url, result.error)
        return result

    response.encoding = "utf-8"
    if not response.text.strip():
        result.error = "empty response"
        logger.warning("fetch rejected: %s -> %s", url, result.error)
        return result

    result.text = response.text
    return result


async def fetch_text(client: httpx.AsyncClient, url: str) -> str | None:
    """기존 호출부를 위한 텍스트 전용 래퍼."""
    return (await fetch_url(client, url)).text


def _parse_link_line(line: str) -> tuple[str, str] | None:
    """중첩/이스케이프 대괄호를 고려해 첫 Markdown 링크를 파싱한다."""
    value = line.lstrip()
    if not value.startswith("- ["):
        return None

    markdown = value[2:]
    depth = 0
    escaped = False
    for index, character in enumerate(markdown):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "[":
            depth += 1
            continue
        if character != "]":
            continue

        depth -= 1
        if depth != 0 or markdown[index + 1 : index + 2] != "(":
            continue

        url_end = markdown.find(")", index + 2)
        if url_end == -1:
            return None
        title = markdown[1:index]
        url = markdown[index + 2 : url_end]
        if not url.startswith(("https://", "http://")):
            return None
        return title, url

    return None


def parse_links(llms_txt: str) -> list[dict[str, str]]:
    """llms.txt의 모든 Markdown 링크를 섹션 정보와 함께 파싱한다."""
    section = ""
    results: list[dict[str, str]] = []

    for line in llms_txt.splitlines():
        section_match = SECTION_PATTERN.match(line)
        if section_match:
            section = section_match.group("section")
            continue

        link = _parse_link_line(line)
        if link is None:
            continue
        title, url = link

        results.append(
            {
                "title": title,
                "url": url,
                "section": section,
            }
        )

    return results


def _canonicalize_link(link: dict[str, str]) -> dict[str, str] | None:
    parsed = urlsplit(link["url"])
    if parsed.scheme != "https" or parsed.hostname != "docs.upbit.com":
        logger.warning("off-origin link ignored: %s", link["url"])
        return None
    if not parsed.path.startswith("/kr/"):
        logger.warning("out-of-scope link ignored: %s", link["url"])
        return None

    path = parsed.path
    if link.get("section") == "Pages" and path.count("/") == 2:
        path = path.replace("/kr/", "/kr/page/", 1)
    path = PAGE_ALIASES.get(path, path)

    return {
        **link,
        "url": urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, "")),
        "original_url": link["url"],
    }


def canonicalize_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    """동일 원본 범위만 허용하고 Pages 경로/중복을 정규화한다."""
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    for link in links:
        canonical = _canonicalize_link(link)
        if canonical is None or canonical["url"] in seen:
            continue
        seen.add(canonical["url"])
        results.append(canonical)

    return results


async def fetch_seed_pages(
    client: httpx.AsyncClient,
    links: list[dict[str, str]],
    *,
    source_key: str = "upbit",
) -> tuple[list[dict], list[dict[str, str | int | None]]]:
    """하위 페이지를 제한된 동시성으로 수집한다."""
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _fetch_one(
        link: dict[str, str],
    ) -> tuple[dict | None, dict[str, str | int | None] | None]:
        async with sem:
            result = await fetch_url(client, link["url"])
        if not result.ok:
            return None, {
                "url": link["url"],
                "status_code": result.status_code,
                "error": result.error or "unknown error",
            }
        return (
            {
                "source": source_key,
                "url": link["url"],
                "retrieved_url": result.final_url,
                "title": link["title"],
                "section": link.get("section", ""),
                "content": result.text,
                "etag": result.etag,
                "last_modified": result.last_modified,
            },
            None,
        )

    outcomes = await asyncio.gather(*[_fetch_one(link) for link in links])
    documents = [document for document, _ in outcomes if document is not None]
    failures = [failure for _, failure in outcomes if failure is not None]
    return documents, failures


async def collect_all(client: httpx.AsyncClient | None = None) -> dict:
    """모든 소스를 수집하고 문서별 성공/실패 메타데이터를 반환한다."""
    result = {}
    async with _client_scope(client) as active_client:
        for key, source in SOURCES.items():
            raw_result = await fetch_url(active_client, source["llms_url"])
            if not raw_result.ok:
                logger.error("source collection failed: %s (%s)", key, raw_result.error)
                continue

            raw_links = parse_links(raw_result.text or "")
            links = canonicalize_links(raw_links)
            logger.info(
                "upbit links: manifest=%d canonical=%d",
                len(raw_links),
                len(links),
            )
            documents, failures = await fetch_seed_pages(
                active_client,
                links,
                source_key=key,
            )
            result[key] = {
                "raw_text": raw_result.text,
                "etag": raw_result.etag,
                "last_modified": raw_result.last_modified,
                "manifest_count": len(raw_links),
                "canonical_count": len(links),
                "manifest_urls": [link["url"] for link in links],
                "documents": documents,
                "failed_urls": failures,
            }

    logger.info(
        "collection complete: %s",
        {
            key: {
                "documents": len(value["documents"]),
                "failed": len(value["failed_urls"]),
            }
            for key, value in result.items()
        },
    )
    return result


async def fetch_single_source_raw(url: str) -> str | None:
    """단일 URL의 원본 텍스트를 반환한다."""
    async with _new_client() as client:
        return await fetch_text(client, url)


async def check_source_etags(
    stored_etags: dict[str, str],
    client: httpx.AsyncClient | None = None,
) -> tuple[dict[str, str], bool]:
    """소스 ETag를 비교한다. 확인 실패 시 기존 캐시를 유효하게 유지한다."""
    new_etags: dict[str, str] = {}
    changed = False

    async with _client_scope(client) as active_client:
        for key, source in SOURCES.items():
            url = source["llms_url"]
            stored_etag = stored_etags.get(key)
            headers = {"If-None-Match": stored_etag} if stored_etag else None

            response, request_error = await _get_with_retries(
                active_client,
                url,
                headers=headers,
            )
            if response is None:
                logger.warning(
                    "ETag check failed, keeping cache: %s -> %s",
                    key,
                    request_error,
                )
                if stored_etag:
                    new_etags[key] = stored_etag
                continue

            if response.status_code == 304:
                logger.info("ETag 304 (unchanged): %s", key)
                if stored_etag:
                    new_etags[key] = stored_etag
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning("ETag check failed, keeping cache: %s -> %s", key, exc)
                if stored_etag:
                    new_etags[key] = stored_etag
                continue

            etag = response.headers.get("etag")
            if etag:
                new_etags[key] = etag
                if stored_etag == etag:
                    logger.info("ETag unchanged: %s", key)
                else:
                    logger.info("ETag changed: %s [%s -> %s]", key, stored_etag, etag)
                    changed = True
            else:
                logger.info("ETag missing, refresh required: %s", key)
                changed = True

    return new_etags, changed


async def collect_etags() -> dict[str, str]:
    """모든 소스의 현재 ETag를 수집한다."""
    etags: dict[str, str] = {}
    async with _new_client() as client:
        for key, source in SOURCES.items():
            result = await fetch_url(client, source["llms_url"])
            if result.ok and result.etag:
                etags[key] = result.etag
    return etags
