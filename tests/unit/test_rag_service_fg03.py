"""FG 0-3 커버리지 보강 — rag_service 유닛 테스트 (세션 9).

대상: `backend/app/services/rag_service.py` (925줄)

커버 범위:
  - QueryProcessor (normalize/embed)
  - Retriever.retrieve (+ _fetch_document_titles)
  - Reranker.rerank (정렬/threshold/top_n)
  - _sanitize_for_context / ContextBuilder.build
  - build_system_prompt (default / plugin / plugin failure)
  - CitationLinker.extract_citations
  - _build_messages / _sse_data
  - invalidate_provider_cache / _get_default_llm_from_db / get_llm_provider
  - MockLLMProvider (complete / complete_stream)
  - OpenAILLMProvider / AnthropicLLMProvider (init + model_name)
  - RAGService.__init__ / _get_llm / prepare_context / stream_answer / query / query_stream
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator
from unittest.mock import MagicMock

import pytest

from app.services import rag_service as rs_mod
from app.services.rag_service import (
    Citation,
    CitationLinker,
    ContextBuilder,
    MockLLMProvider,
    OpenAILLMProvider,
    AnthropicLLMProvider,
    QueryProcessor,
    RAGResponse,
    RAGService,
    Reranker,
    RetrievedChunk,
    Retriever,
    _build_messages,
    _fetch_document_titles,
    _sanitize_for_context,
    _sse_data,
    build_system_prompt,
    get_llm_provider,
    invalidate_provider_cache,
)


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------


def _mk_chunk(
    idx: int = 1,
    *,
    doc_id: str = "doc-1",
    title: str = "Doc Title",
    similarity: float = 0.9,
    text: str | None = None,
    node_id: str | None = "node-1",
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chunk-{idx}",
        document_id=doc_id,
        version_id=f"ver-{idx}",
        node_id=node_id,
        chunk_index=idx,
        source_text=text or f"본문 내용 {idx}",
        node_path=["루트", f"섹션{idx}"],
        document_type="report",
        document_title=title,
        similarity=similarity,
    )


def _make_conn(fetchall_rows=None):
    """documents 타이틀 조회용 간단 conn."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    cur.fetchall = MagicMock(return_value=fetchall_rows or [])
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. QueryProcessor
# ---------------------------------------------------------------------------


def test_query_processor_normalize_strips_whitespace():
    qp = QueryProcessor()
    assert qp.normalize("   hello world   ") == "hello world"


def test_query_processor_embed_delegates_to_provider(monkeypatch):
    qp = QueryProcessor()
    fake_provider = MagicMock()
    fake_provider.embed_single = MagicMock(return_value=[0.1, 0.2, 0.3])
    monkeypatch.setattr(
        "app.services.rag_service.get_embedding_provider",
        lambda: fake_provider,
    )
    assert qp.embed("질문") == [0.1, 0.2, 0.3]
    fake_provider.embed_single.assert_called_once_with("질문")


# ---------------------------------------------------------------------------
# 2. _fetch_document_titles
# ---------------------------------------------------------------------------


def test_fetch_document_titles_empty_returns_empty():
    conn, _ = _make_conn()
    assert _fetch_document_titles(conn, []) == {}


def test_fetch_document_titles_returns_mapping():
    conn, cur = _make_conn(
        fetchall_rows=[
            {"id": "doc-1", "title": "첫번째 문서"},
            {"id": "doc-2", "title": "두번째 문서"},
        ]
    )
    result = _fetch_document_titles(conn, ["doc-1", "doc-2"])
    assert result == {"doc-1": "첫번째 문서", "doc-2": "두번째 문서"}
    assert cur.execute.called


# ---------------------------------------------------------------------------
# 3. Retriever.retrieve
# ---------------------------------------------------------------------------


