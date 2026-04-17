"""
RAG (Retrieval-Augmented Generation) 서비스.

Phase 11: 문서 기반 자연어 질의응답 파이프라인.

아키텍처:
  QueryProcessor → Retriever → Reranker → ContextBuilder
      → LLMProvider → CitationLinker → RAGResponse

설계 원칙:
  - LLMProvider 추상화: OpenAI / Anthropic 교체 가능
  - 권한 필터: Retriever 단계에서 actor_role 적용
  - SSE 스트리밍: 응답 토큰을 AsyncGenerator로 yield
  - Citation: LLM 응답의 [1],[2] 마커를 원본 청크와 매핑
  - Phase 12 대비: PromptTemplate 추상화 준비
"""

import json
import logging
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional

import psycopg2.extensions
import psycopg2.extras

from app.config import settings
from app.security.prompt_injection import content_directive_separator
from app.services.embedding_service import get_embedding_provider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 도메인 데이터 클래스
# ---------------------------------------------------------------------------

@dataclass
class RetrievedChunk:
    chunk_id: str
    document_id: str
    version_id: str
    node_id: Optional[str]
    chunk_index: int
    source_text: str
    node_path: list[str]
    document_type: str
    document_title: Optional[str] = None
    similarity: float = 0.0


@dataclass
class Citation:
    index: int
    chunk_id: str
    document_id: str
    document_title: Optional[str]
    node_id: Optional[str]
    node_path: list[str]
    source_text: str
    similarity: float = 0.0


@dataclass
class RAGResponse:
    answer: str
    citations: list[Citation]
    context_chunks: list[RetrievedChunk]
    conversation_id: str
    message_id: str
    model: str
    token_used: int = 0


# ---------------------------------------------------------------------------
# QueryProcessor
# ---------------------------------------------------------------------------

class QueryProcessor:
    """질문 정규화 및 임베딩 변환."""

    def normalize(self, question: str) -> str:
        """질문 텍스트 정규화 (불필요한 공백 제거 등)."""
        return question.strip()

    def embed(self, question: str) -> list[float]:
        """질문을 임베딩 벡터로 변환."""
        provider = get_embedding_provider()
        return provider.embed_single(question)


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------

class Retriever:
    """Phase 10 VectorSearchProvider 연계 Retriever.

    hybrid 전략: FTS 점수 + 벡터 유사도 RRF로 청크를 반환.
    권한 필터: actor_role에 따라 접근 가능한 청크만 반환.
    """

    def retrieve(
        self,
        conn: psycopg2.extensions.connection,
        question: str,
        *,
        actor_role: Optional[str] = None,
        document_id: Optional[str] = None,
        document_type: Optional[str] = None,
        top_k: int = 20,
    ) -> list[RetrievedChunk]:
        """문서 청크를 검색해 RetrievedChunk 목록으로 반환."""
        from app.services.vectorization_service import vectorization_pipeline

        # 벡터 검색
        raw_chunks = vectorization_pipeline.semantic_search(
            conn,
            query=question,
            actor_role=actor_role,
            document_type=document_type,
            top_k=top_k,
        )

        # document_id 필터 (특정 문서 범위 제한)
        if document_id:
            raw_chunks = [c for c in raw_chunks if c["document_id"] == document_id]

        # 문서 타이틀 조회 (배치)
        doc_ids = list({c["document_id"] for c in raw_chunks})
        doc_titles = _fetch_document_titles(conn, doc_ids)

        chunks = []
        for c in raw_chunks:
            chunks.append(RetrievedChunk(
                chunk_id=c["chunk_id"],
                document_id=c["document_id"],
                version_id=c["version_id"],
                node_id=c.get("node_id"),
                chunk_index=c["chunk_index"],
                source_text=c["source_text"],
                node_path=c.get("node_path") or [],
                document_type=c["document_type"],
                document_title=doc_titles.get(c["document_id"]),
                similarity=c["similarity"],
            ))

        return chunks


