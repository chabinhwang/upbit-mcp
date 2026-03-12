# upbit-mcp

> AI 코딩 에이전트에게 업비트 개발자 문서를 제공하는 MCP 서버

[업비트 개발자 센터](https://docs.upbit.com) API 문서를 자동으로 수집하고, AI가 검색할 수 있도록 제공하는 [Model Context Protocol (MCP)](https://modelcontextprotocol.io) 서버입니다.

## 주요 기능

- 업비트 개발자 센터 API 문서 자동 수집 (91개 문서)
- 마크다운 헤더 기반 지능형 청킹 (H1 → H2 → H3 재귀 분할)
- 2단계 키워드 검색 (정확 매칭 우선, 부분 매칭 폴백)
- SHA256 해시 기반 캐시로 빠른 재시작
- 비동기 병렬 수집 (동시 8개 요청)

## 빠른 시작

### 필수 조건

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (uvx 사용 시)

### Claude Code

`~/.claude/settings.json`의 `mcpServers`에 추가:

```json
{
  "upbit-docs": {
    "command": "uvx",
    "args": ["--from", "git+https://github.com/chabinhwang/upbit-mcp@main", "upbit-mcp"]
  }
}
```

### Codex

`~/.codex/config.toml`의 `mcp_servers`에 추가:

```toml
[mcp_servers.upbit-docs]
command = "uvx"
args = ["--from", "git+https://github.com/chabinhwang/upbit-mcp@main", "upbit-mcp"]
```

### Gemini CLI

`~/.gemini/settings.json`의 `mcpServers`에 추가:


```json
{
  "mcpServers": {
    "upbit-docs": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/chabinhwang/upbit-mcp@main", "upbit-mcp"]
    }
  }
}
```

### Claude Desktop

`claude_desktop_config.json`에 추가:

<details>
<summary>macOS: ~/Library/Application Support/Claude/claude_desktop_config.json</summary>

```json
{
  "mcpServers": {
    "upbit-docs": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/chabinhwang/upbit-mcp@main", "upbit-mcp"]
    }
  }
}
```
</details>

<details>
<summary>Windows: %APPDATA%\Claude\claude_desktop_config.json</summary>

```json
{
  "mcpServers": {
    "upbit-docs": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/chabinhwang/upbit-mcp@main", "upbit-mcp"]
    }
  }
}
```
</details>

### 로컬 설치 (개발용)

```bash
git clone https://github.com/chabinhwang/upbit-mcp.git
cd upbit-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

```json
{
  "upbit-docs": {
    "command": "/absolute/path/to/upbit-mcp/.venv/bin/upbit-mcp"
  }
}
```

## 제공 도구

### `search_docs`

업비트 개발자 문서를 키워드로 검색합니다.

```
검색어: "주문 API 매수"
```

| 파라미터 | 타입 | 필수 | 설명 |
|----------|------|------|------|
| `query` | string | O | 검색어 (공백으로 구분된 키워드) |
| `source` | string | X | 소스 필터. `"upbit"` |

### `sync_sources`

문서를 수동으로 동기화합니다. 최신 문서가 필요할 때 사용합니다.

| 파라미터 | 타입 | 필수 | 설명 |
|----------|------|------|------|
| `force` | boolean | X | `true`이면 캐시 무시 후 강제 재수집 |

## 동작 방식

```
llms.txt 다운로드
       ↓
  마크다운 링크 파싱 (91개 문서 URL 추출)
       ↓
  하위 페이지 병렬 수집 (동시 8개)
       ↓
  마크다운 헤더 기반 청킹 (최대 3,000자)
       ↓
  로컬 캐시 저장 (~/.upbit-mcp-cache/)
       ↓
  키워드 검색 제공
```

- **캐시**: SHA256 해시로 원본 변경을 감지하여, 변경이 없으면 캐시에서 즉시 로드합니다.
- **재동기화**: `sync_sources(force=True)` 호출 또는 캐시 디렉토리 삭제 후 재시작하면 됩니다.

## 성능 벤치마크

2026-03-13 기준으로, 현재 로컬에서 실행 중인 MCP와 분리하기 위해 매 실행마다 임시 `HOME`을 사용해 캐시를 격리한 뒤 실측했습니다.

- 실행 경로: 실제 서버 시작 시점과 동일하게 `lifespan` 기준 `_init_chunks()`까지 측정
- 반복 횟수: 각 시나리오 10회
- 시나리오 A: `full_recollect_boot`
  캐시가 없는 상태로 부팅해서 `llms.txt`와 하위 문서를 전부 다시 수집
- 시나리오 B: `etag_compare_boot`
  같은 임시 캐시에서 한 번 받아둔 뒤 다시 부팅해서 ETag만 비교하고, 변경이 없으면 재다운로드 없이 캐시 사용
- 참고: `etag_compare_boot`는 10/10회 모두 실제로 "변경 없음, 캐시 사용" 경로를 탔습니다.

| 시나리오 | 평균 | 중앙값 | 최소 | 최대 |
|---|---:|---:|---:|---:|
| `full_recollect_boot` | 8.297s | 5.873s | 5.573s | 24.104s |
| `etag_compare_boot` | 0.380s | 0.361s | 0.319s | 0.509s |

- 평균 기준 차이: `7.917s`
- 중앙값 기준 차이: `5.512s`
- 평균 기준으로 `etag_compare_boot`가 약 `21.8배` 빠름
- 해석: 업비트 MCP도 전체 재수집보다, 지금 코드처럼 ETag 비교 후 변경이 없으면 캐시를 사용하는 부팅 경로가 훨씬 빠릅니다.

## 프로젝트 구조

```
upbit-mcp/
├── pyproject.toml
├── README.md
├── LICENSE
└── upbit_mcp/
    ├── __init__.py
    ├── main.py          # MCP 서버 엔트리포인트
    ├── collector.py     # 문서 수집 (httpx 비동기)
    ├── chunker.py       # 마크다운 청킹
    ├── searcher.py      # 키워드 검색
    └── cache.py         # JSON 캐시 + 해시 관리
```

## 라이선스

MIT License
