"""
S3 Phase 0 / FG 0-3 후속 S3-B — `app.services.vectorization_service` 나머지 분기 커버.

세션 2 에서는 ACL 헬퍼 / _get_permission_snapshot / _fetch_version / _parse_nodes_from_snapshot /
_soft_delete_existing_chunks / vectorize_version 에러 경로를 커버했다.

본 파일은 나머지 분기:
  - `update_permission_metadata` — rowcount 반환
  - `semantic_search` — Milvus 불가 / 빈 후보 / 정상 / actor_role ACL 필터
  - `cleanup_old_chunks` — rowcount 반환
  - `_record_token_usage` — happy + exception 폴백
  - `_save_chunks` — happy (SAVEPOINT) + 일부 청크 실패 롤백 + Milvus upsert
  - `vectorize_all_published` — document_type 필터 on/off / rows 빈 경우
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


DOC_ID = "doc-uuid-00000000-0000-0000-0000-000000000001"
VER_ID = "ver-uuid-00000000-0000-0000-0000-000000000001"


def _make_conn(*, fetchone_values=None, fetchall_values=None, rowcount=0):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    cur.fetchone = MagicMock(side_effect=list(fetchone_values)) if fetchone_values is not None else MagicMock(return_value=None)
    cur.fetchall = MagicMock(side_effect=list(fetchall_values)) if fetchall_values is not None else MagicMock(return_value=[])
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur


def _chunk(**kw):
    """DocumentChunk 대신 SimpleNamespace 로 필요한 속성만 제공."""
    base = {
        "document_id": DOC_ID, "version_id": VER_ID,
        "node_id": "n1", "chunk_index": 0,
        "source_text": "text", "token_count": 10,
        "node_path": ["root"], "document_type": "policy",
        "document_status": "published",
        "accessible_roles": ["VIEWER", "AUTHOR"],
        "accessible_user_ids": ["u1"],
        "accessible_org_ids": ["org-1"],
        "is_public": True,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# 1) update_permission_metadata
# --------------------------------------------------------------------------- #


class TestUpdatePermissionMetadata:
    def test_updates_chunks_and_returns_rowcount(self, monkeypatch):
        from app.services import vectorization_service as mod

        fake_perm = SimpleNamespace(
            accessible_roles=["VIEWER"],
            accessible_user_ids=["u1"],
            accessible_org_ids=["org-1"],
            is_public=True,
        )
        monkeypatch.setattr(mod, "_get_permission_snapshot", lambda conn, doc: fake_perm)

        conn, cur = _make_conn(rowcount=7)
        result = mod.vectorization_pipeline.update_permission_metadata(conn, DOC_ID)
        assert result == 7

        sql = cur.execute.call_args.args[0]
        assert "UPDATE document_chunks" in sql
        for field in ("accessible_roles", "accessible_user_ids", "accessible_org_ids", "is_public"):
            assert f"{field} = %s" in sql
        # 파라미터 순서
        params = cur.execute.call_args.args[1]
        assert params[0] == ["VIEWER"]
        assert params[-1] == DOC_ID

    def test_zero_rowcount_returns_zero(self, monkeypatch):
        from app.services import vectorization_service as mod

        monkeypatch.setattr(
            mod, "_get_permission_snapshot",
            lambda c, d: SimpleNamespace(
                accessible_roles=[], accessible_user_ids=[],
                accessible_org_ids=[], is_public=False,
            ),
        )
        conn, _ = _make_conn(rowcount=0)
        assert mod.vectorization_pipeline.update_permission_metadata(conn, DOC_ID) == 0


# --------------------------------------------------------------------------- #
# 2) cleanup_old_chunks
# --------------------------------------------------------------------------- #


class TestCleanupOldChunks:
    def test_executes_delete_and_returns_rowcount(self):
        from app.services.vectorization_service import vectorization_pipeline

        conn, cur = _make_conn(rowcount=42)
        result = vectorization_pipeline.cleanup_old_chunks(conn, days_old=30)
        assert result == 42

        sql = cur.execute.call_args.args[0]
        assert "DELETE FROM document_chunks" in sql
        assert "is_current = FALSE" in sql
        # days_old 파라미터 전달
        params = cur.execute.call_args.args[1]
        assert params == (30,)

    def test_default_days_old(self):
        from app.services.vectorization_service import vectorization_pipeline

        conn, cur = _make_conn(rowcount=0)
        vectorization_pipeline.cleanup_old_chunks(conn)
        params = cur.execute.call_args.args[1]
        assert params == (30,)  # 기본값


# --------------------------------------------------------------------------- #
# 3) semantic_search
# --------------------------------------------------------------------------- #


class TestSemanticSearch:
    def test_returns_empty_when_embedding_fails(self, monkeypatch):
        from app.services.vectorization_service import VectorizationPipeline

        p = VectorizationPipeline()
        fake_provider = MagicMock()
        fake_provider.embed_single.return_value = None   # 실패
        p._embedding_provider = fake_provider

        conn = MagicMock()
        result = p.semantic_search(conn, query="test query")
        assert result == []

    def test_returns_empty_when_embedding_all_zero(self, monkeypatch):
        from app.services.vectorization_service import VectorizationPipeline

        p = VectorizationPipeline()
        fake_provider = MagicMock()
        fake_provider.embed_single.return_value = [0.0, 0.0, 0.0]  # 빈 벡터
        p._embedding_provider = fake_provider

        result = p.semantic_search(MagicMock(), query="x")
        assert result == []

    def test_returns_empty_when_milvus_unavailable(self, monkeypatch):
        from app.services import vectorization_service as mod
        from app.services.vectorization_service import VectorizationPipeline

        p = VectorizationPipeline()
        fake_provider = MagicMock()
        fake_provider.embed_single.return_value = [0.1, 0.2, 0.3]
        p._embedding_provider = fake_provider

        fake_milvus = MagicMock()
        fake_milvus.is_available.return_value = False
        monkeypatch.setattr("app.db.milvus.get_milvus", lambda: fake_milvus)

        result = p.semantic_search(MagicMock(), query="q")
        assert result == []

    def test_returns_empty_when_no_candidates(self, monkeypatch):
        from app.services.vectorization_service import VectorizationPipeline

        p = VectorizationPipeline()
        fake_provider = MagicMock()
        fake_provider.embed_single.return_value = [0.1] * 10
        p._embedding_provider = fake_provider

        fake_milvus = MagicMock()
        fake_milvus.is_available.return_value = True
        fake_milvus.search.return_value = []   # 후보 없음
        monkeypatch.setattr("app.db.milvus.get_milvus", lambda: fake_milvus)

        result = p.semantic_search(MagicMock(), query="q")
        assert result == []

    def test_happy_path_with_acl_filters(self, monkeypatch):
        from app.services.vectorization_service import VectorizationPipeline

        p = VectorizationPipeline()
        fake_provider = MagicMock()
        fake_provider.embed_single.return_value = [0.1] * 10
        p._embedding_provider = fake_provider

        fake_milvus = MagicMock()
        fake_milvus.is_available.return_value = True
        fake_milvus.search.return_value = ["c-1", "c-2", "c-3"]
        monkeypatch.setattr("app.db.milvus.get_milvus", lambda: fake_milvus)

        rows = [
            {
                "id": "c-1", "document_id": DOC_ID, "version_id": VER_ID,
                "node_id": None, "chunk_index": 0,
                "source_text": "hit 1", "node_path": ["root"],
                "document_type": "policy", "document_status": "published",
                "token_count": 50,
            },
            {
                "id": "c-2", "document_id": DOC_ID, "version_id": VER_ID,
                "node_id": "n1", "chunk_index": 1,
                "source_text": "hit 2", "node_path": None,
                "document_type": "policy", "document_status": "published",
                "token_count": 60,
            },
        ]
        conn, cur = _make_conn(fetchall_values=[rows])
        result = p.semantic_search(
            conn, query="q",
            actor_role="AUTHOR", actor_user_id="u-1", organization_id="org-1",
            document_type="policy", top_k=5,
        )
        assert len(result) == 2
        assert result[0]["chunk_id"] == "c-1"
        assert result[0]["similarity"] == pytest.approx(1.0)
        assert result[1]["similarity"] == pytest.approx(0.99)
        # node_path None 은 빈 리스트로 정규화
        assert result[1]["node_path"] == []

        # SQL 에 ACL / document_type 필터 모두 포함
        sql = cur.execute.call_args.args[0]
        assert "is_public = TRUE" in sql
        assert "%s = ANY(accessible_roles)" in sql
        assert "%s = ANY(accessible_user_ids)" in sql
        assert "%s = ANY(accessible_org_ids)" in sql
        assert "document_type = %s" in sql
        # id IN (...) 에 후보 3개가 포함됨
        assert "'c-1'" in sql and "'c-2'" in sql and "'c-3'" in sql


# --------------------------------------------------------------------------- #
# 4) _record_token_usage
# --------------------------------------------------------------------------- #


class TestRecordTokenUsage:
    def test_happy_path_wraps_in_savepoint(self):
        from app.services.vectorization_service import vectorization_pipeline

        conn, cur = _make_conn()
        vectorization_pipeline._record_token_usage(
            conn,
            document_id=DOC_ID, job_id="job-1",
            model="text-embedding-3-small", total_tokens=1200,
            chunk_count=5,
        )
        # SAVEPOINT / INSERT / RELEASE 세 번 execute
        executed_sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert any("SAVEPOINT sp_token_usage" in s for s in executed_sqls)
        assert any("INSERT INTO embedding_token_usage" in s for s in executed_sqls)
        assert any("RELEASE SAVEPOINT sp_token_usage" in s for s in executed_sqls)

    def test_insert_failure_rollbacks_savepoint(self):
        """INSERT 실패 → ROLLBACK TO SAVEPOINT 호출 + 예외 전파 없음."""
        from app.services.vectorization_service import vectorization_pipeline

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)

        call_count = {"i": 0}

        def _exec(sql, params=None):
            call_count["i"] += 1
            # 2번째 execute (INSERT) 에서 예외
            if call_count["i"] == 2:
                raise RuntimeError("boom")
            return None

        cur.execute = MagicMock(side_effect=_exec)
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        # 예외 전파 금지
        vectorization_pipeline._record_token_usage(
            conn,
            document_id=DOC_ID, job_id=None,
            model="m", total_tokens=0, chunk_count=0,
        )
        # ROLLBACK TO SAVEPOINT 호출됐는지 확인 (세 번째 execute)
        executed_sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert any("ROLLBACK TO SAVEPOINT" in s for s in executed_sqls)


# --------------------------------------------------------------------------- #
# 5) _save_chunks
# --------------------------------------------------------------------------- #


class TestSaveChunks:
    def test_saves_all_chunks_and_upserts_milvus(self, monkeypatch):
        from app.services.vectorization_service import vectorization_pipeline

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute = MagicMock()
        # SAVEPOINT / INSERT(RETURNING id) / RELEASE 반복.
        # fetchone 은 INSERT 후 호출되어 {"id": chunk_id} 반환.
        cur.fetchone = MagicMock(side_effect=[
            {"id": "new-chunk-1"},
            {"id": "new-chunk-2"},
        ])
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        fake_milvus = MagicMock()
        fake_milvus.upsert_batch = MagicMock()
        monkeypatch.setattr("app.db.milvus.get_milvus", lambda: fake_milvus)

        chunks = [_chunk(chunk_index=0), _chunk(chunk_index=1)]
        embeddings = [[0.1, 0.2], [0.3, 0.4]]

        saved, failed = vectorization_pipeline._save_chunks(
            conn, chunks, embeddings, "model-a",
        )
        assert saved == 2
        assert failed == 0
        # Milvus upsert 호출됨
        fake_milvus.upsert_batch.assert_called_once()
        records = fake_milvus.upsert_batch.call_args.args[0]
        assert len(records) == 2
        assert records[0]["chunk_id"] == "new-chunk-1"
        assert records[1]["embedding"] == [0.3, 0.4]

    def test_partial_failure_rollbacks_only_failed_chunk(self, monkeypatch):
        """첫 청크 정상, 두 번째 INSERT 실패 → ROLLBACK TO SAVEPOINT."""
        from app.services.vectorization_service import vectorization_pipeline

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)

        call_log: list[str] = []

        def _exec(sql, params=None):
            call_log.append(sql)
            # 첫 번째 chunk: SAVEPOINT(1) + INSERT(2) + RELEASE(3) 정상
            # 두 번째 chunk: SAVEPOINT(4) + INSERT(5) 실패
            if len(call_log) == 5:
                raise RuntimeError("insert failed")

        cur.execute = MagicMock(side_effect=_exec)
        cur.fetchone = MagicMock(return_value={"id": "c1"})

        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        fake_milvus = MagicMock()
        monkeypatch.setattr("app.db.milvus.get_milvus", lambda: fake_milvus)

        chunks = [_chunk(chunk_index=0), _chunk(chunk_index=1)]
        embeddings = [[0.1], [0.2]]

        saved, failed = vectorization_pipeline._save_chunks(
            conn, chunks, embeddings, "model",
        )
        assert saved == 1
        assert failed == 1
        # ROLLBACK 호출됐음
        assert any("ROLLBACK TO SAVEPOINT" in s for s in call_log)

    def test_milvus_upsert_failure_does_not_propagate(self, monkeypatch):
        """Milvus upsert 실패도 조용히 로그만 남김."""
        from app.services.vectorization_service import vectorization_pipeline

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute = MagicMock()
        cur.fetchone = MagicMock(return_value={"id": "c1"})
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        fake_milvus = MagicMock()
        fake_milvus.upsert_batch = MagicMock(side_effect=RuntimeError("milvus down"))
        monkeypatch.setattr("app.db.milvus.get_milvus", lambda: fake_milvus)

        chunks = [_chunk()]
        embeddings = [[0.1]]
        saved, failed = vectorization_pipeline._save_chunks(
            conn, chunks, embeddings, "m",
        )
        # PG insert 는 성공했으니 saved=1
        assert saved == 1
        assert failed == 0

    def test_zero_embeddings_skip_milvus_upsert(self, monkeypatch):
        """embedding 이 모두 0 이면 milvus records 에 추가되지 않음."""
        from app.services.vectorization_service import vectorization_pipeline

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute = MagicMock()
        cur.fetchone = MagicMock(return_value={"id": "c1"})
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        fake_milvus = MagicMock()
        monkeypatch.setattr("app.db.milvus.get_milvus", lambda: fake_milvus)

        chunks = [_chunk()]
        embeddings = [[0.0, 0.0, 0.0]]    # all-zero → any() False
        saved, failed = vectorization_pipeline._save_chunks(
            conn, chunks, embeddings, "m",
        )
        assert saved == 1
        # Milvus upsert 는 호출되지 않음 (all-zero 는 건너뜀)
        assert not fake_milvus.upsert_batch.called


# --------------------------------------------------------------------------- #
# 6) vectorize_all_published
# --------------------------------------------------------------------------- #


class TestVectorizeAllPublished:
    def test_iterates_rows_and_aggregates_results(self, monkeypatch):
        from app.services.vectorization_service import (
            VectorizationPipeline,
            VectorizationResult,
        )

        p = VectorizationPipeline()

        # SELECT 결과 — published 문서 3건
        rows = [
            {"document_id": "d1", "version_id": "v1"},
            {"document_id": "d2", "version_id": "v2"},
            {"document_id": "d3", "version_id": "v3"},
        ]
        conn, cur = _make_conn(fetchall_values=[rows])

        # vectorize_version 은 모의: d1/d2 성공, d3 실패
        def _fake_vectorize(conn, *, document_id, version_id, job_id=None):
            if document_id == "d3":
                return VectorizationResult(
                    document_id=document_id, version_id=version_id,
                    error="simulated failure",
                )
            return VectorizationResult(
                document_id=document_id, version_id=version_id,
                chunks_created=3,
            )

        monkeypatch.setattr(p, "vectorize_version", _fake_vectorize)
        monkeypatch.setattr("time.sleep", lambda *_: None)  # 테스트 가속

        result = p.vectorize_all_published(conn, limit=10)
        assert result == {"total": 3, "succeeded": 2, "failed": 1}

    def test_document_type_filter_appends_where_clause(self, monkeypatch):
        from app.services.vectorization_service import VectorizationPipeline

        p = VectorizationPipeline()
        conn, cur = _make_conn(fetchall_values=[[]])

        monkeypatch.setattr("time.sleep", lambda *_: None)

        p.vectorize_all_published(conn, document_type="policy", limit=50)

        sql = cur.execute.call_args.args[0]
        assert "d.document_type = %s" in sql
        params = cur.execute.call_args.args[1]
        # ("published", "policy", 50)
        assert "published" in params
        assert "policy" in params
        assert 50 in params

    def test_no_rows_returns_all_zero(self, monkeypatch):
        from app.services.vectorization_service import VectorizationPipeline

        p = VectorizationPipeline()
        conn, _ = _make_conn(fetchall_values=[[]])
        monkeypatch.setattr("time.sleep", lambda *_: None)
        result = p.vectorize_all_published(conn)
        assert result == {"total": 0, "succeeded": 0, "failed": 0}