def test_retriever_retrieve_basic(monkeypatch):
    """semantic_search 결과를 RetrievedChunk 리스트로 변환."""
    raw_chunks = [
        {
            "chunk_id": "c1",
            "document_id": "doc-1",
            "version_id": "v1",
            "node_id": "n1",
            "chunk_index": 0,
            "source_text": "content",
            "node_path": ["a", "b"],
            "document_type": "report",
            "similarity": 0.87,
        }
    ]

    # vectorization_pipeline.semantic_search 모킹
    fake_pipeline = MagicMock()
    fake_pipeline.semantic_search = MagicMock(return_value=raw_chunks)
    # Retriever 가 import 하는 경로 패치
    fake_vs_mod = MagicMock()
    fake_vs_mod.vectorization_pipeline = fake_pipeline
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.services.vectorization_service",
        fake_vs_mod,
    )

    # _fetch_document_titles 패치
    monkeypatch.setattr(
        "app.services.rag_service._fetch_document_titles",
        lambda conn, ids: {"doc-1": "제목"},
    )

    r = Retriever()
    result = r.retrieve(MagicMock(), "질문", actor_role="reader", top_k=10)
    assert len(result) == 1
    assert result[0].chunk_id == "c1"
    assert result[0].document_title == "제목"
    assert result[0].similarity == 0.87
    assert result[0].node_path == ["a", "b"]


def test_retriever_retrieve_filters_by_document_id(monkeypatch):
    raw_chunks = [
        {
            "chunk_id": "c1",
            "document_id": "doc-1",
            "version_id": "v1",
            "node_id": None,
            "chunk_index": 0,
            "source_text": "a",
            "node_path": None,
            "document_type": "report",
            "similarity": 0.9,
        },
        {
            "chunk_id": "c2",
            "document_id": "doc-2",
            "version_id": "v2",
            "node_id": None,
            "chunk_index": 0,
            "source_text": "b",
            "node_path": None,
            "document_type": "report",
            "similarity": 0.8,
        },
    ]
    fake_pipeline = MagicMock()
    fake_pipeline.semantic_search = MagicMock(return_value=raw_chunks)
    fake_vs_mod = MagicMock()
    fake_vs_mod.vectorization_pipeline = fake_pipeline
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.services.vectorization_service",
        fake_vs_mod,
    )
    monkeypatch.setattr(
        "app.services.rag_service._fetch_document_titles",
        lambda conn, ids: {},
    )

    r = Retriever()
    result = r.retrieve(MagicMock(), "q", document_id="doc-1")
    assert len(result) == 1
    assert result[0].document_id == "doc-1"
    # node_path 가 None 인 경우 빈 리스트
    assert result[0].node_path == []


def test_retriever_retrieve_empty(monkeypatch):
    fake_pipeline = MagicMock()
    fake_pipeline.semantic_search = MagicMock(return_value=[])
    fake_vs_mod = MagicMock()
    fake_vs_mod.vectorization_pipeline = fake_pipeline
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.services.vectorization_service",
        fake_vs_mod,
    )
    monkeypatch.setattr(
        "app.services.rag_service._fetch_document_titles",
        lambda conn, ids: {},
    )

    r = Retriever()
    result = r.retrieve(MagicMock(), "질문")
    assert result == []


# ---------------------------------------------------------------------------
# 4. Reranker
# ---------------------------------------------------------------------------


def test_reranker_empty_returns_empty():
    r = Reranker()
    assert r.rerank([]) == []


def test_reranker_sorts_by_similarity_desc():
    r = Reranker()
    chunks = [
        _mk_chunk(1, similarity=0.5),
        _mk_chunk(2, similarity=0.9),
        _mk_chunk(3, similarity=0.7),
    ]
    result = r.rerank(chunks, top_n=5)
    assert [c.similarity for c in result] == [0.9, 0.7, 0.5]


def test_reranker_applies_threshold():
    r = Reranker()
    chunks = [
        _mk_chunk(1, similarity=0.2),
        _mk_chunk(2, similarity=0.8),
        _mk_chunk(3, similarity=0.6),
    ]
    result = r.rerank(chunks, top_n=5, threshold=0.5)
    assert [c.similarity for c in result] == [0.8, 0.6]


def test_reranker_top_n_slice():
    r = Reranker()
    chunks = [
        _mk_chunk(i, similarity=0.9 - i * 0.01) for i in range(1, 6)
    ]
    result = r.rerank(chunks, top_n=2)
    assert len(result) == 2
    assert result[0].similarity > result[1].similarity


