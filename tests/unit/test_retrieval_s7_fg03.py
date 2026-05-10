"""
S3 Phase 0 / FG 0-3 후속 S7 — retrieval 마무리 (vector_retriever + conversation_compressor + citation_service).

커버:
  - VectorRetriever: retrieve (embedding 실패/SQL 실패/document_type 유무/ACL + threshold) + _row_to_result
  - ConversationCompressor: __init__ / compress (빈 메시지 / sliding 기본 / LLM 성공 / LLM 빈 응답 / LLM 예외)
    + _sliding_window (메시지 truncate + role 라벨) + _load_summary_prompt 분기
  - CitationService: verify (hash 일치/불일치 / 청크 부재) + get_content + _fetch_chunk (nil node_id IS NULL /
    명시 node_id / SQL 실패)
"""
from __future__ import annotations

import hashlib
import uuid as uuidlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


DOC_UUID = uuidlib.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
VER_UUID = uuidlib.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
NODE_UUID = uuidlib.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")


def _make_conn(*, fetchone_value=None, fetchall_value=None, raise_on_execute=False):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    if raise_on_execute:
        cur.execute = MagicMock(side_effect=RuntimeError("SQL fail"))
    else:
        cur.execute = MagicMock()
    cur.fetchone = MagicMock(return_value=fetchone_value)
    cur.fetchall = MagicMock(return_value=fetchall_value or [])
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur


# =========================================================================== #
# 1) VectorRetriever
# =========================================================================== #


def _vec_row(**kw):
    """PG document_chunks JOIN documents 결과 행 (Milvus 전환 후 — score 별 인자, metadata 컬럼 부재)."""
    base = {
        "chunk_id": uuidlib.UUID("aaaaaaaa-0000-0000-0000-000000000001"),
        "document_id": DOC_UUID, "version_id": VER_UUID, "node_id": NODE_UUID,
        "source_text": "청크 본문",
        "document_type": "policy",
        "document_title": "Doc 1",
    }
    base.update(kw)
    return base


def _make_milvus(*, available=True, candidates=None, search_raises=False):
    """Milvus stub — search_with_score 가 (chunk_id, similarity) 튜플 반환."""
    fake = MagicMock()
    fake.is_available = MagicMock(return_value=available)
    if search_raises:
        fake.search_with_score = MagicMock(side_effect=RuntimeError("milvus fail"))
    else:
        fake.search_with_score = MagicMock(return_value=candidates or [])
    return fake


class TestVectorRetrieverRowToResult:
    def test_happy(self):
        from app.services.retrieval.vector_retriever import VectorRetriever
        r = VectorRetriever._row_to_result(_vec_row(), score=0.85)
        assert r is not None
        assert r.score == pytest.approx(0.85)
        assert r.content == "청크 본문"

    def test_empty_source_text_returns_none(self):
        from app.services.retrieval.vector_retriever import VectorRetriever
        assert VectorRetriever._row_to_result(_vec_row(source_text=""), score=0.5) is None
        assert VectorRetriever._row_to_result(_vec_row(source_text=None), score=0.5) is None

    def test_node_id_none_replaced_with_nil_uuid(self):
        from app.services.retrieval.vector_retriever import VectorRetriever
        r = VectorRetriever._row_to_result(_vec_row(node_id=None), score=0.5)
        assert r is not None
        assert r.node_id == uuidlib.UUID(int=0)


