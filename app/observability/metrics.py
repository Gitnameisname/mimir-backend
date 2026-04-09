"""
Prometheus 메트릭 수집 모듈 (Phase 13-3).

수집 메트릭:
  - http_requests_total        : 요청 수 (method, path, status)
  - http_request_duration_ms   : 응답 시간 히스토그램 (P50/P95/P99 추적)
  - http_errors_total          : 5xx 오류 수
  - active_connections         : 현재 처리 중인 요청 수 (게이지)
  - db_query_duration_ms       : DB 쿼리 시간 (추후 확장용)

노출 endpoint: GET /metrics (Prometheus scrape)
인증: settings.environment == "production" 시 Bearer 토큰 필요

SLO 기준 (Phase 13 계획서):
  - P95 응답 < 500ms (검색/문서 조회)
  - RAG P95 TTFT < 2000ms
  - 오류율 < 0.1% (5xx 기준)
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from threading import Lock
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

# --------------------------------------------------------------------------- #
# 메트릭 저장소 (in-process, Prometheus 텍스트 형식 직접 생성)
# 외부 의존성 없이 prometheus_client 없이도 동작하도록 경량 구현
# prometheus_client가 있으면 실제 클라이언트 사용 권장
# --------------------------------------------------------------------------- #

_lock = Lock()

# Counter: {(method, path_pattern, status_code): count}
_request_counts: dict[tuple[str, str, int], int] = defaultdict(int)
# Counter: {(method, path_pattern): count} — 5xx only
_error_counts: dict[tuple[str, str], int] = defaultdict(int)
# Histogram buckets (ms): {(method, path_pattern): [durations]}
_durations: dict[tuple[str, str], list[float]] = defaultdict(list)
# Gauge
_active_connections: int = 0

# 히스토그램 버킷 경계 (ms)
_BUCKETS = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]

# duration 히스토그램 최대 샘플 수 (메모리 릭 방지)
# 경로당 최대 10,000개 샘플을 유지하고 초과 시 가장 오래된 절반을 버린다.
_MAX_SAMPLES_PER_PATH = 10_000

# path를 패턴으로 정규화 (UUID/숫자 → {id})
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_INT_SEGMENT_RE = re.compile(r"(?<=/)\d+(?=/|$)")


def _normalize_path(path: str) -> str:
    path = _UUID_RE.sub("{id}", path)
    path = _INT_SEGMENT_RE.sub("{id}", path)
    return path


# --------------------------------------------------------------------------- #
# 미들웨어
# --------------------------------------------------------------------------- #

class PrometheusMiddleware(BaseHTTPMiddleware):
    """요청/응답 메트릭을 수집하는 미들웨어."""

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        global _active_connections

        method = request.method
        path = _normalize_path(request.url.path)

        with _lock:
            _active_connections += 1

        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            with _lock:
                _active_connections -= 1
                _request_counts[(method, path, status)] += 1
                bucket = _durations[(method, path)]
                bucket.append(duration_ms)
                # 메모리 릭 방지: 최대 샘플 수 초과 시 오래된 절반 제거
                if len(bucket) > _MAX_SAMPLES_PER_PATH:
                    del bucket[: _MAX_SAMPLES_PER_PATH // 2]
                if status >= 500:
                    _error_counts[(method, path)] += 1

        return response


# --------------------------------------------------------------------------- #
# 메트릭 텍스트 생성
# --------------------------------------------------------------------------- #

def _histogram_text(name: str, help_text: str, durations_map: dict[tuple[str, str], list[float]]) -> list[str]:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
    for (method, path), durations in sorted(durations_map.items()):
        base_labels = f'method="{method}",path="{path}"'
        total = len(durations)
        count = total
        bucket_counts: dict[float, int] = {}
        for b in _BUCKETS:
            bucket_counts[b] = sum(1 for d in durations if d <= b)
        bucket_counts[float("inf")] = count

        for b in _BUCKETS:
            lines.append(f'{name}_bucket{{{base_labels},le="{b}"}} {bucket_counts[b]}')
        lines.append(f'{name}_bucket{{{base_labels},le="+Inf"}} {count}')
        lines.append(f'{name}_sum{{{base_labels}}} {sum(durations):.2f}')
        lines.append(f'{name}_count{{{base_labels}}} {count}')
    return lines


def generate_metrics_text() -> str:
    """Prometheus text 형식 메트릭 문자열을 생성한다."""
    lines: list[str] = []

    with _lock:
        request_counts_snapshot = dict(_request_counts)
        error_counts_snapshot = dict(_error_counts)
        durations_snapshot = {k: list(v) for k, v in _durations.items()}
        active = _active_connections

    # http_requests_total
    lines += [
        "# HELP http_requests_total Total number of HTTP requests",
        "# TYPE http_requests_total counter",
    ]
    for (method, path, status), count in sorted(request_counts_snapshot.items()):
        lines.append(
            f'http_requests_total{{method="{method}",path="{path}",status="{status}"}} {count}'
        )

    # http_errors_total
    lines += [
        "# HELP http_errors_total Total number of HTTP 5xx errors",
        "# TYPE http_errors_total counter",
    ]
    for (method, path), count in sorted(error_counts_snapshot.items()):
        lines.append(
            f'http_errors_total{{method="{method}",path="{path}"}} {count}'
        )

    # active_connections
    lines += [
        "# HELP active_connections Number of currently active HTTP connections",
        "# TYPE active_connections gauge",
        f"active_connections {active}",
    ]

    # http_request_duration_ms histogram
    lines += _histogram_text(
        "http_request_duration_ms",
        "HTTP request duration in milliseconds",
        durations_snapshot,
    )

    return "\n".join(lines) + "\n"