def test_reranker_threshold_zero_keeps_all():
    """threshold=0.0 은 필터링 미적용."""
    r = Reranker()
    chunks = [
        _mk_chunk(1, similarity=0.0),
        _mk_chunk(2, similarity=0.5),
    ]
    result = r.rerank(chunks, top_n=5, threshold=0.0)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# 5. _sanitize_for_context
# ---------------------------------------------------------------------------


def test_sanitize_escapes_document_context_tags():
    text = "앞 <document_context> 가짜 </document_context> 뒤"
    result = _sanitize_for_context(text)
    assert "<document_context>" not in result
    assert "</document_context>" not in result
    assert "&lt;document_context&gt;" in result
    assert "&lt;/document_context&gt;" in result


def test_sanitize_preserves_normal_text():
    text = "평범한 한국어 문장입니다."
    assert _sanitize_for_context(text) == text


# ---------------------------------------------------------------------------
# 6. ContextBuilder
# ---------------------------------------------------------------------------


def test_context_builder_empty_chunks():
    cb = ContextBuilder()
    context, included = cb.build([])
    assert included == []
    # wrap 로 감싼 빈 raw_context 이므로 wrap 결과가 존재할 수 있음
    assert isinstance(context, str)


def test_context_builder_includes_within_limit():
    cb = ContextBuilder()
    chunks = [_mk_chunk(1, text="짧은 본문"), _mk_chunk(2, text="또 다른 본문")]
    context, included = cb.build(chunks, max_tokens=6000)
    assert len(included) == 2
    # 각 청크에 [1], [2] 번호 부착
    assert "[1]" in context
    assert "[2]" in context
    assert "Doc Title" in context


def test_context_builder_truncates_over_limit():
    cb = ContextBuilder()
    long_text = "가" * 5000  # 5000자
    chunks = [
        _mk_chunk(1, text=long_text),
        _mk_chunk(2, text=long_text),
        _mk_chunk(3, text=long_text),
    ]
    # max_tokens=1000 → 4000 char 상한
    context, included = cb.build(chunks, max_tokens=1000)
    # 첫 청크만 허용되어야 하며, 그마저 5000 > 4000 이므로 포함 안됨
    assert len(included) <= 1


def test_context_builder_doc_label_fallback_uses_id_prefix():
    cb = ContextBuilder()
    # title 이 없는 chunk → document_id[:8] 사용
    chunks = [
        RetrievedChunk(
            chunk_id="c1",
            document_id="abcdefghijklmnop",
            version_id="v1",
            node_id=None,
            chunk_index=0,
            source_text="body",
            node_path=[],
            document_type="report",
            document_title=None,
            similarity=0.5,
        )
    ]
    context, _ = cb.build(chunks)
    assert "abcdefgh" in context


# ---------------------------------------------------------------------------
# 7. build_system_prompt
# ---------------------------------------------------------------------------


def test_build_system_prompt_default_template():
    result = build_system_prompt("테스트 컨텍스트")
    assert "테스트 컨텍스트" in result
    assert "[숫자] 형식" in result


def test_build_system_prompt_with_plugin_success(monkeypatch):
    fake_template = MagicMock()
    fake_template.render = MagicMock(return_value="플러그인 프롬프트")
    fake_plugin_rag = MagicMock()
    fake_plugin_rag.get_prompt_template = MagicMock(return_value=fake_template)
    fake_plugin = MagicMock()
    fake_plugin.rag_plugin = MagicMock(return_value=fake_plugin_rag)
    fake_registry_instance = MagicMock()
    fake_registry_instance.get = MagicMock(return_value=fake_plugin)
    fake_registry_cls = MagicMock()
    fake_registry_cls.instance = MagicMock(return_value=fake_registry_instance)

    fake_plugins_base = MagicMock()
    fake_plugins_base.DocumentTypeRegistry = fake_registry_cls
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.plugins.base",
        fake_plugins_base,
    )

    result = build_system_prompt("ctx", document_type="report")
    assert result == "플러그인 프롬프트"
    fake_template.render.assert_called_once_with("ctx")