@pytest.mark.asyncio
class TestVectorRetrieverRetrieve:
    async def test_embedding_failure_returns_empty(self, monkeypatch):
        from app.services.retrieval.vector_retriever import VectorRetriever

        def _boom():
            raise RuntimeError("embedding down")
        monkeypatch.setattr("app.services.embedding_service.get_embedding_provider", _boom)

        conn, _ = _make_conn()
        r = VectorRetriever(conn)
        results = await r.retrieve(
            query="Q", document_type="policy", top_k=5,
            filters={"actor_role": "VIEWER"},
        )
        assert results == []

    async def test_milvus_unavailable_returns_empty(self, monkeypatch):
        """MILVUS_HOST 미설정 → NullClient. is_available()=False 면 빈 결과."""
        from app.services.retrieval.vector_retriever import VectorRetriever

        fake_provider = MagicMock()
        fake_provider.embed_single = MagicMock(return_value=[0.1] * 10)
        monkeypatch.setattr(
            "app.services.embedding_service.get_embedding_provider",
            lambda: fake_provider,
        )
        monkeypatch.setattr(
            "app.db.milvus.get_milvus",
            lambda: _make_milvus(available=False),
        )

        conn, _ = _make_conn()
        r = VectorRetriever(conn)
        results = await r.retrieve(
            query="Q", document_type="policy", top_k=5,
            filters={"actor_role": "VIEWER"},
        )
        assert results == []

    async def test_milvus_search_failure_returns_empty(self, monkeypatch):
        from app.services.retrieval.vector_retriever import VectorRetriever

        fake_provider = MagicMock()
        fake_provider.embed_single = MagicMock(return_value=[0.1] * 10)
        monkeypatch.setattr(
            "app.services.embedding_service.get_embedding_provider",
            lambda: fake_provider,
        )
        monkeypatch.setattr(
            "app.db.milvus.get_milvus",
            lambda: _make_milvus(search_raises=True),
        )

        conn, _ = _make_conn()
        r = VectorRetriever(conn)
        results = await r.retrieve(
            query="Q", document_type="policy", top_k=5,
            filters={"actor_role": "VIEWER"},
        )
        assert results == []

    async def test_milvus_empty_candidates_returns_empty(self, monkeypatch):
        from app.services.retrieval.vector_retriever import VectorRetriever

        fake_provider = MagicMock()
        fake_provider.embed_single = MagicMock(return_value=[0.1] * 10)
        monkeypatch.setattr(
            "app.services.embedding_service.get_embedding_provider",
            lambda: fake_provider,
        )
        monkeypatch.setattr(
            "app.db.milvus.get_milvus",
            lambda: _make_milvus(candidates=[]),
        )

        conn, _ = _make_conn()
        r = VectorRetriever(conn)
        results = await r.retrieve(
            query="Q", document_type="policy", top_k=5,
            filters={"actor_role": "VIEWER"},
        )
        assert results == []

    async def test_threshold_filters_low_score_candidates(self, monkeypatch):
        """similarity < threshold 후보는 PG 조회 전에 제거된다."""
        from app.services.retrieval.vector_retriever import VectorRetriever

        fake_provider = MagicMock()
        fake_provider.embed_single = MagicMock(return_value=[0.1] * 10)
        monkeypatch.setattr(
            "app.services.embedding_service.get_embedding_provider",
            lambda: fake_provider,
        )
        # 후보 모두 threshold 미만 (0.1 < 0.3)
        monkeypatch.setattr(
            "app.db.milvus.get_milvus",
            lambda: _make_milvus(candidates=[("c1", 0.1), ("c2", 0.2)]),
        )

        conn, cur = _make_conn(fetchall_value=[])
        r = VectorRetriever(conn, similarity_threshold=0.3)
        results = await r.retrieve(
            query="Q", document_type="policy", top_k=5,
            filters={"actor_role": "VIEWER"},
        )
        assert results == []
        # PG 쿼리 자체가 호출되지 않음 (threshold 단계에서 빈 결과)
        assert cur.execute.call_count == 0

    async def test_happy_path_with_acl_and_type(self, monkeypatch):
        from app.services.retrieval.vector_retriever import VectorRetriever

        fake_provider = MagicMock()
        fake_provider.embed_single = MagicMock(return_value=[0.1] * 10)
        monkeypatch.setattr(
            "app.services.embedding_service.get_embedding_provider",
            lambda: fake_provider,
        )
        chunk_id = uuidlib.UUID("aaaaaaaa-0000-0000-0000-000000000001")
        monkeypatch.setattr(
            "app.db.milvus.get_milvus",
            lambda: _make_milvus(candidates=[(str(chunk_id), 0.85)]),
        )

        rows = [_vec_row(chunk_id=chunk_id)]
        conn, cur = _make_conn(fetchall_value=rows)
        r = VectorRetriever(conn, similarity_threshold=0.3)
        results = await r.retrieve(
            query="Q", document_type="policy", top_k=5,
            filters={"actor_role": "VIEWER"},
        )
        assert len(results) == 1
        # Milvus score 가 그대로 score 로
        assert results[0].score == pytest.approx(0.85)

        sql = cur.execute.call_args.args[0]
        # 새 SQL: chunk_id IN (chunk_ids) — pgvector `<=>` 제거 확인
        assert "<=>" not in sql
        assert "::vector" not in sql
        assert "dc.id = ANY(%s::uuid[])" in sql
        # ACL 절 보존
        assert "%s = ANY(dc.accessible_roles)" in sql
        # document_type 필터
        assert "dc.document_type = %s" in sql

    async def test_empty_document_type_skips_filter(self, monkeypatch):
        from app.services.retrieval.vector_retriever import VectorRetriever

        fake_provider = MagicMock()
        fake_provider.embed_single = MagicMock(return_value=[0.1] * 10)
        monkeypatch.setattr(
            "app.services.embedding_service.get_embedding_provider",
            lambda: fake_provider,
        )
        monkeypatch.setattr(
            "app.db.milvus.get_milvus",
            lambda: _make_milvus(candidates=[("c1", 0.5)]),
        )

        conn, cur = _make_conn(fetchall_value=[])
        r = VectorRetriever(conn)
        await r.retrieve(
            query="Q", document_type="", top_k=5, filters={"actor_role": "VIEWER"},
        )
        sql = cur.execute.call_args.args[0]
        assert "dc.document_type = %s" not in sql

    async def test_pg_sql_exception_returns_empty(self, monkeypatch):
        """PG metadata 조회가 실패해도 retrieve 는 빈 결과로 graceful 종료."""
        from app.services.retrieval.vector_retriever import VectorRetriever

        fake_provider = MagicMock()
        fake_provider.embed_single = MagicMock(return_value=[0.1] * 10)
        monkeypatch.setattr(
            "app.services.embedding_service.get_embedding_provider",
            lambda: fake_provider,
        )
        monkeypatch.setattr(
            "app.db.milvus.get_milvus",
            lambda: _make_milvus(candidates=[("c1", 0.85)]),
        )
        conn, _ = _make_conn(raise_on_execute=True)
        r = VectorRetriever(conn)
        results = await r.retrieve(
            query="Q", document_type="", top_k=5, filters={"actor_role": "VIEWER"},
        )
        assert results == []


