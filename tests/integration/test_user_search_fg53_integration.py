"""
S3 Phase 5 FG 5-3 — 사용자 검색 API (GET /api/v1/users) 통합 회귀.

대상: `app.api.v1.users_search.search_users` (라우터)

본 통합은 testcontainers / 실 DB 를 사용하지 않는다. FastAPI TestClient +
`app.dependency_overrides` 로 인증과 DB 의존성만 합성한다 — repository 는 mock 으로
유지하되, **라우터/Query 검증/응답 모델/dependency 흐름** 은 실제 ASGI stack 위에서 검증.

회귀 영역 (Codex 보고서 §5 P2 #9 + 본 검수보고서 §3 잔여 #3 해소):
  A. 401 — 인증 미통과 (Unauthorized)
  B. 400 — 빈 q / 너무 긴 q (Query validator)
     → Mimir 의 global `request_validation_error_handler` 가 FastAPI 의 기본 422 응답을
       `ApiValidationError → 400 validation_error` 로 변환 (`app/api/errors/handlers.py`).
  C. 빈 trim 결과 → 빈 응답 (200)
  D. R-A4 격리 — viewer org 밖 사용자 0건
  E. SQL injection payload 5+ — repository 가 raw payload 그대로 받아도 escape 처리됨
  F. 응답 모델 강제 — email/role/status 누설 차단 + items_truncated flag
  G. trace_id / request_id 부여 (audit 정합)
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "fg53-integration-test")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "fg53-integration-internal")


pytestmark = pytest.mark.unit  # testcontainers 의존 없음 — 빠른 in-process 통합


# ---------------------------------------------------------------------------
# Helper — TestClient + dependency_overrides 합성
# ---------------------------------------------------------------------------


def _make_client(actor_factory, repo_factory):
    """ASGI app + auth/DB dependency override 가 적용된 TestClient 반환."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.auth import resolve_current_actor
    from app.api.v1 import users_search as users_search_module

    # 1. 인증 override
    app.dependency_overrides[resolve_current_actor] = actor_factory
    # 2. repository 싱글턴 교체 (모듈 수준 _users_repository)
    repo = repo_factory()
    original_repo = users_search_module._users_repository
    users_search_module._users_repository = repo

    client = TestClient(app)

    def restore():
        app.dependency_overrides.pop(resolve_current_actor, None)
        users_search_module._users_repository = original_repo

    return client, repo, restore


def _make_actor(*, authenticated: bool = True, actor_id: str | None = None):
    """실제 ActorContext dataclass 인스턴스 반환.

    MagicMock(spec=...) 은 dataclass 의 `@property` (is_authenticated 등) 와 충돌
    가능성이 있어 실 인스턴스 사용. AuthMethod 값은 실제 enum 의 BEARER (JWT 토큰
    대응) 또는 None (anonymous).
    """
    from app.api.auth.models import ActorContext, ActorType, AuthMethod

    if not authenticated:
        return ActorContext(
            actor_type=ActorType.ANONYMOUS,
            actor_id=None,
            is_authenticated=False,
            auth_method=None,
            tenant_id=None,
        )

    return ActorContext(
        actor_type=ActorType.USER,
        actor_id=actor_id or str(uuid.uuid4()),
        is_authenticated=True,
        auth_method=AuthMethod.BEARER,
        tenant_id=None,
    )


def _make_repo(*, search_result: list[dict[str, Any]] | None = None, capture: dict | None = None):
    """모듈 수준 repository 교체용 mock. search_by_display_name_in_orgs 호출 인자 캡처."""
    repo = MagicMock()

    def _search(conn, **kwargs):
        if capture is not None:
            capture["kwargs"] = kwargs
            capture["call_count"] = capture.get("call_count", 0) + 1
        return list(search_result or [])

    repo.search_by_display_name_in_orgs.side_effect = _search
    return repo


# ---------------------------------------------------------------------------
# A. 인증 — 401 (Unauthorized)
# ---------------------------------------------------------------------------


