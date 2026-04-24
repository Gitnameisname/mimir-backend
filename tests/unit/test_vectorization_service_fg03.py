"""
S3 Phase 0 / FG 0-3 후속 S2-C — `app.services.vectorization_service` 유닛 테스트.

실 DB · 실 임베딩 서비스 · 실 Milvus 없이 MagicMock 으로 핵심 분기 커버.

커버 대상:
  - 헬퍼: _normalize_acl_values / _extract_acl_values
  - _get_permission_snapshot: published / draft / not_found / metadata-json-string / 에러 폴백
  - PermissionSnapshot dataclass 기본값
  - _fetch_version: 존재 / 부재
  - _fetch_nodes: nodes 테이블 hit / snapshot fallback
  - _parse_nodes_from_snapshot: heading / paragraph 혼합 + 빈 스냅샷
  - _soft_delete_existing_chunks: SQL 실행 확인
  - vectorize_version: 버전 부재 에러 경로
  - VectorizationResult dataclass 기본값

BUG-04 연관: chunk 저장 경로의 embedding 전달은 통합 테스트(FG 0-1 IT-02)에서 E2E 로 검증.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.vectorization_service import (
    PermissionSnapshot,
    VectorizationResult,
    _extract_acl_values,
    _get_permission_snapshot,
    _normalize_acl_values,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Mock 헬퍼
# --------------------------------------------------------------------------- #


def _make_conn(
    *,
    fetchone_values: list | None = None,
    fetchall_values: list | None = None,
    execute_raises: Exception | None = None,
):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    if execute_raises is not None:
        cur.execute = MagicMock(side_effect=execute_raises)
    else:
        cur.execute = MagicMock()
    if fetchone_values is not None:
        cur.fetchone = MagicMock(side_effect=list(fetchone_values))
    else:
        cur.fetchone = MagicMock(return_value=None)
    if fetchall_values is not None:
        cur.fetchall = MagicMock(side_effect=list(fetchall_values))
    else:
        cur.fetchall = MagicMock(return_value=[])
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur


DOC_ID = "dddddddd-1111-2222-3333-444444444444"
VER_ID = "vvvvvvvv-1111-2222-3333-555555555555"


# --------------------------------------------------------------------------- #
# 1) ACL helpers
# --------------------------------------------------------------------------- #


class TestNormalizeACLValues:
    def test_none_returns_empty_list(self):
        assert _normalize_acl_values(None) == []

    def test_single_string_trimmed(self):
        assert _normalize_acl_values("  user-1  ") == ["user-1"]

    def test_empty_string_omitted(self):
        assert _normalize_acl_values("") == []
        assert _normalize_acl_values("   ") == []

    def test_list_of_strings(self):
        assert _normalize_acl_values(["a", "b", "c"]) == ["a", "b", "c"]

    def test_list_filters_empty(self):
        assert _normalize_acl_values(["a", "", "  ", "b"]) == ["a", "b"]

    def test_non_iterable_coerced_to_string(self):
        # int 등 — str 변환 후 단일 리스트
        assert _normalize_acl_values(42) == ["42"]


class TestExtractACLValues:
    def test_merges_multiple_keys_deduplicated(self):
        metadata = {
            "organization_id": "org-a",
            "org_id": "org-a",   # 중복
            "organization_ids": ["org-b", "org-c"],
        }
        keys = ("organization_id", "org_id", "organization_ids", "org_ids")
        result = _extract_acl_values(metadata, keys)
        # 중복 제거 + 순서 유지 (dict.fromkeys)
        assert result == ["org-a", "org-b", "org-c"]

    def test_missing_keys_ignored(self):
        assert _extract_acl_values({}, ("a", "b", "c")) == []


# --------------------------------------------------------------------------- #
# 2) _get_permission_snapshot
# --------------------------------------------------------------------------- #


class TestPermissionSnapshot:
    def test_published_public_no_org_constraint(self):
        row = {
            "status": "published",
            "document_type": "policy",
            "metadata": {"visibility": "public"},
            "created_by": "author-1",
        }
        conn, _ = _make_conn(fetchone_values=[row])
        snap = _get_permission_snapshot(conn, DOC_ID)
        assert snap.is_public is True
        assert "VIEWER" in snap.accessible_roles
        assert "author-1" in snap.accessible_user_ids

    def test_published_with_restricted_visibility_not_public(self):
        row = {
            "status": "published",
            "document_type": "policy",
            "metadata": {"visibility": "restricted"},
            "created_by": "author-1",
        }
        conn, _ = _make_conn(fetchone_values=[row])
        snap = _get_permission_snapshot(conn, DOC_ID)
        assert snap.is_public is False
        # 여전히 published_roles 반환
        assert "VIEWER" in snap.accessible_roles

    def test_published_with_org_restriction_not_public(self):
        row = {
            "status": "published",
            "document_type": "policy",
            "metadata": {"organization_ids": ["org-1"]},
            "created_by": "author-1",
        }
        conn, _ = _make_conn(fetchone_values=[row])
        snap = _get_permission_snapshot(conn, DOC_ID)
        assert snap.is_public is False
        assert snap.accessible_org_ids == ["org-1"]

    def test_draft_document_uses_draft_roles(self):
        row = {
            "status": "draft",
            "document_type": "policy",
            "metadata": {},
            "created_by": "author-1",
        }
        conn, _ = _make_conn(fetchone_values=[row])
        snap = _get_permission_snapshot(conn, DOC_ID)
        # VIEWER 는 draft 권한에 포함되지 않음
        assert "VIEWER" not in snap.accessible_roles
        assert "AUTHOR" in snap.accessible_roles
        assert snap.is_public is False

    def test_metadata_as_json_string_is_parsed(self):
        """metadata 가 JSON 문자열로 저장된 경우 json.loads 수행."""
        row = {
            "status": "published",
            "document_type": "policy",
            "metadata": json.dumps({"user_id": "user-xyz"}),
            "created_by": "author-1",
        }
        conn, _ = _make_conn(fetchone_values=[row])
        snap = _get_permission_snapshot(conn, DOC_ID)
        assert "user-xyz" in snap.accessible_user_ids

    def test_document_not_found_returns_default_snapshot(self):
        conn, _ = _make_conn(fetchone_values=[None])
        snap = _get_permission_snapshot(conn, DOC_ID)
        # 빈 스냅샷 (dataclass 기본값)
        assert snap.accessible_roles == []
        assert snap.accessible_user_ids == []
        assert snap.accessible_org_ids == []
        assert snap.is_public is False

    def test_sql_error_returns_default_snapshot_gracefully(self):
        """권한 조회 실패 시 예외 전파하지 않고 빈 스냅샷 반환."""
        conn, _ = _make_conn(execute_raises=RuntimeError("DB timeout"))
        snap = _get_permission_snapshot(conn, DOC_ID)
        assert isinstance(snap, PermissionSnapshot)
        assert snap.accessible_roles == []


# --------------------------------------------------------------------------- #
# 3) PermissionSnapshot / VectorizationResult dataclass
# --------------------------------------------------------------------------- #


class TestDataclassDefaults:
    def test_permission_snapshot_defaults(self):
        snap = PermissionSnapshot()
        assert snap.accessible_roles == []
        assert snap.accessible_user_ids == []
        assert snap.accessible_org_ids == []
        assert snap.is_public is False

    def test_vectorization_result_defaults(self):
        r = VectorizationResult(document_id="d", version_id="v")
        assert r.chunks_created == 0
        assert r.chunks_failed == 0
        assert r.total_tokens == 0
        assert r.model == ""
        assert r.error is None
        assert r.job_id is None


# --------------------------------------------------------------------------- #
# 4) VectorizationPipeline._fetch_version / _fetch_nodes / _parse_nodes
# --------------------------------------------------------------------------- #


class TestPipelineHelpers:
    def test_fetch_version_returns_row(self):
        from app.services.vectorization_service import VectorizationPipeline
        p = VectorizationPipeline()
        row = {
            "id": VER_ID,
            "document_id": DOC_ID,
            "version_status": "published",
            "document_type": "policy",
            "document_status": "published",
        }
        conn, _ = _make_conn(fetchone_values=[row])
        result = p._fetch_version(conn, VER_ID, DOC_ID)
        assert result == row

    def test_fetch_version_missing_returns_none(self):
        from app.services.vectorization_service import VectorizationPipeline
        p = VectorizationPipeline()
        conn, _ = _make_conn(fetchone_values=[None])
        assert p._fetch_version(conn, VER_ID, DOC_ID) is None

    def test_fetch_nodes_uses_nodes_table_when_present(self):
        from app.services.vectorization_service import VectorizationPipeline
        p = VectorizationPipeline()
        nodes = [
            {"id": "n1", "parent_id": None, "node_type": "heading", "order_index": 0, "title": "t", "content": None},
            {"id": "n2", "parent_id": None, "node_type": "paragraph", "order_index": 1, "title": None, "content": "c"},
        ]
        conn, _ = _make_conn(fetchall_values=[nodes])
        result = p._fetch_nodes(conn, VER_ID)
        assert result == nodes

    def test_fetch_nodes_fallback_to_snapshot_when_empty(self):
        """nodes 테이블 비면 _parse_nodes_from_snapshot 호출."""
        from app.services.vectorization_service import VectorizationPipeline
        p = VectorizationPipeline()
        snapshot = {
            "content": [
                {"type": "heading", "content": [{"type": "text", "text": "Title"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Body text"}]},
            ]
        }
        # 첫 fetchall = 빈 리스트 (nodes 부재) / 두 번째 fetchone = snapshot
        conn, _ = _make_conn(
            fetchall_values=[[]],
            fetchone_values=[{"content_snapshot": snapshot}],
        )
        result = p._fetch_nodes(conn, VER_ID)
        assert len(result) == 2
        assert result[0]["node_type"] == "heading"
        assert result[0]["title"] == "Title"
        assert result[1]["node_type"] == "paragraph"
        assert result[1]["content"] == "Body text"

    def test_parse_nodes_from_snapshot_handles_json_string(self):
        """content_snapshot 이 JSON 문자열인 경우 json.loads 수행."""
        from app.services.vectorization_service import VectorizationPipeline
        p = VectorizationPipeline()
        snapshot_json = json.dumps({
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Hi"}]},
            ]
        })
        conn, _ = _make_conn(fetchone_values=[{"content_snapshot": snapshot_json}])
        result = p._parse_nodes_from_snapshot(conn, VER_ID)
        assert len(result) == 1
        assert result[0]["content"] == "Hi"

    def test_parse_nodes_from_snapshot_empty_content(self):
        from app.services.vectorization_service import VectorizationPipeline
        p = VectorizationPipeline()
        conn, _ = _make_conn(fetchone_values=[{"content_snapshot": None}])
        assert p._parse_nodes_from_snapshot(conn, VER_ID) == []

    def test_parse_nodes_from_snapshot_invalid_json_returns_empty(self):
        """깨진 JSON 문자열 → 빈 리스트 반환 (예외 전파 안함)."""
        from app.services.vectorization_service import VectorizationPipeline
        p = VectorizationPipeline()
        conn, _ = _make_conn(fetchone_values=[{"content_snapshot": "not-json{"}])
        assert p._parse_nodes_from_snapshot(conn, VER_ID) == []

    def test_soft_delete_existing_chunks_executes_update(self):
        from app.services.vectorization_service import VectorizationPipeline
        p = VectorizationPipeline()
        conn, cur = _make_conn()
        p._soft_delete_existing_chunks(conn, VER_ID)
        sql = cur.execute.call_args.args[0]
        assert "UPDATE document_chunks" in sql
        assert "is_current = FALSE" in sql
        assert "WHERE version_id = %s::uuid AND is_current = TRUE" in sql


# --------------------------------------------------------------------------- #
# 5) vectorize_version 에러 경로
# --------------------------------------------------------------------------- #


class TestVectorizeVersionErrorPath:
    def test_version_not_found_returns_error_result(self, monkeypatch):
        """_fetch_version 이 None 이면 VectorizationResult.error 설정."""
        from app.services.vectorization_service import VectorizationPipeline

        p = VectorizationPipeline()
        # _fetch_version 만 None 반환하도록 패치 — 나머지 경로는 타지 않음
        monkeypatch.setattr(p, "_fetch_version", lambda conn, v, d: None)

        conn = MagicMock()
        result = p.vectorize_version(conn, document_id=DOC_ID, version_id=VER_ID)

        assert result.error is not None
        assert "찾을 수 없" in result.error or "not found" in result.error.lower() or VER_ID in result.error
        assert result.chunks_created == 0

    def test_no_chunks_generated_returns_early(self, monkeypatch):
        """chunking_service 가 빈 리스트를 반환하면 embed 호출 없이 즉시 반환."""
        from app.services import vectorization_service as mod
        from app.services.vectorization_service import VectorizationPipeline

        p = VectorizationPipeline()

        # 버전 정보 + chunking_config 정상, 노드는 임의, chunks=[]
        version_row = {
            "id": VER_ID, "document_id": DOC_ID,
            "version_status": "published",
            "document_type": "policy",
            "document_status": "published",
        }
        monkeypatch.setattr(p, "_fetch_version", lambda conn, v, d: version_row)
        monkeypatch.setattr(p, "_fetch_nodes", lambda conn, v: [])

        # chunking_service.get_chunking_config_for_type, chunk_version 를 mock
        fake_config = MagicMock()
        fake_config.strategy = "section"
        fake_config.max_chunk_tokens = 500
        fake_config.min_chunk_tokens = 50
        fake_config.overlap_tokens = 50
        fake_config.include_parent_context = False
        fake_config.index_version_policy = "published_only"

        monkeypatch.setattr(
            mod.chunking_service, "get_chunking_config_for_type",
            lambda conn, doc_type: fake_config,
        )
        monkeypatch.setattr(
            mod.chunking_service, "chunk_version",
            lambda **kwargs: [],
        )

        # embedding provider 는 호출되지 않아야 함 (chunks 비었으므로)
        fake_provider = MagicMock()
        p._embedding_provider = fake_provider

        conn = MagicMock()
        result = p.vectorize_version(conn, document_id=DOC_ID, version_id=VER_ID)

        # 에러 없이 반환
        assert result.error is None
        assert result.chunks_created == 0
        # 임베딩 provider 는 호출되지 않음
        assert not fake_provider.embed_batch.called

    def test_vectorize_version_catches_pipeline_exception_and_records_error(self, monkeypatch):
        """파이프라인 중 예외 발생 시 result.error 에 기록하고 조용히 반환."""
        from app.services.vectorization_service import VectorizationPipeline

        p = VectorizationPipeline()
        monkeypatch.setattr(
            p, "_fetch_version",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        conn = MagicMock()
        result = p.vectorize_version(conn, document_id=DOC_ID, version_id=VER_ID)
        assert result.error == "boom"