# =========================================================================== #
# 2) ConversationCompressor
# =========================================================================== #


def _msg(content, role_name="user", turn=1):
    from app.schemas.conversation import ConversationMessage, MessageRole
    role = MessageRole.USER if role_name == "user" else MessageRole.ASSISTANT
    return ConversationMessage(role=role, content=content, turn_number=turn)


class TestConversationCompressorInit:
    def test_defaults(self):
        from app.services.retrieval.conversation_compressor import ConversationCompressor
        c = ConversationCompressor()
        assert c._llm is None
        assert c._prompt_registry is None
        assert c._window_size == 10

    def test_custom_window_size(self):
        from app.services.retrieval.conversation_compressor import ConversationCompressor
        c = ConversationCompressor(window_size=3)
        assert c._window_size == 3


class TestSlidingWindow:
    def test_keeps_last_n_messages(self):
        from app.services.retrieval.conversation_compressor import ConversationCompressor
        c = ConversationCompressor(window_size=3)
        messages = [_msg(f"m{i}", turn=i) for i in range(10)]
        text = c._sliding_window(messages)
        # m7, m8, m9 만 포함 (최근 3개)
        assert "m7" in text and "m8" in text and "m9" in text
        assert "m6" not in text

    def test_role_labels_and_delimiters(self):
        from app.services.retrieval.conversation_compressor import ConversationCompressor
        c = ConversationCompressor(window_size=10)
        text = c._sliding_window([
            _msg("Q1", role_name="user"),
            _msg("A1", role_name="assistant"),
        ])
        assert "대화 이력 시작" in text
        assert "대화 이력 끝" in text
        assert "[사용자]" in text and "[어시스턴트]" in text

    def test_long_message_truncated_to_500(self):
        from app.services.retrieval.conversation_compressor import ConversationCompressor
        c = ConversationCompressor()
        long = "가" * 1000
        text = c._sliding_window([_msg(long)])
        assert "가" * 500 in text
        assert "가" * 501 not in text


