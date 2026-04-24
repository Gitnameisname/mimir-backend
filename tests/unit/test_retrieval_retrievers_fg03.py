"""
S3 Phase 0 / FG 0-3 후속 S6-B — retrieval 패키지 핵심 테스트.

커버:
  - `app.services.retrieval.base`:
      - extract_acl_subjects: filters / access_context / 혼합 / None
      - build_chunk_acl_clause: 빈 filters / 모든 주체 / table_alias
      - Retriever._warn_if_no_acl (구체 구현으로 간접 검증)
  - `app.services.retrieval.query_rewriter`:
      - __init__
      - rewrite_query: 빈 이력 / LLM 실패 폴백 / LLM 빈 응답 / 성공 / PromptRegistry 로드 성공·실패
      - _format_history: 메시지 길이 제한
  - `app.services.retrieval.fts_retriever`:
      - retrieve: document_type 유/무 / SQL 예외 폴백 / _row_to_result (빈 source_text, node_id 유/무)
      - ACL 필터 적용
  - `app.services.retrieval.hybrid_retriever`:
      - _rrf_merge: 겹침 / 없음 / top_k 잘라내기
      - retrieve: 병렬 실행 / 한쪽 실패 / 양쪽 실패
"""
from __future__ import annotations

import uuid as uuidlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


DOC_UUID = uuidlib.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
VER_UUID = uuidlib.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
NODE_UUID = uuidlib.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")


# =========================================================================== #
# 1) base.py 헬퍼
# =========================================================================== #


class TestExtractACLSubjects:
    def test_none_filters_returns_all_none(self):
        from app.services.retrieval.base import extract_acl_subjects

        out = extract_acl_subjects(None)
        assert out == {"actor_role": None, "actor_user_id": None, "organization_id": None}

    def test_filter_direct_keys(self):
        from app.services.retrieval.base import extract_acl_subjects

        out = extract_acl_subjects({
            "actor_role": "VIEWER",
            "actor_user_id": "u1",
            "organization_id": "org-1",
        })
        assert out["actor_role"] == "VIEWER"
        assert out["actor_user_id"] == "u1"
        assert out["organization_id"] == "org-1"

    def test_access_context_fallback(self):
        from app.services.retrieval.base import extract_acl_subjects

        out = extract_acl_subjects({
            "access_context": {
                "actor_role": "AUTHOR",
                "user_id": "u-ctx",
                "organization_id": "org-ctx",
            },
        })
        assert out["actor_role"] == "AUTHOR"
        assert out["actor_user_id"] == "u-ctx"
        assert out["organization_id"] == "org-ctx"

    def test_direct_takes_precedence_over_context(self):
        from app.services.retrieval.base import extract_acl_subjects

        out = extract_acl_subjects({
            "actor_role": "VIEWER",
            "access_context": {"actor_role": "ADMIN"},
        })
        # 직접 필터가 우선
        assert out["actor_role"] == "VIEWER"

    def test_non_dict_access_context_ignored(self):
        from app.services.retrieval.base import extract_acl_subjects

        out = extract_acl_subjects({"access_context": "not-a-dict"})
        assert out["actor_role"] is None


class TestBuildChunkACLClause:
    def test_minimal_only_public(self):
        from app.services.retrieval.base import build_chunk_acl_clause

        clause, params = build_chunk_acl_clause(None)
        assert "dc.is_public = TRUE" in clause
        assert " OR " not in clause     # 단일 절
        assert params == []

    def test_all_subjects_combined(self):
        from app.services.retrieval.base import build_chunk_acl_clause

        clause, params = build_chunk_acl_clause({
            "actor_role": "VIEWER",
            "actor_user_id": "u1",
            "organization_id": "org-1",
        })
        assert clause.count(" OR ") == 3
        assert "%s = ANY(dc.accessible_roles)" in clause
        assert "%s = ANY(dc.accessible_user_ids)" in clause
        assert "%s = ANY(dc.accessible_org_ids)" in clause
        assert params == ["VIEWER", "u1", "org-1"]

    def test_custom_table_alias(self):
        from app.services.retrieval.base import build_chunk_acl_clause

        clause, _ = build_chunk_acl_clause({"actor_role": "X"}, table_alias="c")
        assert "c.is_public = TRUE" in clause
        assert "ANY(c.accessible_roles)" in clause


# =========================================================================== #
# 2) QueryRewriter
# =========================================================================== #