def _fetch_document_titles(
    conn: psycopg2.extensions.connection,
    doc_ids: list[str],
) -> dict[str, str]:
    """document_id → title 매핑 조회."""
    if not doc_ids:
        return {}
    placeholders = ",".join(["%s::uuid"] * len(doc_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, title FROM documents WHERE id IN ({placeholders})",
            doc_ids,
        )
        return {str(row["id"]): row["title"] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Reranker
# ---------------------------------------------------------------------------

class Reranker:
    """Score-threshold 기반 재랭커.

    cross-encoder 모델 없이 유사도 점수만으로 필터링.
    Phase 12에서 cross-encoder 모델 교체 가능하도록 추상화 준비.
    """

    def rerank(
        self,
        chunks: list[RetrievedChunk],
        *,
        top_n: int = 5,
        threshold: float = 0.0,
    ) -> list[RetrievedChunk]:
        """유사도 점수 기준 재정렬 후 Top-N 반환."""
        if not chunks:
            return []

        # 유사도 내림차순 정렬
        sorted_chunks = sorted(chunks, key=lambda c: c.similarity, reverse=True)

        # 점수 임계값 필터 (설정된 경우)
        if threshold > 0.0:
            sorted_chunks = [c for c in sorted_chunks if c.similarity >= threshold]

        return sorted_chunks[:top_n]


# ---------------------------------------------------------------------------
# ContextBuilder
# ---------------------------------------------------------------------------

def _sanitize_for_context(text: str) -> str:
    """청크 텍스트에서 프롬프트 구분자 XML 태그를 이스케이프한다.

    RAG-004: 문서 내용이 <document_context> 태그를 포함해 시스템 프롬프트
    구조를 오염시키는 프롬프트 인젝션을 방지한다.
    """
    text = text.replace("<document_context>", "&lt;document_context&gt;")
    text = text.replace("</document_context>", "&lt;/document_context&gt;")
    return text


class ContextBuilder:
    """청크 → LLM 프롬프트 컨텍스트 조합.

    토큰 한도(max_tokens)를 넘지 않도록 청크를 순차적으로 포함.
    출처 번호([1], [2])를 각 청크 앞에 부착.
    """

    def build(
        self,
        chunks: list[RetrievedChunk],
        *,
        max_tokens: int = 6000,
    ) -> tuple[str, list[RetrievedChunk]]:
        """컨텍스트 문자열과 실제 포함된 청크 목록을 반환."""
        context_parts: list[str] = []
        included: list[RetrievedChunk] = []
        total_chars = 0
        # 대략 1토큰 ≈ 4자 기준
        max_chars = max_tokens * 4

        for i, chunk in enumerate(chunks, start=1):
            # RAG-004: 구분자 태그 이스케이프
            text = _sanitize_for_context(chunk.source_text.strip())
            doc_label = chunk.document_title or chunk.document_id[:8]
            part = f"[{i}] (문서: {doc_label})\n{text}"
            if total_chars + len(part) > max_chars:
                break
            context_parts.append(part)
            included.append(chunk)
            total_chars += len(part)

        raw_context = "\n\n".join(context_parts)
        # OWASP LLM01: 검색 결과를 신뢰할 수 없는 컨텐츠(untrusted)로 격리
        context = content_directive_separator.wrap(raw_context)
        return context, included


# ---------------------------------------------------------------------------
# LLMProvider 추상화
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """LLM 모델 추상화 인터페이스."""

    @abstractmethod
    async def complete_stream(
        self,
        system_prompt: str,
        messages: list[dict],
    ) -> AsyncGenerator[str, None]:
        """스트리밍 응답 생성 — 토큰 단위 yield."""
        ...

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        messages: list[dict],
    ) -> tuple[str, int]:
        """단건 응답 생성 — (content, token_used) 반환."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        ...


class OpenAILLMProvider(LLMProvider):
    """OpenAI GPT-4o 연동."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key or settings.openai_api_key
        self._model = model or settings.llm_model
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(api_key=self._api_key)
            except ImportError:
                raise RuntimeError("openai 패키지가 설치되지 않았습니다.")
        return self._client

    @property
    def model_name(self) -> str:
        return self._model

    async def complete_stream(
        self,
        system_prompt: str,
        messages: list[dict],
    ) -> AsyncGenerator[str, None]:
        client = self._get_client()
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        stream = await client.chat.completions.create(
            model=self._model,
            messages=full_messages,
            stream=True,
            temperature=0.2,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict],
    ) -> tuple[str, int]:
        client = self._get_client()
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        response = await client.chat.completions.create(
            model=self._model,
            messages=full_messages,
            stream=False,
            temperature=0.2,
        )
        content = response.choices[0].message.content or ""
        token_used = response.usage.total_tokens if response.usage else 0
        return content, token_used