def test_build_system_prompt_plugin_failure_falls_back(monkeypatch):
    fake_registry_cls = MagicMock()
    fake_registry_cls.instance = MagicMock(side_effect=RuntimeError("no plugin"))
    fake_plugins_base = MagicMock()
    fake_plugins_base.DocumentTypeRegistry = fake_registry_cls
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.plugins.base",
        fake_plugins_base,
    )

    result = build_system_prompt("폴백 컨텍스트", document_type="unknown")
    assert "폴백 컨텍스트" in result


# ---------------------------------------------------------------------------
# 8. CitationLinker
# ---------------------------------------------------------------------------


def test_citation_linker_no_markers_returns_empty():
    cl = CitationLinker()
    chunks = [_mk_chunk(1), _mk_chunk(2)]
    assert cl.extract_citations("마커 없는 답변", chunks) == []


def test_citation_linker_extracts_single_citation():
    cl = CitationLinker()
    chunks = [_mk_chunk(1, title="문서 A")]
    answer = "이것은 [1] 답변입니다"
    cits = cl.extract_citations(answer, chunks)
    assert len(cits) == 1
    assert cits[0].index == 1
    assert cits[0].document_title == "문서 A"
    # source_text 는 300자 요약
    assert len(cits[0].source_text) <= 300


def test_citation_linker_filters_out_of_range():
    cl = CitationLinker()
    chunks = [_mk_chunk(1)]
    answer = "[1] 과 [5] 는 유효하지 않음"
    cits = cl.extract_citations(answer, chunks)
    # [1] 만 유효, [5] 는 범위 밖
    assert len(cits) == 1
    assert cits[0].index == 1


def test_citation_linker_deduplicates_and_sorts():
    cl = CitationLinker()
    chunks = [_mk_chunk(1), _mk_chunk(2), _mk_chunk(3)]
    answer = "[3] 과 [1] 그리고 [1] 다시 [2]"
    cits = cl.extract_citations(answer, chunks)
    # 중복 제거 + 오름차순
    assert [c.index for c in cits] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 9. _build_messages / _sse_data
# ---------------------------------------------------------------------------


def test_build_messages_empty_history():
    result = _build_messages("질문입니다", [])
    assert result == [{"role": "user", "content": "질문입니다"}]


def test_build_messages_appends_to_history():
    history = [
        {"role": "user", "content": "이전"},
        {"role": "assistant", "content": "답변"},
    ]
    result = _build_messages("새 질문", history)
    assert len(result) == 3
    assert result[-1] == {"role": "user", "content": "새 질문"}
    # history 원본 보존 (mutation 방지)
    assert len(history) == 2


def test_sse_data_format():
    payload = {"event": "start", "data": {"x": 1}}
    out = _sse_data(payload)
    assert out.startswith("data: ")
    assert out.endswith("\n\n")
    # JSON 직렬화 확인
    json_part = out[len("data: "):-2]
    parsed = json.loads(json_part)
    assert parsed == payload


def test_sse_data_handles_korean_without_ascii_escape():
    payload = {"event": "delta", "data": {"text": "한글"}}
    out = _sse_data(payload)
    assert "한글" in out  # ensure_ascii=False


# ---------------------------------------------------------------------------
# 10. invalidate_provider_cache / _get_default_llm_from_db
# ---------------------------------------------------------------------------


def test_invalidate_provider_cache_clears():
    rs_mod._provider_cache["llm"] = {"data": {"x": 1}, "expires": 9e9}
    invalidate_provider_cache()
    assert rs_mod._provider_cache == {}


def test_get_default_llm_from_db_returns_cached_on_hit():
    # 캐시 히트
    rs_mod._provider_cache["llm"] = {
        "data": {"model_name": "cached-model"},
        "expires": 9e9,
    }
    result = rs_mod._get_default_llm_from_db()
    assert result == {"model_name": "cached-model"}
    invalidate_provider_cache()


def test_get_default_llm_from_db_exception_returns_none(monkeypatch):
    invalidate_provider_cache()
    # get_db import 자체를 실패하게
    fake_connection_mod = MagicMock()
    fake_connection_mod.get_db = MagicMock(side_effect=RuntimeError("DB down"))
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.db.connection",
        fake_connection_mod,
    )
    assert rs_mod._get_default_llm_from_db() is None


