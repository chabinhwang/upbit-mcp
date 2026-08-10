"""업비트 API 문서 검색 MCP 서버."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from mcp.server.mcpserver import MCPServer

from .cache import (
    CACHE_SCHEMA_VERSION,
    PARSER_VERSION,
    cache_is_current,
    load_cache_meta,
    load_chunks,
    load_etags,
    save_cache_meta,
    save_chunks,
    save_etags,
    update_hashes,
)
from .chunker import chunk_all
from .collector import check_source_etags, collect_all
from .searcher import search

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

MIN_COVERAGE_RATIO = 0.9
REQUIRED_URLS = {
    "https://docs.upbit.com/kr/reference/auth.md",
    "https://docs.upbit.com/kr/reference/rate-limits.md",
    "https://docs.upbit.com/kr/reference/rest-api-guide.md",
    "https://docs.upbit.com/kr/reference/websocket-guide.md",
    "https://docs.upbit.com/kr/reference/new-order.md",
}

_chunks: list[dict] = []
_last_sync_status: dict = {}
_sync_lock = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _status_from_meta(meta: dict, *, state: str = "cached") -> dict:
    return {
        "state": state,
        "using_cached": True,
        "manifest_count": meta.get("manifest_count"),
        "canonical_count": meta.get("canonical_count"),
        "fetched_count": meta.get("fetched_count"),
        "failed_count": meta.get("failed_count", 0),
        "coverage": meta.get("coverage"),
        "complete": meta.get("complete", False),
        "last_attempt_at": meta.get("last_attempt_at"),
        "last_success_at": meta.get("last_success_at"),
    }


def _merge_failed_documents(
    new_chunks: list[dict],
    cached_chunks: list[dict],
    failed_urls: set[str],
) -> list[dict]:
    """갱신 실패 URL에 대해서만 직전 정상 청크를 유지한다."""
    retained = [chunk for chunk in cached_chunks if chunk.get("url") in failed_urls]
    return new_chunks + retained


def _build_snapshot(
    collected: dict,
    cached_chunks: list[dict],
) -> tuple[list[dict], dict, bool]:
    source = collected.get("upbit")
    if not source:
        return (
            [],
            {
                "state": "unavailable",
                "using_cached": bool(cached_chunks),
                "error": "llms.txt source is unavailable",
            },
            False,
        )

    new_chunks = chunk_all(collected)
    failed_rows = source.get("failed_urls", [])
    failed_urls = {row["url"] for row in failed_rows}
    candidate = _merge_failed_documents(new_chunks, cached_chunks, failed_urls)

    manifest_urls = set(source.get("manifest_urls", []))
    covered_urls = {chunk.get("url") for chunk in candidate if chunk.get("url")}
    covered_manifest_urls = manifest_urls & covered_urls
    denominator = max(len(manifest_urls), 1)
    coverage = len(covered_manifest_urls) / denominator
    required_present = REQUIRED_URLS <= covered_urls
    valid = bool(candidate) and coverage >= MIN_COVERAGE_RATIO and required_present
    complete = valid and not failed_rows and covered_manifest_urls == manifest_urls

    status = {
        "state": "ok" if complete else "partial",
        "using_cached": bool(failed_urls & {c.get("url") for c in cached_chunks}),
        "manifest_count": source.get("manifest_count", 0),
        "canonical_count": source.get("canonical_count", len(manifest_urls)),
        "fetched_count": len(source.get("documents", [])),
        "failed_count": len(failed_rows),
        "failed_urls": failed_rows,
        "coverage": coverage,
        "complete": complete,
    }
    if not required_present:
        status["error"] = "required API reference documents are missing"
    elif coverage < MIN_COVERAGE_RATIO:
        status["error"] = f"coverage {coverage:.1%} is below {MIN_COVERAGE_RATIO:.0%}"

    return candidate, status, valid


def _cache_meta(status: dict, previous_meta: dict) -> dict:
    attempted_at = _now_iso()
    complete = status.get("complete") is True
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "complete": complete,
        "manifest_count": status.get("manifest_count"),
        "canonical_count": status.get("canonical_count"),
        "fetched_count": status.get("fetched_count"),
        "failed_count": status.get("failed_count", 0),
        "failed_urls": status.get("failed_urls", []),
        "coverage": status.get("coverage"),
        "last_attempt_at": attempted_at,
        "last_success_at": (
            attempted_at if complete else previous_meta.get("last_success_at")
        ),
    }


async def _refresh_chunks(cached_chunks: list[dict]) -> dict:
    """원격 문서를 수집하고 검증된 스냅샷만 캐시에 반영한다."""
    global _chunks, _last_sync_status

    previous_meta = load_cache_meta()
    collected = await collect_all()
    candidate, status, valid = _build_snapshot(collected, cached_chunks)

    if not valid:
        if cached_chunks:
            _chunks = cached_chunks
            status["state"] = "stale"
            status["using_cached"] = True
            logger.warning(
                "refresh rejected, keeping last-known-good cache: %s", status
            )
        else:
            _chunks = candidate
            logger.warning(
                "cold refresh incomplete; using in-memory partial result: %s", status
            )

        save_cache_meta(_cache_meta(status, previous_meta))
        _last_sync_status = status
        return status

    _chunks = candidate
    save_chunks(_chunks)

    raw_texts = {
        key: value["raw_text"]
        for key, value in collected.items()
        if value.get("raw_text")
    }
    if raw_texts:
        update_hashes(raw_texts)

    # 부분 수집이면 다음 시작 시 재시도할 수 있도록 새 ETag를 확정하지 않는다.
    if status["complete"]:
        etags = {
            key: value["etag"] for key, value in collected.items() if value.get("etag")
        }
        if etags:
            save_etags(etags)

    save_cache_meta(_cache_meta(status, previous_meta))
    _last_sync_status = status
    logger.info("refresh applied: %s", status)
    return status


async def _init_chunks(*, force: bool = False) -> dict:
    """캐시 또는 수집으로 청크를 초기화한다."""
    global _chunks, _last_sync_status

    async with _sync_lock:
        cached = load_chunks() or []
        meta = load_cache_meta()
        if cached:
            _chunks = cached

        if cached and not force and cache_is_current(meta):
            stored_etags = load_etags()
            if stored_etags:
                logger.info("checking source ETag")
                new_etags, needs_refresh = await check_source_etags(stored_etags)
                if not needs_refresh:
                    if new_etags:
                        save_etags(new_etags)
                    _last_sync_status = _status_from_meta(meta)
                    logger.info(
                        "source unchanged, using cache: %d chunks", len(_chunks)
                    )
                    return _last_sync_status

        if cached and not cache_is_current(meta):
            logger.info("cache parser/schema is stale or incomplete; refreshing")
        elif force:
            logger.info("forced refresh requested")
        else:
            logger.info("cache unavailable or source changed; refreshing")

        return await _refresh_chunks(cached)


@asynccontextmanager
async def lifespan(server: MCPServer):
    """서버 시작 시 문서를 로드한다."""
    await _init_chunks()
    yield


mcp = MCPServer(
    "upbit-docs",
    instructions="업비트 개발자 센터 API 문서 검색 도구",
    lifespan=lifespan,
)


@mcp.tool()
async def search_docs(query: str, source: str | None = None) -> str:
    """업비트 개발자 문서를 검색합니다.

    Args:
        query: 검색어 (공백으로 구분된 키워드)
        source: 소스 필터 (선택). "upbit"
    """
    if not _chunks:
        return "문서가 아직 로드되지 않았습니다. sync_sources를 호출해 주세요."

    results = search(_chunks, query, source=source)
    if not results:
        return f"'{query}'에 대한 검색 결과가 없습니다."

    output_parts = []
    for index, result in enumerate(results, 1):
        title = result.get("title") or result["header"] or "제목 없음"
        output_parts.append(
            f"### 결과 {index} [{result['source']}]\n"
            f"**문서**: {title}\n"
            f"**헤더**: {result['header']}\n"
            f"**URL**: {result['url']}\n"
            f"**매칭**: {result['match_count']}개 키워드 "
            f"({result['match_ratio']:.0%})\n\n"
            f"{result['content']}\n"
        )

    return "\n---\n".join(output_parts)


@mcp.tool()
async def sync_sources(force: bool = False) -> str:
    """문서를 수동으로 동기화합니다.

    Args:
        force: True이면 ETag를 무시하고 강제 재수집
    """
    status = await _init_chunks(force=force)
    return (
        f"동기화 상태: {status.get('state', 'unknown')}, "
        f"인덱스 {status.get('manifest_count', '?')}개, "
        f"정규화 {status.get('canonical_count', '?')}개, "
        f"수집 {status.get('fetched_count', '?')}개, "
        f"실패 {status.get('failed_count', 0)}개, "
        f"청크 {len(_chunks)}개"
    )


@mcp.tool()
async def docs_status() -> str:
    """현재 문서 캐시의 커버리지와 최신화 상태를 반환합니다."""
    status = _last_sync_status or _status_from_meta(load_cache_meta())
    return json.dumps(status, ensure_ascii=False, indent=2)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