class AnthropicLLMProvider(LLMProvider):
    """Anthropic Claude claude-sonnet-4-6 연동."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key or settings.anthropic_api_key
        self._model = model or "claude-sonnet-4-6"
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
            except ImportError:
                raise RuntimeError("anthropic 패키지가 설치되지 않았습니다.")
        return self._client

    @property
    def model_name(self) -> str:
        return self._model

    async def complete_stream(
        self,
        system_prompt: str,
        messages: list[dict],
    ) -> AsyncGenerator[str, None]:
        client = self._get_client()
        async with client.messages.stream(
            model=self._model,
            max_tokens=2048,
            system=system_prompt,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict],
    ) -> tuple[str, int]:
        client = self._get_client()
        response = await client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=system_prompt,
            messages=messages,
        )
        content = response.content[0].text if response.content else ""
        token_used = (
            response.usage.input_tokens + response.usage.output_tokens
            if response.usage else 0
        )
        return content, token_used


def get_llm_provider() -> LLMProvider:
    """설정에 따라 적절한 LLMProvider를 반환한다."""
    provider_name = settings.llm_provider.lower()
    if provider_name == "anthropic" and settings.anthropic_api_key:
        return AnthropicLLMProvider()
    if settings.openai_api_key:
        return OpenAILLMProvider()
    logger.warning(
        "LLM API 키가 설정되지 않았습니다. MockLLMProvider를 사용합니다."
    )
    return MockLLMProvider()


class MockLLMProvider(LLMProvider):
    """개발/테스트용 Mock LLM — API 키 없이 동작."""

    @property
    def model_name(self) -> str:
        return "mock-llm"

    async def complete_stream(
        self,
        system_prompt: str,
        messages: list[dict],
    ) -> AsyncGenerator[str, None]:
        yield "LLM API 키가 설정되지 않아 실제 답변을 생성할 수 없습니다. "
        yield "OPENAI_API_KEY 또는 ANTHROPIC_API_KEY 환경 변수를 설정하세요."

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict],
    ) -> tuple[str, int]:
        return (
            "LLM API 키가 설정되지 않아 실제 답변을 생성할 수 없습니다. "
            "OPENAI_API_KEY 또는 ANTHROPIC_API_KEY 환경 변수를 설정하세요.",
            0,
        )


# ---------------------------------------------------------------------------
# PromptBuilder (Phase 12 DocumentType별 커스터마이징 대비)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """당신은 문서 기반 지식 도우미입니다.
아래의 문서 컨텍스트를 바탕으로 사용자의 질문에 정확하게 답변하세요.

규칙:
1. 반드시 제공된 컨텍스트에 근거해서만 답변하세요.
2. 답변에서 근거로 사용한 내용은 [숫자] 형식으로 출처를 표기하세요. 예: [1], [2]
3. 컨텍스트에 없는 내용은 "제공된 문서에서 해당 정보를 찾을 수 없습니다"라고 답변하세요.
4. 답변은 한국어로 작성하세요.
5. 마크다운 형식을 활용해 가독성 있게 작성하세요.