def test_get_default_llm_from_db_queries_when_cache_miss(monkeypatch):
    invalidate_provider_cache()

    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    cur.fetchone = MagicMock(return_value={"model_name": "gpt-4o", "api_key": "k"})
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    fake_ctx = MagicMock()
    fake_ctx.__enter__ = MagicMock(return_value=conn)
    fake_ctx.__exit__ = MagicMock(return_value=False)

    fake_connection_mod = MagicMock()
    fake_connection_mod.get_db = MagicMock(return_value=fake_ctx)
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.db.connection",
        fake_connection_mod,
    )

    result = rs_mod._get_default_llm_from_db()
    assert result is not None
    assert result["model_name"] == "gpt-4o"
    invalidate_provider_cache()


# ---------------------------------------------------------------------------
# 11. get_llm_provider
# ---------------------------------------------------------------------------


def test_get_llm_provider_db_openai_compatible(monkeypatch):
    """DB 에 base_url 있으면 OpenAI 호환 클라이언트."""
    monkeypatch.setattr(
        "app.services.rag_service._get_default_llm_from_db",
        lambda: {
            "model_name": "qwen-7b",
            "api_key": "k",
            "api_base_url": "http://local:8000",
        },
    )
    p = get_llm_provider()
    assert isinstance(p, OpenAILLMProvider)
    assert p.model_name == "qwen-7b"


def test_get_llm_provider_db_claude_model(monkeypatch):
    monkeypatch.setattr(
        "app.services.rag_service._get_default_llm_from_db",
        lambda: {
            "model_name": "claude-sonnet-4-6",
            "api_key": "sk",
            "api_base_url": None,
        },
    )
    p = get_llm_provider()
    assert isinstance(p, AnthropicLLMProvider)


def test_get_llm_provider_db_plain_model_without_base_url(monkeypatch):
    """base_url=None + claude 접두사 아님 → OpenAI 경로."""
    monkeypatch.setattr(
        "app.services.rag_service._get_default_llm_from_db",
        lambda: {
            "model_name": "gpt-4o",
            "api_key": "k",
            "api_base_url": None,
        },
    )
    p = get_llm_provider()
    assert isinstance(p, OpenAILLMProvider)


def test_get_llm_provider_env_anthropic(monkeypatch):
    monkeypatch.setattr(
        "app.services.rag_service._get_default_llm_from_db",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.llm_provider",
        "anthropic",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.anthropic_api_key",
        "sk-ant",
        raising=False,
    )
    p = get_llm_provider()
    assert isinstance(p, AnthropicLLMProvider)


def test_get_llm_provider_env_openai(monkeypatch):
    monkeypatch.setattr(
        "app.services.rag_service._get_default_llm_from_db",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.llm_provider",
        "openai",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.openai_api_key",
        "sk-oai",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.anthropic_api_key",
        None,
        raising=False,
    )
    p = get_llm_provider()
    assert isinstance(p, OpenAILLMProvider)


def test_get_llm_provider_no_keys_returns_mock(monkeypatch):
    monkeypatch.setattr(
        "app.services.rag_service._get_default_llm_from_db",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.llm_provider",
        "openai",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.openai_api_key",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.anthropic_api_key",
        None,
        raising=False,
    )
    p = get_llm_provider()
    assert isinstance(p, MockLLMProvider)


# ---------------------------------------------------------------------------
# 12. MockLLMProvider
# ---------------------------------------------------------------------------


def test_mock_provider_model_name():
    assert MockLLMProvider().model_name == "mock-llm"


@pytest.mark.asyncio
async def test_mock_provider_complete_returns_fallback():
    content, token = await MockLLMProvider().complete("sys", [])
    assert "LLM API 키" in content
    assert token == 0


@pytest.mark.asyncio
async def test_mock_provider_complete_stream_yields_fallback():
    chunks = []
    async for t in MockLLMProvider().complete_stream("sys", []):
        chunks.append(t)
    full = "".join(chunks)
    assert "LLM API 키" in full