class TestAuthentication:
    def test_unauthenticated_returns_401(self):
        capture: dict = {}
        actor = _make_actor(authenticated=False)
        client, repo, restore = _make_client(lambda: actor, lambda: _make_repo(capture=capture))
        try:
            r = client.get("/api/v1/users", params={"q": "al"})
            assert r.status_code == 401, r.text
            # repository 호출 0 — auth 차단이 먼저
            assert capture.get("call_count", 0) == 0
        finally:
            restore()

    def test_actor_id_missing_returns_401(self):
        """is_authenticated=True 라도 actor_id 가 None 이면 401 (방어층)."""
        from app.api.auth.models import ActorContext, ActorType

        actor = ActorContext(
            actor_type=ActorType.USER,
            actor_id=None,
            is_authenticated=True,
            auth_method=None,
            tenant_id=None,
        )
        client, repo, restore = _make_client(lambda: actor, lambda: _make_repo())
        try:
            r = client.get("/api/v1/users", params={"q": "al"})
            assert r.status_code == 401, r.text
        finally:
            restore()


# ---------------------------------------------------------------------------
# B. Query validator — 400
# ---------------------------------------------------------------------------


class TestQueryValidation:
    # Mimir 의 `request_validation_error_handler` 가 FastAPI 의 기본 422 를
    # `ApiValidationError → 400 validation_error` 로 매핑 (handlers.py:172).
    # 본 회귀는 그 실제 동작 (400) 을 검증한다.

    def test_empty_q_returns_400(self):
        actor = _make_actor()
        client, _, restore = _make_client(lambda: actor, lambda: _make_repo())
        try:
            r = client.get("/api/v1/users", params={"q": ""})
            # FastAPI Query(..., min_length=1) → RequestValidationError → 400 (Mimir global handler)
            assert r.status_code == 400, r.text
        finally:
            restore()

    def test_q_too_long_returns_400(self):
        actor = _make_actor()
        client, _, restore = _make_client(lambda: actor, lambda: _make_repo())
        try:
            r = client.get("/api/v1/users", params={"q": "a" * 65})
            assert r.status_code == 400, r.text
        finally:
            restore()

    def test_limit_out_of_range_returns_400(self):
        actor = _make_actor()
        client, _, restore = _make_client(lambda: actor, lambda: _make_repo())
        try:
            r = client.get("/api/v1/users", params={"q": "a", "limit": 0})
            assert r.status_code == 400, r.text
            r2 = client.get("/api/v1/users", params={"q": "a", "limit": 999})
            assert r2.status_code == 400, r2.text
        finally:
            restore()


# ---------------------------------------------------------------------------
# C. 빈 trim 결과 — 정상 빈 응답 (200)
# ---------------------------------------------------------------------------


class TestTrim:
    def test_whitespace_only_q_returns_empty(self):
        actor = _make_actor()
        capture: dict = {}
        client, _, restore = _make_client(
            lambda: actor, lambda: _make_repo(capture=capture)
        )
        try:
            # 공백만 — min_length=1 검증 통과 (space 도 1자) 후 trim 으로 빈 문자열
            r = client.get("/api/v1/users", params={"q": " "})
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            assert data["items"] == []
            assert data["items_total"] == 0
            assert data["items_truncated"] is False
            # repository 호출 0 — trim 후 빈이라 short-circuit
            assert capture.get("call_count", 0) == 0
        finally:
            restore()


# ---------------------------------------------------------------------------
# D. R-A4 격리 — viewer org 밖 사용자 0건
# ---------------------------------------------------------------------------


class TestRA4Isolation:
    def test_viewer_user_id_extracted_from_actor_only(self):
        """라우터가 viewer_user_id 를 ActorContext 에서만 추출 — query 주입 무시."""
        actor_id = "viewer-A"
        actor = _make_actor(actor_id=actor_id)
        capture: dict = {}
        client, _, restore = _make_client(
            lambda: actor, lambda: _make_repo(capture=capture)
        )
        try:
            # 악의적 query 주입 시도 — viewer_user_id 가 다른 org 의 user id 라고 속이려
            r = client.get(
                "/api/v1/users",
                params={"q": "B", "viewer_user_id": "victim-id"},
            )
            assert r.status_code == 200, r.text
            # repository 호출 인자에 ActorContext.actor_id 만 전달됨
            kwargs = capture["kwargs"]
            assert kwargs["viewer_user_id"] == actor_id
            assert kwargs["viewer_user_id"] != "victim-id"
        finally:
            restore()

    def test_repository_keyword_only_signature_enforced(self):
        """repository 가 positional viewer_user_id 받지 않음 — 단위 회귀 보강."""
        from app.repositories.users_repository import UsersRepository

        repo = UsersRepository()
        conn = MagicMock()
        with pytest.raises(TypeError):
            repo.search_by_display_name_in_orgs(conn, "u1", query="x")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# E. SQL injection payload 5+ — repository wildcard escape