class TestQueryRewriterInit:
    def test_stores_llm_and_registry(self):
        from app.services.retrieval.query_rewriter import QueryRewriter

        llm = MagicMock()
        registry = MagicMock()
        qr = QueryRewriter(llm=llm, prompt_registry=registry)
        assert qr._llm is llm
        assert qr._prompt_registry is registry

    def test_registry_optional(self):
        from app.services.retrieval.query_rewriter import QueryRewriter

        qr = QueryRewriter(llm=MagicMock())
        assert qr._prompt_registry is None


class TestQueryRewriterFormatHistory:
    def test_formats_user_and_assistant_roles(self):
        from app.services.retrieval.query_rewriter import QueryRewriter
        from app.schemas.conversation import ConversationMessage, MessageRole

        messages = [
            ConversationMessage(role=MessageRole.USER, content="Q1", turn_number=1),
            ConversationMessage(role=MessageRole.ASSISTANT, content="A1", turn_number=1),
        ]
        text = QueryRewriter._format_history(messages)
        assert "사용자" in text
        assert "어시스턴트" in text
        assert "Q1" in text and "A1" in text
        assert "이전 대화 이력 시작" in text
        assert "이전 대화 이력 끝" in text

    def test_long_message_truncated(self):
        from app.services.retrieval.query_rewriter import QueryRewriter
        from app.schemas.conversation import ConversationMessage, MessageRole

        long_content = "가" * 1000
        messages = [
            ConversationMessage(role=MessageRole.USER, content=long_content, turn_number=1),
        ]
        text = QueryRewriter._format_history(messages)
        # 500자 제한
        assert "가" * 500 in text
        assert "가" * 501 not in text


@pytest.mark.asyncio
class TestQueryRewriterRewriteQuery:
    async def test_empty_history_returns_original(self):
        from app.services.retrieval.query_rewriter import QueryRewriter

        qr = QueryRewriter(llm=MagicMock())
        result = await qr.rewrite_query("원본 질의", [])
        assert result == "원본 질의"

    async def test_llm_success_returns_rewritten(self):
        from app.services.retrieval.query_rewriter import QueryRewriter
        from app.schemas.conversation import ConversationMessage, MessageRole

        llm = MagicMock()
        llm.complete = AsyncMock(return_value=("재작성된 질의", 10))

        qr = QueryRewriter(llm=llm)
        history = [
            ConversationMessage(role=MessageRole.USER, content="이전", turn_number=1),
        ]
        result = await qr.rewrite_query("Q", history)
        assert result == "재작성된 질의"

    async def test_llm_empty_response_falls_back_to_original(self):
        from app.services.retrieval.query_rewriter import QueryRewriter
        from app.schemas.conversation import ConversationMessage, MessageRole

        llm = MagicMock()
        llm.complete = AsyncMock(return_value=("   ", 0))   # whitespace → 빈 문자열로 간주

        qr = QueryRewriter(llm=llm)
        history = [ConversationMessage(role=MessageRole.USER, content="prev", turn_number=1)]
        result = await qr.rewrite_query("원본", history)
        assert result == "원본"

    async def test_llm_exception_falls_back_to_original(self):
        from app.services.retrieval.query_rewriter import QueryRewriter
        from app.schemas.conversation import ConversationMessage, MessageRole

        llm = MagicMock()
        llm.complete = AsyncMock(side_effect=RuntimeError("llm down"))

        qr = QueryRewriter(llm=llm)
        history = [ConversationMessage(role=MessageRole.USER, content="prev", turn_number=1)]
        result = await qr.rewrite_query("원본", history)
        assert result == "원본"

    async def test_registry_template_used_when_available(self):
        from app.services.retrieval.query_rewriter import QueryRewriter
        from app.schemas.conversation import ConversationMessage, MessageRole

        registry = MagicMock()
        registry.get.return_value = "CUSTOM: {original_query} / {conversation_history}"

        llm = MagicMock()
        llm.complete = AsyncMock(return_value=("rewritten", 5))

        qr = QueryRewriter(llm=llm, prompt_registry=registry)
        history = [ConversationMessage(role=MessageRole.USER, content="prev", turn_number=1)]
        await qr.rewrite_query("Q", history)

        # llm.complete 에 전달된 user content 에 CUSTOM 템플릿 흔적
        kwargs = llm.complete.call_args.kwargs
        user_msg = kwargs["messages"][0]["content"]
        assert "CUSTOM:" in user_msg

    async def test_registry_exception_falls_back_to_default(self):
        from app.services.retrieval.query_rewriter import QueryRewriter
        from app.schemas.conversation import ConversationMessage, MessageRole

        registry = MagicMock()
        registry.get.side_effect = RuntimeError("registry fail")
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=("rewritten", 5))

        qr = QueryRewriter(llm=llm, prompt_registry=registry)
        history = [ConversationMessage(role=MessageRole.USER, content="prev", turn_number=1)]
        # 예외 없이 기본 프롬프트 사용
        result = await qr.rewrite_query("Q", history)
        assert result == "rewritten"