# ---------------------------------------------------------------------------
# 13. OpenAILLMProvider / AnthropicLLMProvider — init + model_name
# ---------------------------------------------------------------------------


def test_openai_provider_init_with_explicit_args():
    p = OpenAILLMProvider(api_key="k", model="gpt-4o-mini", base_url="http://x")
    assert p.model_name == "gpt-4o-mini"
    assert p._api_key == "k"
    assert p._base_url == "http://x"


def test_openai_provider_init_uses_settings_fallback(monkeypatch):
    monkeypatch.setattr(
        "app.services.rag_service.settings.openai_api_key", None, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.llm_model", "gpt-4o", raising=False
    )
    p = OpenAILLMProvider()
    assert p._api_key == "dummy"
    assert p.model_name == "gpt-4o"


def test_anthropic_provider_init_with_args():
    p = AnthropicLLMProvider(api_key="k", model="claude-sonnet-4-6")
    assert p.model_name == "claude-sonnet-4-6"


def test_anthropic_provider_init_defaults(monkeypatch):
    monkeypatch.setattr(
        "app.services.rag_service.settings.anthropic_api_key", "sk", raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.llm_model", None, raising=False
    )
    p = AnthropicLLMProvider()
    assert p.model_name == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# 14. RAGService — __init__ / _get_llm
# ---------------------------------------------------------------------------


def test_rag_service_init_defaults():
    svc = RAGService()
    assert svc._llm is None
    assert isinstance(svc._qp, QueryProcessor)
    assert isinstance(svc._retriever, Retriever)
    assert isinstance(svc._reranker, Reranker)
    assert isinstance(svc._cb, ContextBuilder)
    assert isinstance(svc._cl, CitationLinker)


def test_rag_service_init_with_injected_components():
    fake_llm = MagicMock(spec=MockLLMProvider)
    fake_qp = MagicMock(spec=QueryProcessor)
    svc = RAGService(llm_provider=fake_llm, query_processor=fake_qp)
    assert svc._llm is fake_llm
    assert svc._qp is fake_qp


def test_rag_service_get_llm_lazy_loads(monkeypatch):
    svc = RAGService()
    fake = MagicMock()
    monkeypatch.setattr(
        "app.services.rag_service.get_llm_provider",
        lambda: fake,
    )
    assert svc._get_llm() is fake
    # 두 번째 호출은 캐싱됨
    assert svc._get_llm() is fake


def test_rag_service_get_llm_returns_injected():
    fake = MagicMock()
    svc = RAGService(llm_provider=fake)
    assert svc._get_llm() is fake


# ---------------------------------------------------------------------------
# 15. RAGService.prepare_context
# ---------------------------------------------------------------------------


def test_rag_service_prepare_context_basic_flow(monkeypatch):
    chunks = [_mk_chunk(1, similarity=0.9), _mk_chunk(2, similarity=0.8)]
    fake_retriever = MagicMock()
    fake_retriever.retrieve = MagicMock(return_value=chunks)
    fake_reranker = MagicMock()
    fake_reranker.rerank = MagicMock(return_value=chunks[:1])
    svc = RAGService(retriever=fake_retriever, reranker=fake_reranker)

    # reranker_enabled 설정
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_reranker_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_reranker_threshold",
        0.0,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_k", 20, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_n", 5, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_max_context_tokens",
        6000,
        raising=False,
    )

    ctx, included, messages = svc.prepare_context(MagicMock(), "  질문  ")

    assert "[1]" in ctx
    assert len(included) == 1
    assert messages[-1] == {"role": "user", "content": "질문"}
    # retriever 호출 인자 확인
    fake_retriever.retrieve.assert_called_once()


def test_rag_service_prepare_context_reranker_disabled(monkeypatch):
    chunks = [_mk_chunk(i, similarity=0.5) for i in range(1, 11)]
    fake_retriever = MagicMock()
    fake_retriever.retrieve = MagicMock(return_value=chunks)
    fake_reranker = MagicMock()
    fake_reranker.rerank = MagicMock(return_value=[])  # 호출되면 안 됨
    svc = RAGService(retriever=fake_retriever, reranker=fake_reranker)

    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_reranker_enabled",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_k", 20, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_n", 3, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_max_context_tokens",
        6000,
        raising=False,
    )

    ctx, included, _ = svc.prepare_context(MagicMock(), "q")
    fake_reranker.rerank.assert_not_called()
    # top_n=3 슬라이스만 적용
    assert len(included) == 3