@pytest.mark.asyncio
class TestCompressAsync:
    async def test_empty_messages_returns_empty_string(self):
        from app.services.retrieval.conversation_compressor import ConversationCompressor
        c = ConversationCompressor()
        assert await c.compress([]) == ""

    async def test_sliding_window_default_strategy(self):
        from app.services.retrieval.conversation_compressor import ConversationCompressor
        c = ConversationCompressor()
        result = await c.compress([_msg("hi")])
        assert "대화 이력" in result

    async def test_summarize_strategy_without_llm_falls_back_to_sliding(self):
        """llm=None 이면 'summarize' 전략도 sliding 로 폴백."""
        from app.services.retrieval.conversation_compressor import ConversationCompressor
        c = ConversationCompressor(llm=None)
        result = await c.compress([_msg("hi")], strategy="summarize")
        assert "대화 이력" in result

    async def test_summarize_llm_success_returns_summary(self):
        from app.services.retrieval.conversation_compressor import ConversationCompressor
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=("핵심 요약 텍스트", 20))
        c = ConversationCompressor(llm=llm)
        result = await c.compress([_msg("hi")], strategy="summarize")
        assert result == "핵심 요약 텍스트"

    async def test_summarize_llm_empty_response_falls_back(self):
        from app.services.retrieval.conversation_compressor import ConversationCompressor
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=("   ", 0))
        c = ConversationCompressor(llm=llm)
        result = await c.compress([_msg("hi")], strategy="summarize")
        # whitespace 만 → fallback
        assert "대화 이력" in result

    async def test_summarize_llm_exception_falls_back(self):
        from app.services.retrieval.conversation_compressor import ConversationCompressor
        llm = MagicMock()
        llm.complete = AsyncMock(side_effect=RuntimeError("llm fail"))
        c = ConversationCompressor(llm=llm)
        result = await c.compress([_msg("hi")], strategy="summarize")
        assert "대화 이력" in result


class TestLoadSummaryPrompt:
    def test_no_registry_uses_default(self):
        from app.services.retrieval.conversation_compressor import (
            ConversationCompressor, _DEFAULT_SUMMARY_PROMPT,
        )
        c = ConversationCompressor()
        assert c._load_summary_prompt() == _DEFAULT_SUMMARY_PROMPT

    def test_registry_template_used(self):
        from app.services.retrieval.conversation_compressor import ConversationCompressor
        registry = MagicMock()
        registry.get.return_value = "CUSTOM TEMPLATE"
        c = ConversationCompressor(prompt_registry=registry)
        assert c._load_summary_prompt() == "CUSTOM TEMPLATE"

    def test_registry_empty_template_falls_back(self):
        from app.services.retrieval.conversation_compressor import (
            ConversationCompressor, _DEFAULT_SUMMARY_PROMPT,
        )
        registry = MagicMock()
        registry.get.return_value = ""
        c = ConversationCompressor(prompt_registry=registry)
        assert c._load_summary_prompt() == _DEFAULT_SUMMARY_PROMPT

    def test_registry_exception_falls_back(self):
        from app.services.retrieval.conversation_compressor import (
            ConversationCompressor, _DEFAULT_SUMMARY_PROMPT,
        )
        registry = MagicMock()
        registry.get.side_effect = RuntimeError("registry down")
        c = ConversationCompressor(prompt_registry=registry)
        assert c._load_summary_prompt() == _DEFAULT_SUMMARY_PROMPT


# =========================================================================== #
# 3) CitationService
# =========================================================================== #


def _chunk_row(source_text="본문 텍스트", metadata=None):
    return {"source_text": source_text, "metadata": metadata or {}}


class TestCitationServiceVerify:
    def test_hash_matches_returns_verified_true(self):
        from app.services.retrieval.citation_service import CitationService

        text = "본문 텍스트"
        hash_hex = hashlib.sha256(text.encode("utf-8")).hexdigest()
        conn, _ = _make_conn(fetchone_value=_chunk_row(text))
        svc = CitationService(conn)

        resp = svc.verify(
            document_id=DOC_UUID, version_id=VER_UUID, node_id=NODE_UUID,
            content_hash=hash_hex, actor_role="VIEWER",
        )
        assert resp is not None
        assert resp.verified is True
        assert resp.modified is False

    def test_hash_mismatch_returns_modified_true(self):
        """Phase 1 FG 1-3 (skip 복구). content_hash 는 64자 hex 여야 CitationService
        입력 규약과 호환된다. 실 chunk 의 sha256 과 다른 임의 64자 hex 를 넘기면
        verified=False + modified=True 로 검출돼야 한다.
        """
        from app.services.retrieval.citation_service import CitationService

        conn, _ = _make_conn(fetchone_value=_chunk_row("다른 본문"))
        svc = CitationService(conn)
        stale_hash = "deadbeef" * 8  # 8*8 = 64 chars, valid lowercase hex
        assert len(stale_hash) == 64
        resp = svc.verify(
            document_id=DOC_UUID, version_id=VER_UUID, node_id=NODE_UUID,
            content_hash=stale_hash,
            actor_role="VIEWER",
        )
        assert resp is not None
        assert resp.verified is False
        assert resp.modified is True

    def test_chunk_not_found_returns_none(self):
        from app.services.retrieval.citation_service import CitationService

        conn, _ = _make_conn(fetchone_value=None)
        svc = CitationService(conn)
        assert svc.verify(
            document_id=DOC_UUID, version_id=VER_UUID, node_id=NODE_UUID,
            content_hash="x", actor_role="VIEWER",
        ) is None

    def test_sql_error_returns_none(self):
        from app.services.retrieval.citation_service import CitationService

        conn, _ = _make_conn(raise_on_execute=True)
        svc = CitationService(conn)
        assert svc.verify(
            document_id=DOC_UUID, version_id=VER_UUID, node_id=NODE_UUID,
            content_hash="x", actor_role="VIEWER",
        ) is None