# =========================================================================== #
# 3) FTSRetriever
# =========================================================================== #


def _fts_row(**kw):
    base = {
        "chunk_id": uuidlib.UUID("aaaaaaaa-0000-0000-0000-000000000001"),
        "document_id": DOC_UUID, "version_id": VER_UUID, "node_id": NODE_UUID,
        "source_text": "본문 내용",
        "metadata": {"k": 1},
        "document_type": "policy",
        "document_title": "Doc 1",
        "score": 0.85,
    }
    base.update(kw)
    return base


def _make_conn_cursor(fetchall_value=None, raise_on_execute=False):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    if raise_on_execute:
        cur.execute = MagicMock(side_effect=RuntimeError("SQL error"))
    else:
        cur.execute = MagicMock()
    cur.fetchall = MagicMock(return_value=fetchall_value or [])
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur


class TestFTSRetrieverRowToResult:
    def test_happy_path(self):
        from app.services.retrieval.fts_retriever import FTSRetriever
        result = FTSRetriever._row_to_result(_fts_row())
        assert result is not None
        assert result.document_id == DOC_UUID
        assert result.score == pytest.approx(0.85)
        assert result.content == "본문 내용"

    def test_empty_source_text_returns_none(self):
        from app.services.retrieval.fts_retriever import FTSRetriever
        assert FTSRetriever._row_to_result(_fts_row(source_text="")) is None
        assert FTSRetriever._row_to_result(_fts_row(source_text=None)) is None

    def test_node_id_none_uses_nil_uuid(self):
        from app.services.retrieval.fts_retriever import FTSRetriever
        result = FTSRetriever._row_to_result(_fts_row(node_id=None))
        assert result is not None
        assert result.node_id == uuidlib.UUID(int=0)


@pytest.mark.asyncio
class TestFTSRetrieverRetrieve:
    async def test_happy_path_with_acl_and_type(self):
        from app.services.retrieval.fts_retriever import FTSRetriever

        rows = [_fts_row(), _fts_row(source_text="")]  # 두 번째는 _row_to_result 에서 None 반환
        conn, cur = _make_conn_cursor(fetchall_value=rows)
        r = FTSRetriever(conn)
        results = await r.retrieve(
            query="Q", document_type="policy", top_k=5,
            filters={"actor_role": "VIEWER", "actor_user_id": "u1"},
        )
        assert len(results) == 1   # 빈 source_text 는 필터됨
        sql = cur.execute.call_args.args[0]
        assert "to_tsvector" in sql
        assert "dc.document_type = %s" in sql
        # ACL 절 포함
        assert "%s = ANY(dc.accessible_roles)" in sql

    async def test_empty_document_type_skips_type_filter(self):
        from app.services.retrieval.fts_retriever import FTSRetriever

        conn, cur = _make_conn_cursor(fetchall_value=[])
        r = FTSRetriever(conn)
        await r.retrieve(
            query="Q", document_type="", top_k=5,
            filters={"actor_role": "VIEWER"},
        )
        sql = cur.execute.call_args.args[0]
        assert "dc.document_type = %s" not in sql

    async def test_sql_exception_returns_empty_list(self):
        from app.services.retrieval.fts_retriever import FTSRetriever

        conn, _ = _make_conn_cursor(raise_on_execute=True)
        r = FTSRetriever(conn)
        results = await r.retrieve(
            query="Q", document_type="", top_k=5,
            filters={"actor_role": "VIEWER"},
        )
        assert results == []


# =========================================================================== #
# 4) HybridRetriever
# =========================================================================== #


def _make_result(*, doc="d1", node="n1", score=0.5):
    """Retrieval 결과 하나 — citation 은 MagicMock 로 대체 (구조만 필요)."""
    from app.services.retrieval.base import RetrievalResult

    doc_uuid = uuidlib.UUID("11111111-0000-0000-0000-000000000000") if doc == "d1" else uuidlib.UUID("22222222-0000-0000-0000-000000000000")
    node_uuid = uuidlib.UUID("33333333-0000-0000-0000-000000000000") if node == "n1" else uuidlib.UUID("44444444-0000-0000-0000-000000000000")
    return RetrievalResult(
        document_id=doc_uuid,
        version_id=VER_UUID,
        node_id=node_uuid,
        content=f"{doc}:{node}",
        score=score,
        citation=MagicMock(),
        metadata={},
        document_type="policy",
    )