def test_rag_service_prepare_context_plugin_config_override(monkeypatch):
    chunks = [_mk_chunk(1)]
    fake_retriever = MagicMock()
    fake_retriever.retrieve = MagicMock(return_value=chunks)
    svc = RAGService(retriever=fake_retriever)

    fake_rag_plugin = MagicMock()
    fake_rag_plugin.get_context_config = MagicMock(
        return_value={"max_context_tokens": 2000, "top_n": 2}
    )
    fake_plugin = MagicMock()
    fake_plugin.rag_plugin = MagicMock(return_value=fake_rag_plugin)
    fake_registry = MagicMock()
    fake_registry.get = MagicMock(return_value=fake_plugin)
    fake_reg_cls = MagicMock()
    fake_reg_cls.instance = MagicMock(return_value=fake_registry)
    fake_plugins_base = MagicMock()
    fake_plugins_base.DocumentTypeRegistry = fake_reg_cls
    monkeypatch.setitem(
        __import__("sys").modules, "app.plugins.base", fake_plugins_base
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_reranker_enabled",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_k", 20, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_n", 5, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_max_context_tokens",
        6000,
        raising=False,
    )

    svc.prepare_context(MagicMock(), "q", document_type="report")
    # plugin 의 context_config 가 실제로 조회됨
    fake_rag_plugin.get_context_config.assert_called_once()


def test_rag_service_prepare_context_plugin_failure_falls_back(monkeypatch):
    """플러그인 조회 예외 시 기본값 사용."""
    chunks = [_mk_chunk(1)]
    fake_retriever = MagicMock()
    fake_retriever.retrieve = MagicMock(return_value=chunks)
    svc = RAGService(retriever=fake_retriever)

    fake_reg_cls = MagicMock()
    fake_reg_cls.instance = MagicMock(side_effect=RuntimeError("no plugin"))
    fake_plugins_base = MagicMock()
    fake_plugins_base.DocumentTypeRegistry = fake_reg_cls
    monkeypatch.setitem(
        __import__("sys").modules, "app.plugins.base", fake_plugins_base
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_reranker_enabled",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_k", 20, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_n", 5, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_max_context_tokens",
        6000,
        raising=False,
    )

    ctx, included, _ = svc.prepare_context(
        MagicMock(), "q", document_type="unknown"
    )
    assert len(included) == 1


# ---------------------------------------------------------------------------
# 16. RAGService.stream_answer (async SSE)
# ---------------------------------------------------------------------------


class _FakeStreamLLM:
    def __init__(self, tokens=("안녕", "[1]", " 답변"), name="fake-llm"):
        self._tokens = tokens
        self._name = name

    @property
    def model_name(self):
        return self._name

    async def complete_stream(
        self, system_prompt: str, messages: list
    ) -> AsyncGenerator[str, None]:
        for t in self._tokens:
            yield t

    async def complete(self, system_prompt, messages):
        return "".join(self._tokens), 42


@pytest.mark.asyncio
async def test_rag_service_stream_answer_emits_sse_events():
    fake_llm = _FakeStreamLLM(tokens=("대답", "[1]"))
    svc = RAGService(llm_provider=fake_llm)
    included = [_mk_chunk(1)]
    events = []
    async for payload in svc.stream_answer(
        "ctx",
        included,
        [{"role": "user", "content": "질문"}],
        conversation_id="conv-1",
    ):
        events.append(payload)

    assert any('"event": "start"' in e for e in events)
    assert any('"event": "delta"' in e for e in events)
    assert any('"event": "citation"' in e for e in events)
    assert any('"event": "done"' in e for e in events)