class TestCitationServiceGetContent:
    def test_returns_content_and_computes_hash(self):
        from app.services.retrieval.citation_service import CitationService

        text = "청크 본문입니다"
        expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        conn, _ = _make_conn(fetchone_value=_chunk_row(text, metadata={"k": "v"}))
        svc = CitationService(conn)
        resp = svc.get_content(
            document_id=DOC_UUID, version_id=VER_UUID, node_id=NODE_UUID,
            actor_role="VIEWER",
        )
        assert resp is not None
        assert resp.content == text
        assert resp.metadata == {"k": "v"}
        assert resp.citation.content_hash == expected_hash

    def test_not_found_returns_none(self):
        from app.services.retrieval.citation_service import CitationService

        conn, _ = _make_conn(fetchone_value=None)
        svc = CitationService(conn)
        assert svc.get_content(
            document_id=DOC_UUID, version_id=VER_UUID, node_id=NODE_UUID,
        ) is None


class TestCitationServiceFetchChunk:
    def test_nil_node_id_uses_is_null_clause(self):
        from app.services.retrieval.citation_service import CitationService
        from app.services.retrieval.citation_builder import _NIL_NODE_ID

        conn, cur = _make_conn(fetchone_value=_chunk_row())
        svc = CitationService(conn)
        result = svc._fetch_chunk(
            document_id=DOC_UUID, version_id=VER_UUID,
            node_id=_NIL_NODE_ID, actor_role="VIEWER",
        )
        assert result is not None
        sql = cur.execute.call_args.args[0]
        assert "dc.node_id IS NULL" in sql
        assert "dc.node_id = %s::uuid" not in sql

    def test_explicit_node_id_uses_equality(self):
        from app.services.retrieval.citation_service import CitationService

        conn, cur = _make_conn(fetchone_value=_chunk_row())
        svc = CitationService(conn)
        svc._fetch_chunk(
            document_id=DOC_UUID, version_id=VER_UUID,
            node_id=NODE_UUID, actor_role="VIEWER",
        )
        sql = cur.execute.call_args.args[0]
        assert "dc.node_id = %s::uuid" in sql
        # 파라미터에 node_id 문자열 포함
        params = cur.execute.call_args.args[1]
        assert str(NODE_UUID) in params

    def test_acl_clause_applied(self):
        from app.services.retrieval.citation_service import CitationService

        conn, cur = _make_conn(fetchone_value=_chunk_row())
        svc = CitationService(conn)
        svc._fetch_chunk(
            document_id=DOC_UUID, version_id=VER_UUID,
            node_id=NODE_UUID, actor_role="AUTHOR",
        )
        sql = cur.execute.call_args.args[0]
        assert "dc.is_public = TRUE" in sql
        assert "%s = ANY(dc.accessible_roles)" in sql

    def test_none_actor_role_only_public(self):
        from app.services.retrieval.citation_service import CitationService

        conn, cur = _make_conn(fetchone_value=None)
        svc = CitationService(conn)
        svc._fetch_chunk(
            document_id=DOC_UUID, version_id=VER_UUID,
            node_id=NODE_UUID, actor_role=None,
        )
        sql = cur.execute.call_args.args[0]
        assert "dc.is_public = TRUE" in sql
        # actor_role 없음 → ANY(accessible_roles) 조건 없음
        assert "ANY(dc.accessible_roles)" not in sql