class TestHybridRetrieverRRFMerge:
    def test_overlap_gets_combined_score(self):
        from app.services.retrieval.hybrid_retriever import HybridRetriever

        fts_res = [_make_result(doc="d1", node="n1", score=0.8)]
        vec_res = [_make_result(doc="d1", node="n1", score=0.9)]

        hr = HybridRetriever(fts=MagicMock(), vector=MagicMock(),
                             fts_weight=0.4, vector_weight=0.6)
        merged = hr._rrf_merge(fts_res, vec_res, top_k=10)
        assert len(merged) == 1
        # 점수는 fts_weight/(60+1) + vec_weight/(60+1) = 1/61
        expected = 0.4 / 61 + 0.6 / 61
        assert merged[0].score == pytest.approx(expected)

    def test_disjoint_results_keep_both(self):
        from app.services.retrieval.hybrid_retriever import HybridRetriever

        fts_res = [_make_result(doc="d1", node="n1", score=0.9)]
        vec_res = [_make_result(doc="d1", node="n2", score=0.8)]   # 다른 node → 다른 키

        hr = HybridRetriever(fts=MagicMock(), vector=MagicMock())
        merged = hr._rrf_merge(fts_res, vec_res, top_k=10)
        assert len(merged) == 2

    def test_top_k_truncates(self):
        from app.services.retrieval.hybrid_retriever import HybridRetriever

        fts_res = [
            _make_result(doc="d1", node=f"n{i}", score=1.0 - i * 0.1)
            for i in range(5)
        ]
        vec_res = [
            _make_result(doc="d2", node=f"n{i}", score=1.0 - i * 0.1)
            for i in range(5)
        ]
        hr = HybridRetriever(fts=MagicMock(), vector=MagicMock())
        merged = hr._rrf_merge(fts_res, vec_res, top_k=3)
        assert len(merged) == 3


@pytest.mark.asyncio
class TestHybridRetrieverRetrieve:
    async def test_both_retrievers_succeed_and_merge(self):
        from app.services.retrieval.hybrid_retriever import HybridRetriever

        fts = MagicMock()
        fts.retrieve = AsyncMock(return_value=[_make_result(doc="d1", node="n1")])
        vec = MagicMock()
        vec.retrieve = AsyncMock(return_value=[_make_result(doc="d2", node="n1")])

        hr = HybridRetriever(fts=fts, vector=vec)
        results = await hr.retrieve(
            query="Q", document_type="policy", top_k=5,
            filters={"actor_role": "VIEWER"},
        )
        assert len(results) == 2
        # 양쪽 retriever 모두 top_k*3 으로 호출됨
        assert fts.retrieve.call_args.args[2] == 15
        assert vec.retrieve.call_args.args[2] == 15

    async def test_fts_failure_continues_with_vector(self):
        from app.services.retrieval.hybrid_retriever import HybridRetriever

        fts = MagicMock()
        fts.retrieve = AsyncMock(side_effect=RuntimeError("fts down"))
        vec = MagicMock()
        vec.retrieve = AsyncMock(return_value=[_make_result(doc="d1", node="n1")])

        hr = HybridRetriever(fts=fts, vector=vec)
        results = await hr.retrieve(
            query="Q", document_type="policy", top_k=5,
            filters={"actor_role": "VIEWER"},
        )
        # vec 결과만으로 1개 반환
        assert len(results) == 1

    async def test_both_failures_return_empty(self):
        from app.services.retrieval.hybrid_retriever import HybridRetriever

        fts = MagicMock()
        fts.retrieve = AsyncMock(side_effect=RuntimeError("boom"))
        vec = MagicMock()
        vec.retrieve = AsyncMock(side_effect=RuntimeError("boom"))

        hr = HybridRetriever(fts=fts, vector=vec)
        results = await hr.retrieve(
            query="Q", document_type="policy", top_k=5,
            filters={"actor_role": "VIEWER"},
        )
        assert results == []

    async def test_custom_weights_affect_score(self):
        """fts_weight / vector_weight 은 최종 score 에 반영된다."""
        from app.services.retrieval.hybrid_retriever import HybridRetriever

        fts = MagicMock()
        fts.retrieve = AsyncMock(return_value=[_make_result(doc="d1", node="n1")])
        vec = MagicMock()
        vec.retrieve = AsyncMock(return_value=[])

        hr = HybridRetriever(fts=fts, vector=vec, fts_weight=1.0, vector_weight=0.0)
        results = await hr.retrieve(
            query="Q", document_type="policy", top_k=5,
            filters={"actor_role": "VIEWER"},
        )
        # fts 만으로 1/61 점수
        assert len(results) == 1
        assert results[0].score == pytest.approx(1.0 / 61)