<document_context>
{context}
</document_context>"""


def build_system_prompt(context: str, document_type: Optional[str] = None) -> str:
    """시스템 프롬프트 생성. Phase 12: DocumentTypeRegistry 경유로 타입별 템플릿 사용."""
    if document_type:
        try:
            from app.plugins.base import DocumentTypeRegistry
            plugin = DocumentTypeRegistry.instance().get(document_type)
            return plugin.rag_plugin().get_prompt_template().render(context)
        except Exception as exc:
            logger.warning(
                "RAG 플러그인 프롬프트 템플릿 조회 실패 (%s), 기본값 사용: %s", document_type, exc
            )
    return _SYSTEM_PROMPT_TEMPLATE.format(context=context)


# ---------------------------------------------------------------------------
# CitationLinker
# ---------------------------------------------------------------------------

class CitationLinker:
    """응답 문장의 [n] 마커를 원본 청크와 매핑."""

    def extract_citations(
        self,
        answer: str,
        chunks: list[RetrievedChunk],
    ) -> list[Citation]:
        """응답 텍스트에서 [n] 패턴을 찾아 Citation 목록으로 변환."""
        cited_indices = set(int(m) for m in re.findall(r"\[(\d+)\]", answer))
        citations = []
        for idx in sorted(cited_indices):
            if 1 <= idx <= len(chunks):
                chunk = chunks[idx - 1]
                citations.append(Citation(
                    index=idx,
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    document_title=chunk.document_title,
                    node_id=chunk.node_id,
                    node_path=chunk.node_path,
                    source_text=chunk.source_text[:300],  # 요약 (300자)
                    similarity=chunk.similarity,
                ))
        return citations


# ---------------------------------------------------------------------------
# RAGService — 전체 파이프라인 조율
# ---------------------------------------------------------------------------

class RAGService:
    """RAG 파이프라인 전체 조율 서비스."""

    def __init__(
        self,
        llm_provider: Optional[LLMProvider] = None,
        query_processor: Optional[QueryProcessor] = None,
        retriever: Optional[Retriever] = None,
        reranker: Optional[Reranker] = None,
        context_builder: Optional[ContextBuilder] = None,
        citation_linker: Optional[CitationLinker] = None,
    ):
        self._llm = llm_provider
        self._qp = query_processor or QueryProcessor()
        self._retriever = retriever or Retriever()
        self._reranker = reranker or Reranker()
        self._cb = context_builder or ContextBuilder()
        self._cl = citation_linker or CitationLinker()

    def _get_llm(self) -> LLMProvider:
        if self._llm is None:
            self._llm = get_llm_provider()
        return self._llm

    # ------------------------------------------------------------------
    # DB 단계 — 컨텍스트 준비 (RAG-002: conn 조기 해제)
    # ------------------------------------------------------------------

    def prepare_context(
        self,
        conn: psycopg2.extensions.connection,
        question: str,
        *,
        actor_role: Optional[str] = None,
        document_id: Optional[str] = None,
        document_type: Optional[str] = None,
        history: Optional[list[dict]] = None,
    ) -> tuple[str, list[RetrievedChunk], list[dict]]:
        """Retrieve → Rerank → ContextBuilder 단계만 실행한다.

        DB 연결이 필요한 작업을 모두 완료하고 (context, included_chunks, messages)를
        반환한다. 호출자는 이 메서드 반환 직후 conn을 해제하면 된다.
        LLM 스트리밍 중에는 DB 연결을 점유하지 않는다 (RAG-002 DB 풀 고갈 방지).
        """
        question = self._qp.normalize(question)

        # Phase 12: 타입별 RAG 설정 사전 로드 (top_n, max_context_tokens)
        max_tokens = settings.rag_max_context_tokens
        top_n = settings.rag_top_n
        if document_type:
            try:
                from app.plugins.base import DocumentTypeRegistry
                plugin = DocumentTypeRegistry.instance().get(document_type)
                ctx_cfg = plugin.rag_plugin().get_context_config()
                max_tokens = ctx_cfg.get("max_context_tokens", max_tokens)
                top_n = ctx_cfg.get("top_n", top_n)
            except Exception as exc:
                logger.warning("DocumentType plugin context config 조회 실패 (%s), 기본값 사용: %s", document_type, exc)

        # 1. Retrieve
        raw_chunks = self._retriever.retrieve(
            conn, question,
            actor_role=actor_role,
            document_id=document_id,
            top_k=settings.rag_top_k,
        )

        # 2. Rerank (타입별 top_n 적용)
        if settings.rag_reranker_enabled:
            chunks = self._reranker.rerank(
                raw_chunks,
                top_n=top_n,
                threshold=settings.rag_reranker_threshold,
            )
        else:
            chunks = raw_chunks[:top_n]

        # 3. Build context (타입별 max_context_tokens 적용)
        context, included_chunks = self._cb.build(chunks, max_tokens=max_tokens)

        # 4. Build messages
        messages = _build_messages(question, history or [])

        return context, included_chunks, messages

    # ------------------------------------------------------------------
    # LLM 스트리밍 단계 — DB 연결 없음 (RAG-002, RAG-001)
    # ------------------------------------------------------------------

    async def stream_answer(
        self,
        context: str,
        included_chunks: list[RetrievedChunk],
        messages: list[dict],
        *,
        conversation_id: str,
        message_id: Optional[str] = None,
        document_type: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """LLM 스트리밍 단계 — DB 연결 없이 실행.

        RAG-001: 내부 예외 메시지를 클라이언트에게 노출하지 않는다.
        RAG-002: DB 연결을 사용하지 않아 커넥션 풀 점유가 없다.
        """
        message_id = message_id or str(uuid.uuid4())

        try:
            llm = self._get_llm()
            system_prompt = build_system_prompt(context, document_type=document_type)

            # start 이벤트
            yield _sse_data({"event": "start", "data": {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "model": llm.model_name,
            }})

            # 스트리밍 응답
            full_answer = ""
            async for token in llm.complete_stream(system_prompt, messages):
                full_answer += token
                yield _sse_data({"event": "delta", "data": {"text": token}})

            # Citation 추출 후 전송
            citations = self._cl.extract_citations(full_answer, included_chunks)
            citation_dicts = [
                {
                    "index": c.index,
                    "chunk_id": c.chunk_id,
                    "document_id": c.document_id,
                    "document_title": c.document_title,
                    "node_id": c.node_id,
                    "node_path": c.node_path,
                    "source_text": c.source_text,
                    "similarity": c.similarity,
                }
                for c in citations
            ]
            chunk_dicts = [
                {
                    "chunk_id": ch.chunk_id,
                    "document_id": ch.document_id,
                    "document_title": ch.document_title,
                    "node_id": ch.node_id,
                    "source_text": ch.source_text[:200],
                    "similarity": ch.similarity,
                    "chunk_index": ch.chunk_index,
                }
                for ch in included_chunks
            ]
            yield _sse_data({"event": "citation", "data": {
                "citations": citation_dicts,
                "context_chunks": chunk_dicts,
            }})

            # done 이벤트
            yield _sse_data({"event": "done", "data": {
                "message_id": message_id,
                "answer": full_answer,
                "token_used": 0,
            }})

        except Exception as exc:
            logger.error("RAG stream_answer 오류: %s", exc, exc_info=True)
            # RAG-001: 내부 오류 상세는 비노출이 원칙이지만, 운영자가 해결 가능한
            # 설정성 오류(모델 접근 권한 / API 키 누락)는 사용자에게 간결한 힌트만
            # 제공한다. 민감 토큰/경로/스택트레이스는 절대 포함하지 않는다.
            message = "응답 생성 중 오류가 발생했습니다."
            try:
                from openai import (
                    AuthenticationError as _OAIAuthErr,
                    PermissionDeniedError as _OAIPermErr,
                    NotFoundError as _OAINotFoundErr,
                )
                if isinstance(exc, _OAIPermErr) or isinstance(exc, _OAINotFoundErr):
                    message = (
                        "LLM 모델에 접근할 수 없습니다. "
                        "관리자에게 LLM_MODEL 설정 확인을 요청하세요."
                    )
                elif isinstance(exc, _OAIAuthErr):
                    message = "LLM API 키가 올바르지 않습니다. 관리자에게 문의하세요."
            except Exception:
                pass
            yield _sse_data({"event": "error", "data": {"message": message}})

    # ------------------------------------------------------------------
    # 비스트리밍 단건 질의
    # ------------------------------------------------------------------

    async def query(
        self,
        conn: psycopg2.extensions.connection,
        question: str,
        *,
        conversation_id: str,
        actor_role: Optional[str] = None,
        document_id: Optional[str] = None,
        history: Optional[list[dict]] = None,
    ) -> RAGResponse:
        """단건 RAG 질의 — 전체 응답 반환."""
        question = self._qp.normalize(question)

        # 1. Retrieve
        raw_chunks = self._retriever.retrieve(
            conn, question,
            actor_role=actor_role,
            document_id=document_id,
            top_k=settings.rag_top_k,
        )

        # 2. Rerank
        if settings.rag_reranker_enabled:
            chunks = self._reranker.rerank(
                raw_chunks,
                top_n=settings.rag_top_n,
                threshold=settings.rag_reranker_threshold,
            )
        else:
            chunks = raw_chunks[:settings.rag_top_n]

        # 3. Build context
        context, included_chunks = self._cb.build(
            chunks, max_tokens=settings.rag_max_context_tokens
        )

        # 4. Build prompt messages
        system_prompt = build_system_prompt(context)
        messages = _build_messages(question, history or [])

        # 5. LLM 호출
        llm = self._get_llm()
        answer, token_used = await llm.complete(system_prompt, messages)

        # 6. Citation 추출
        citations = self._cl.extract_citations(answer, included_chunks)

        return RAGResponse(
            answer=answer,
            citations=citations,
            context_chunks=included_chunks,
            conversation_id=conversation_id,
            message_id=str(uuid.uuid4()),
            model=llm.model_name,
            token_used=token_used,
        )

    # ------------------------------------------------------------------
    # SSE 스트리밍 질의
    # ------------------------------------------------------------------

    async def query_stream(
        self,
        conn: psycopg2.extensions.connection,
        question: str,
        *,
        conversation_id: str,
        actor_role: Optional[str] = None,
        document_id: Optional[str] = None,
        history: Optional[list[dict]] = None,
    ) -> AsyncGenerator[str, None]:
        """SSE 스트리밍 RAG 질의.

        SSE 이벤트 형식:
          data: {"event": "start", "data": {"conversation_id": "...", "model": "..."}}
          data: {"event": "delta", "data": {"text": "..."}}
          data: {"event": "citation", "data": [Citation, ...]}
          data: {"event": "done", "data": {"token_used": 123, "message_id": "..."}}
          data: {"event": "error", "data": {"message": "..."}}
        """
        question = self._qp.normalize(question)
        message_id = str(uuid.uuid4())

        try:
            # 1. Retrieve
            raw_chunks = self._retriever.retrieve(
                conn, question,
                actor_role=actor_role,
                document_id=document_id,
                top_k=settings.rag_top_k,
            )

            # 2. Rerank
            if settings.rag_reranker_enabled:
                chunks = self._reranker.rerank(
                    raw_chunks,
                    top_n=settings.rag_top_n,
                    threshold=settings.rag_reranker_threshold,
                )
            else:
                chunks = raw_chunks[:settings.rag_top_n]

            # 3. Build context
            context, included_chunks = self._cb.build(
                chunks, max_tokens=settings.rag_max_context_tokens
            )

            # 4. LLM 준비
            llm = self._get_llm()
            system_prompt = build_system_prompt(context)
            messages = _build_messages(question, history or [])

            # start 이벤트
            yield _sse_data({"event": "start", "data": {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "model": llm.model_name,
            }})

            # 5. 스트리밍 응답
            full_answer = ""
            async for token in llm.complete_stream(system_prompt, messages):
                full_answer += token
                yield _sse_data({"event": "delta", "data": {"text": token}})

            # 6. Citation 추출 후 전송
            citations = self._cl.extract_citations(full_answer, included_chunks)
            citation_dicts = [
                {
                    "index": c.index,
                    "chunk_id": c.chunk_id,
                    "document_id": c.document_id,
                    "document_title": c.document_title,
                    "node_id": c.node_id,
                    "node_path": c.node_path,
                    "source_text": c.source_text,
                    "similarity": c.similarity,
                }
                for c in citations
            ]
            chunk_dicts = [
                {
                    "chunk_id": ch.chunk_id,
                    "document_id": ch.document_id,
                    "document_title": ch.document_title,
                    "node_id": ch.node_id,
                    "source_text": ch.source_text[:200],
                    "similarity": ch.similarity,
                    "chunk_index": ch.chunk_index,
                }
                for ch in included_chunks
            ]
            yield _sse_data({"event": "citation", "data": {
                "citations": citation_dicts,
                "context_chunks": chunk_dicts,
            }})

            # done 이벤트
            yield _sse_data({"event": "done", "data": {
                "message_id": message_id,
                "answer": full_answer,
                "token_used": 0,  # 스트리밍에서는 정확한 토큰 수 불가
            }})

        except Exception as exc:
            logger.error("RAG 스트리밍 오류: %s", exc, exc_info=True)
            # RAG-001: 내부 오류 상세를 클라이언트에게 노출하지 않는다
            yield _sse_data({"event": "error", "data": {"message": "요청 처리 중 오류가 발생했습니다."}})


def _sse_data(payload: dict) -> str:
    """SSE data 라인 포맷으로 직렬화."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _build_messages(question: str, history: list[dict]) -> list[dict]:
    """LLM messages 배열 구성 (이전 대화 + 현재 질문)."""
    messages = list(history)
    messages.append({"role": "user", "content": question})
    return messages


# ---------------------------------------------------------------------------
# 싱글턴
# ---------------------------------------------------------------------------

rag_service = RAGService()
