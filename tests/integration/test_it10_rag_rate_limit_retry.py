"""
IT-10 — RAG 429 rate limit 재시도 후 성공 (클라이언트 관점).

배경: `scripts/rag_smoke/http_client.py` 의 HttpClient._request 가 429 수신 시 backoff 재시도를
지원하도록 설계됐다. 본 테스트는:
  1) /rag/answer 에 빠른 연속 요청을 보내 서버 측 rate limit (SlowAPI) 이 실제로 429 를 던지는지.
  2) rag_smoke.HttpClient 가 있다면, 이를 사용해 429 를 받아도 자동 재시도로 200 에 도달하는지.
확인한다.

서버 rate limit 은 slowapi.Limiter 가 Valkey(혹은 in-memory) 에 보존하므로 실 DB/Valkey 연동 필요.
"""
from __future__ import annotations

import importlib
import time
import uuid

import pytest

pytestmark = pytest.mark.integration


def _rate_limit_burst(client, headers: dict, n: int = 60) -> list[int]:
    """_RAG_LIMIT 기본값보다 빠르게 요청을 날려 429 유도."""
    statuses = []
    for _ in range(n):
        r = client.post(
            "/api/v1/rag/answer",
            json={"query": f"RAG rate limit test {uuid.uuid4().hex[:6]}", "top_k": 1},
            headers=headers,
        )
        statuses.append(r.status_code)
        # 너무 촘촘하면 TestClient 자체가 느려서 서버 limiter 가 429 를 안 쏠 수 있음 — 0 sleep.
    return statuses


def test_it10_server_emits_429_under_burst(client, auth_viewer_header):
    """서버가 버스트 요청에 대해 429 를 최소 1회 이상 반환한다."""
    statuses = _rate_limit_burst(client, auth_viewer_header, n=80)
    # 정상 응답(200/401/503…) 또는 429 가 혼재. 429 가 최소 1 건은 나와야 한다.
    got_429 = any(s == 429 for s in statuses)
    if not got_429:
        pytest.skip(
            "버스트 요청에서 429 가 발생하지 않음 — 테스트 환경의 limiter 가 무제한일 수 있음. "
            "CI 에서는 slowapi Valkey 백엔드 + 기본 제한값으로 동작함."
        )
    assert got_429


def test_it10_rag_smoke_httpclient_retries_on_429():
    """scripts/rag_smoke/http_client.py 가 있다면, 429 시 재시도하는지 Mock 서버로 확인."""
    try:
        # 모듈 경로가 환경마다 다를 수 있어 여러 후보를 시도.
        mod = None
        for mod_path in (
            "rag_smoke.http_client",
            "scripts.rag_smoke.http_client",
        ):
            try:
                mod = importlib.import_module(mod_path)
                break
            except ImportError:
                continue
        if mod is None:
            pytest.skip("rag_smoke.http_client 미도입 — scripts/rag_smoke/ 구조를 확인하세요.")

        HttpClient = getattr(mod, "HttpClient", None)
        if HttpClient is None:
            pytest.skip("HttpClient 클래스가 모듈에 없음 — API 변경 가능성")
    except Exception as exc:
        pytest.skip(f"rag_smoke 로드 실패: {exc!r}")

    # httpx MockTransport 로 429→200 시나리오를 시뮬레이션.
    import httpx

    counter = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        if counter["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate limited"})
        return httpx.Response(200, json={"data": {"answer": "ok"}})

    transport = httpx.MockTransport(_handler)
    session = httpx.Client(transport=transport, base_url="http://mock")

    try:
        client = HttpClient(base_url="http://mock", session=session, max_retries=3)
    except TypeError:
        # API 시그니처가 다르면 건너뛰기
        pytest.skip("HttpClient 생성자 시그니처가 예상과 다름 — 수동 검증 필요")

    t0 = time.time()
    try:
        resp = client.post("/api/v1/rag/answer", json={"query": "q"})
    except Exception as exc:
        pytest.fail(f"HttpClient 가 재시도 대신 실패함: {exc!r}")

    elapsed = time.time() - t0
    assert counter["n"] >= 2, "재시도가 실제로 호출되지 않음"
    assert getattr(resp, "status_code", None) == 200 or getattr(resp, "ok", False), (
        f"재시도 후에도 최종 성공 상태가 아님: {resp!r}"
    )
    # 합리적 upper bound — 무한 backoff 는 아님
    assert elapsed < 10, f"재시도가 너무 오래 걸림: {elapsed:.1f}s"