@pytest.mark.asyncio
async def test_rag_service_stream_answer_error_emits_error_event(monkeypatch):
    class _BrokenLLM:
        @property
        def model_name(self):
            return "broken"

        async def complete_stream(self, sp, m):
            raise RuntimeError("boom")
            yield  # pragma: no cover

        async def complete(self, sp, m):
            raise RuntimeError("boom")

    svc = RAGService(llm_provider=_BrokenLLM())
    events = []
    async for payload in svc.stream_answer(
        "ctx", [_mk_chunk(1)], [], conversation_id="c",
    ):
        events.append(payload)

    # 에러 이벤트 포함
    joined = "".join(events)
    assert "error" in joined
    # 내부 예외 메시지는 노출되지 않아야 함
    assert "boom" not in joined


# ---------------------------------------------------------------------------
# 17. RAGService.query (단건)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rag_service_query_full_flow(monkeypatch):
    chunks = [_mk_chunk(1, similarity=0.9)]
    fake_retriever = MagicMock()
    fake_retriever.retrieve = MagicMock(return_value=chunks)
    fake_llm = _FakeStreamLLM(tokens=("답변", "[1]"))
    svc = RAGService(llm_provider=fake_llm, retriever=fake_retriever)

    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_reranker_enabled",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_k", 20, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_n", 5, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_max_context_tokens",
        6000,
        raising=False,
    )

    resp = await svc.query(
        MagicMock(),
        "  질문  ",
        conversation_id="conv-1",
    )
    assert isinstance(resp, RAGResponse)
    assert resp.answer == "답변[1]"
    assert resp.token_used == 42
    assert resp.conversation_id == "conv-1"
    assert resp.model == "fake-llm"
    assert len(resp.citations) == 1
    assert resp.citations[0].index == 1


@pytest.mark.asyncio
async def test_rag_service_query_with_reranker_enabled(monkeypatch):
    chunks = [_mk_chunk(i, similarity=0.9 - i * 0.01) for i in range(1, 6)]
    fake_retriever = MagicMock()
    fake_retriever.retrieve = MagicMock(return_value=chunks)
    fake_reranker = MagicMock()
    fake_reranker.rerank = MagicMock(return_value=chunks[:2])
    fake_llm = _FakeStreamLLM(tokens=("ok",))
    svc = RAGService(
        llm_provider=fake_llm,
        retriever=fake_retriever,
        reranker=fake_reranker,
    )

    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_reranker_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_reranker_threshold",
        0.0,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_k", 20, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_n", 2, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_max_context_tokens",
        6000,
        raising=False,
    )

    resp = await svc.query(MagicMock(), "q", conversation_id="c")
    fake_reranker.rerank.assert_called_once()
    assert len(resp.context_chunks) == 2


# ---------------------------------------------------------------------------
# 18. RAGService.query_stream (SSE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rag_service_query_stream_full(monkeypatch):
    chunks = [_mk_chunk(1)]
    fake_retriever = MagicMock()
    fake_retriever.retrieve = MagicMock(return_value=chunks)
    fake_llm = _FakeStreamLLM(tokens=("ans", "[1]"))
    svc = RAGService(llm_provider=fake_llm, retriever=fake_retriever)

    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_reranker_enabled",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_k", 20, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_n", 5, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_max_context_tokens",
        6000,
        raising=False,
    )

    events = []
    async for p in svc.query_stream(
        MagicMock(), "  질문  ", conversation_id="c-1"
    ):
        events.append(p)

    joined = "".join(events)
    assert '"event": "start"' in joined
    assert '"event": "delta"' in joined
    assert '"event": "citation"' in joined
    assert '"event": "done"' in joined


@pytest.mark.asyncio
async def test_rag_service_query_stream_retriever_failure(monkeypatch):
    """retriever 예외 시 error 이벤트 + 내부 메시지 비노출."""
    fake_retriever = MagicMock()
    fake_retriever.retrieve = MagicMock(side_effect=RuntimeError("db down"))
    svc = RAGService(
        llm_provider=MockLLMProvider(), retriever=fake_retriever
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_k", 20, raising=False
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.rag_top_n", 5, raising=False
    )

    events = []
    async for p in svc.query_stream(MagicMock(), "q", conversation_id="c"):
        events.append(p)
    joined = "".join(events)
    assert "error" in joined
    assert "db down" not in joined
