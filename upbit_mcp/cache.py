"""원자적 JSON 캐시 + SHA256 해시 변경 감지."""

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".upbit-mcp-cache"
CHUNKS_FILE = CACHE_DIR / "chunks.json"
HASHES_FILE = CACHE_DIR / "hashes.json"
ETAGS_FILE = CACHE_DIR / "etags.json"
META_FILE = CACHE_DIR / "meta.json"

CACHE_SCHEMA_VERSION = 2
PARSER_VERSION = 2


def _ensure_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("cache read failed, ignoring %s: %s", path, exc)
        return default


def _write_json(path: Path, data: Any) -> None:
    """같은 디렉터리에 쓴 뒤 원자적으로 교체한다."""
    _ensure_dir()
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=CACHE_DIR,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            json.dump(data, temp_file, ensure_ascii=False, separators=(",", ":"))
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def compute_hash(text: str) -> str:
    """텍스트의 SHA256 해시를 계산한다."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_hashes() -> dict[str, str]:
    """저장된 해시를 로드한다."""
    _ensure_dir()
    data = _load_json(HASHES_FILE, {})
    return data if isinstance(data, dict) else {}


def save_hashes(hashes: dict[str, str]):
    """해시를 원자적으로 저장한다."""
    _write_json(HASHES_FILE, hashes)


def load_chunks() -> list[dict] | None:
    """캐시된 청크를 로드한다. 없으면 None."""
    _ensure_dir()
    if not CHUNKS_FILE.exists():
        return None
    data = _load_json(CHUNKS_FILE, None)
    if not isinstance(data, list):
        return None
    logger.info("캐시 로드: %d개 청크", len(data))
    return data


def save_chunks(chunks: list[dict]):
    """청크를 원자적으로 저장한다."""
    _write_json(CHUNKS_FILE, chunks)
    logger.info("캐시 저장: %d개 청크", len(chunks))


def needs_refresh(current_raw_texts: dict[str, str]) -> bool:
    """현재 원본 텍스트 해시와 저장된 해시를 비교하여 갱신 필요 여부를 반환한다."""
    saved = load_hashes()
    for key, text in current_raw_texts.items():
        current_hash = compute_hash(text)
        if saved.get(key) != current_hash:
            logger.info("해시 불일치: %s → 재수집 필요", key)
            return True
    # 캐시 파일이 없는 경우도 갱신 필요
    if not CHUNKS_FILE.exists():
        return True
    logger.info("해시 일치: 캐시 사용")
    return False


def update_hashes(raw_texts: dict[str, str]):
    """현재 원본 텍스트의 해시를 저장한다."""
    hashes = {key: compute_hash(text) for key, text in raw_texts.items()}
    save_hashes(hashes)


def load_etags() -> dict[str, str]:
    """저장된 ETag를 로드한다."""
    _ensure_dir()
    data = _load_json(ETAGS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_etags(etags: dict[str, str]):
    """ETag를 원자적으로 저장한다."""
    _write_json(ETAGS_FILE, etags)


def load_cache_meta() -> dict[str, Any]:
    """캐시 스키마/수집 상태를 로드한다."""
    _ensure_dir()
    data = _load_json(META_FILE, {})
    return data if isinstance(data, dict) else {}


def save_cache_meta(meta: dict[str, Any]) -> None:
    """캐시 스키마/수집 상태를 원자적으로 저장한다."""
    _write_json(META_FILE, meta)


def cache_is_current(meta: dict[str, Any]) -> bool:
    """현재 코드로 완전하게 생성된 캐시인지 확인한다."""
    return (
        meta.get("schema_version") == CACHE_SCHEMA_VERSION
        and meta.get("parser_version") == PARSER_VERSION
        and meta.get("complete") is True
    )