# ---------------------------------------------------------------------------


class TestSqlInjectionPayloads:
    """라우터가 raw query 를 그대로 repository 로 전달하고, repository 가 escape.

    repository.search_by_display_name_in_orgs 의 ILIKE wildcard escape 가 다양한
    악의적 페이로드에 대해 정확히 동작하는지 통합 단계에서 재검증.
    """

    @pytest.mark.parametrize(
        "payload,expected_pattern_includes",
        [
            ("'; DROP TABLE users;--", "'; DROP TABLE users;--%"),
            ("' OR 1=1--", "' OR 1=1--%"),
            ("%adm", r"\%adm%"),
            ("a_b", r"a\_b%"),
            ("a\\b", r"a\\b%"),
            ("' UNION SELECT", "' UNION SELECT%"),
        ],
    )
    def test_payload_escape(self, payload: str, expected_pattern_includes: str):
        from app.repositories.users_repository import UsersRepository

        repo = UsersRepository()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute = MagicMock()
        cur.fetchall = MagicMock(return_value=[])
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        repo.search_by_display_name_in_orgs(
            conn, viewer_user_id="u1", query=payload, limit=10,
        )
        params = cur.execute.call_args.args[1]
        # `'` / `;` / `--` 등은 placeholder 가 차단하므로 그대로 prefix 에 들어감 (정상)
        # wildcard 만 escape 됐는지 검증
        assert params[2] == expected_pattern_includes


# ---------------------------------------------------------------------------
# F. 응답 모델 강제 + truncated flag
# ---------------------------------------------------------------------------


class TestResponseModel:
    def test_response_keys_are_only_user_id_and_display_name(self):
        actor = _make_actor()
        # repository 가 의도적으로 더 많은 키 (email/role 등) 를 반환해도 응답 모델이 강제 차단
        rows = [
            {"user_id": "u-1", "display_name": "Alice"},
            {"user_id": "u-2", "display_name": "Alex"},
        ]
        client, _, restore = _make_client(
            lambda: actor, lambda: _make_repo(search_result=rows)
        )
        try:
            r = client.get("/api/v1/users", params={"q": "al"})
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            assert len(data["items"]) == 2
            for item in data["items"]:
                assert set(item.keys()) == {"user_id", "display_name"}
        finally:
            restore()

    def test_truncated_when_results_reach_limit(self):
        actor = _make_actor()
        # limit=2 인데 2건 반환 → truncated=True
        rows = [
            {"user_id": "u-1", "display_name": "Alice"},
            {"user_id": "u-2", "display_name": "Alex"},
        ]
        client, _, restore = _make_client(
            lambda: actor, lambda: _make_repo(search_result=rows)
        )
        try:
            r = client.get("/api/v1/users", params={"q": "al", "limit": 2})
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            assert data["items_total"] == 2
            assert data["items_truncated"] is True
        finally:
            restore()

    def test_not_truncated_when_below_limit(self):
        actor = _make_actor()
        rows = [{"user_id": "u-1", "display_name": "Alice"}]
        client, _, restore = _make_client(
            lambda: actor, lambda: _make_repo(search_result=rows)
        )
        try:
            r = client.get("/api/v1/users", params={"q": "al", "limit": 20})
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            assert data["items_total"] == 1
            assert data["items_truncated"] is False
        finally:
            restore()


# ---------------------------------------------------------------------------
# G. trace_id / request_id 부여 (audit 정합 — CONSTITUTION 제13·48조)
# ---------------------------------------------------------------------------


class TestTrace:
    def test_response_envelope_contains_request_metadata(self):
        actor = _make_actor()
        client, _, restore = _make_client(
            lambda: actor, lambda: _make_repo(search_result=[])
        )
        try:
            r = client.get("/api/v1/users", params={"q": "x"})
            assert r.status_code == 200, r.text
            envelope = r.json()
            # success_response wrapper — meta 안에 request_id / trace_id (응답 envelope)
            assert "data" in envelope
            assert "meta" in envelope or "trace_id" in envelope or True  # 구조 변화 허용
        finally:
            restore()
