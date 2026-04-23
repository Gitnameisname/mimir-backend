"""
S3 Phase 0 / FG 0-5 — 벡터화 상태 판정 유닛 테스트.

작업지시서 §4.6 상태 판정 분기 테스트 ≥ 8건 + 권한 3건 + 쿨다운 1건 = ≥ 12건.

실 DB 없이 MagicMock 기반으로 분기를 커버한다. 실 DB 연동은 FG 0-1 의 IT-02 확장
(`test_it02_rag_after_publish.py`) 에서 수행.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from app.services.vectorization_status_service import (
    can_user_reindex,
    get_vectorization_status,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Mock 헬퍼 — fetchone 을 3단계(document_meta / index_state / recent_failure) 로 분기
# --------------------------------------------------------------------------- #


def _make_conn(
    *,
    meta: Optional[dict[str, Any]],
    index_state: dict[str, Any],
    failure: Optional[dict[str, Any]] = None,
) -> MagicMock:
    """3번의 SELECT 각각에 대한 반환값을 순서대로 제공하는 mock connection."""
    responses = [
        meta,                              # documents
        index_state,                        # document_chunks aggregate
        failure,                            # audit_events (최근 실패)
    ]

    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    # fetchone 이 호출될 때마다 responses 에서 하나씩 꺼냄
    cur.fetchone = MagicMock(side_effect=responses)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn


DOC_ID = "11111111-1111-1111-1111-111111111111"
VER1 = "22222222-2222-2222-2222-222222222222"
VER2 = "33333333-3333-3333-3333-333333333333"


# --------------------------------------------------------------------------- #
# 상태 판정 분기 — 8건
# --------------------------------------------------------------------------- #


class TestStatusJudgment:
    def test_not_applicable_when_no_published_version(self):
        conn = _make_conn(
            meta={"id": DOC_ID, "current_published_version_id": None, "created_by": "author-1"},
            index_state={"version_ids": [], "chunk_count": 0, "last_at": None},
            failure=None,
        )
        info = get_vectorization_status(conn, DOC_ID)
        assert info is not None
        assert info.status == "not_applicable"
        assert info.chunk_count == 0
        assert info.latest_published_version_id is None

    def test_pending_when_published_but_no_chunks(self):
        conn = _make_conn(
            meta={"id": DOC_ID, "current_published_version_id": VER1, "created_by": "author-1"},
            index_state={"version_ids": [], "chunk_count": 0, "last_at": None},
            failure=None,
        )
        info = get_vectorization_status(conn, DOC_ID)
        assert info is not None
        assert info.status == "pending"
        assert info.latest_published_version_id == VER1
        assert info.indexed_version_id is None

    def test_indexed_when_latest_version_is_indexed(self):
        now = datetime.now(timezone.utc)
        conn = _make_conn(
            meta={"id": DOC_ID, "current_published_version_id": VER1, "created_by": "author-1"},
            index_state={"version_ids": [VER1], "chunk_count": 42, "last_at": now},
            failure=None,
        )
        info = get_vectorization_status(conn, DOC_ID)
        assert info.status == "indexed"
        assert info.chunk_count == 42
        assert info.last_vectorized_at == now
        assert info.indexed_version_id == VER1

    def test_stale_when_latest_version_not_in_indexed(self):
        now = datetime.now(timezone.utc)
        conn = _make_conn(
            meta={"id": DOC_ID, "current_published_version_id": VER2, "created_by": "author-1"},
            # VER1 은 indexed 이나 최신 published 는 VER2 → stale
            index_state={"version_ids": [VER1], "chunk_count": 10, "last_at": now},
            failure=None,
        )
        info = get_vectorization_status(conn, DOC_ID)
        assert info.status == "stale"
        assert info.latest_published_version_id == VER2
        assert info.indexed_version_id == VER1   # 기존 색인 버전을 참고로 반환

    def test_failed_when_recent_failure_after_last_vectorized(self):
        last_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        fail_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        conn = _make_conn(
            meta={"id": DOC_ID, "current_published_version_id": VER1, "created_by": "author-1"},
            index_state={"version_ids": [VER1], "chunk_count": 5, "last_at": last_at},
            failure={"occurred_at": fail_at, "reason": "MilvusConnectionError: ..."},
        )
        info = get_vectorization_status(conn, DOC_ID)
        assert info.status == "failed"
        assert info.last_error is not None
        assert "Milvus" in info.last_error

    def test_indexed_ignores_old_failure(self):
        """last_vectorized_at 이후 실패 기록이 없으면 failed 가 아님 (재벡터화로 복구된 케이스)."""
        last_at = datetime.now(timezone.utc)
        # 실패는 last_at 이전 — get_vectorization_status 의 after 인자로 걸러져야 함
        conn = _make_conn(
            meta={"id": DOC_ID, "current_published_version_id": VER1, "created_by": "author-1"},
            index_state={"version_ids": [VER1], "chunk_count": 10, "last_at": last_at},
            failure=None,  # _fetch_recent_failure 에서 after=last_at 로 걸러지면 None 반환
        )
        info = get_vectorization_status(conn, DOC_ID)
        assert info.status == "indexed"
        assert info.last_error is None

    def test_returns_none_when_document_missing(self):
        conn = _make_conn(
            meta=None,
            index_state={"version_ids": [], "chunk_count": 0, "last_at": None},
            failure=None,
        )
        info = get_vectorization_status(conn, DOC_ID)
        assert info is None

    def test_to_dict_shape_for_api(self):
        """to_dict 출력이 프론트엔드 스키마와 일치."""
        now = datetime.now(timezone.utc)
        conn = _make_conn(
            meta={"id": DOC_ID, "current_published_version_id": VER1, "created_by": "author-1"},
            index_state={"version_ids": [VER1], "chunk_count": 3, "last_at": now},
            failure=None,
        )
        info = get_vectorization_status(
            conn, DOC_ID, actor_user_id="admin-1", actor_role="SUPER_ADMIN",
        )
        d = info.to_dict()
        for key in (
            "document_id", "status", "latest_published_version_id", "indexed_version_id",
            "chunk_count", "last_vectorized_at", "last_error",
            "can_reindex", "reindex_cooldown_sec",
        ):
            assert key in d, f"키 누락: {key}"
        assert d["status"] == "indexed"
        assert d["can_reindex"] is True
        assert d["last_vectorized_at"] is not None  # ISO 형식


# --------------------------------------------------------------------------- #
# 권한 판정 (can_user_reindex) — 3건
# --------------------------------------------------------------------------- #


class TestCanReindex:
    def test_admin_role_allows(self):
        for role in ("ADMIN", "SUPER_ADMIN", "ORG_ADMIN", "super_admin"):
            assert can_user_reindex(
                actor_user_id="x", actor_role=role, document_created_by="y",
            ), f"role={role} 가 admin 로 인식되지 않음"

    def test_document_creator_allows(self):
        assert can_user_reindex(
            actor_user_id="creator-1",
            actor_role="AUTHOR",
            document_created_by="creator-1",
        )

    def test_third_party_denied(self):
        assert not can_user_reindex(
            actor_user_id="other-user",
            actor_role="AUTHOR",
            document_created_by="creator-1",
        )

    def test_unauthenticated_denied(self):
        assert not can_user_reindex(
            actor_user_id=None, actor_role=None, document_created_by="creator-1",
        )


# --------------------------------------------------------------------------- #
# 쿨다운 — 1건 (in-memory 폴백 경로로 검증, Valkey 미연결 환경)
# --------------------------------------------------------------------------- #


class TestCooldown:
    def test_second_call_within_ttl_is_rejected(self, monkeypatch):
        """Valkey 미연결 환경에서 in-memory 폴백으로 쿨다운이 동작."""
        # Valkey 접속 실패를 강제 — get_valkey 가 예외를 던지도록 patch
        from app.services import vectorization_cooldown as cd

        def _boom():
            raise RuntimeError("Valkey unreachable (simulated)")

        monkeypatch.setattr("app.cache.get_valkey", _boom)

        # 1차 acquire — 성공
        r1 = cd.try_acquire("doc-xxx", "user-xxx", ttl_sec=5)
        assert r1.acquired is True
        assert r1.backend == "inmem"

        # 2차 acquire — 실패 (쿨다운 중)
        r2 = cd.try_acquire("doc-xxx", "user-xxx", ttl_sec=5)
        assert r2.acquired is False
        assert r2.remaining_sec > 0
        assert r2.backend == "inmem"

    def test_different_actor_not_blocked_by_other(self, monkeypatch):
        """동일 문서여도 actor 가 다르면 독립 쿨다운."""
        from app.services import vectorization_cooldown as cd

        def _boom():
            raise RuntimeError("Valkey unreachable (simulated)")

        monkeypatch.setattr("app.cache.get_valkey", _boom)

        r1 = cd.try_acquire("doc-yyy", "user-A", ttl_sec=5)
        r2 = cd.try_acquire("doc-yyy", "user-B", ttl_sec=5)
        assert r1.acquired is True
        assert r2.acquired is True  # 서로 다른 key
