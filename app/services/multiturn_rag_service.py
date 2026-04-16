"""멀티턴 RAG 서비스 — Phase 2 S2 FG2.3.

단발 쿼리(S1 호환) + 멀티턴 대화(S2 확장) 모드를 모두 지원한다.

설계 원칙:
  - S1 RAGService에 위임(delegation) — 코드 복사 금지
  - conversation_id 없으면 단발 쿼리 모드 (S1 동작 그대로)
  - conversation_id 있으면 멀티턴 모드 (QueryRewriter + CitationCache)
  - actor_type 감사 로그 필수 (S2 원칙)
"""
from __future__ import annotations

import logging
from typing import List, Optional
from uuid import UUID

import psycopg2.extensions

from app.schemas.rag import RAGCitationInfo, RAGRequest, RAGResponse
from app.schemas.citation import Citation
from app.services.retrieval.citation_cache import (
    CitationCache,
    ConversationTurn,
    get_citation_cache,
)
from app.services.retrieval.conversation_compressor import ConversationCompressor
from app.services.retrieval.query_rewriter import QueryRewriter

logger = logging.getLogger(__name__)

# 대화 이력 압축 시작 임계값 (메시지 수 기준)
_COMPRESS_THRESHOLD = 6


class MultiturnRAGService:
    """멀티턴 대화를 지원하는 RAG 서비스.

    Args:
        conn: DB 연결
        query_rewriter: QueryRewriter 인스턴스
        compressor: ConversationCompressor 인스턴스
        citation_cache: CitationCache 인스턴스 (기본: 전역 싱글톤)
    """

    def __init__(
        self,
        conn: psycopg2.extensions.connection,
        query_rewriter: QueryRewriter,
        compressor: ConversationCompressor,
        citation_cache: Optional[CitationCache] = None,
    ) -> None:
        self._conn = conn
        self._rewriter = query_rewriter
        self._compressor = compressor
        self._cache = citation_cache or get_citation_cache()

    async def answer(
        self,
        query: str,
        top_k: int = 10,
        document_type: Optional[str] = None,
        conversation_id: Optional[UUID] = None,
        actor_role: Optional[str] = None,
        actor_type: str = "user",
        actor_id: Optional[str] = None,
    ) -> RAGResponse:
        """질의에 답변한다.

        Args:
            query: 사용자 질의
            top_k: 검색 결과 수
            document_type: 대상 DocumentType (None → 전체)
            conversation_id: 멀티턴 대화 ID (None → 단발 쿼리 모드)
            actor_role: 요청자 역할 (ACL 필터용)
            actor_type: "user" 또는 "agent" (감사 로그용, S2 원칙)
            actor_id: 요청자 ID (대화 소유권 검증용, IDOR 방지)

        Returns:
            RAGResponse

        Raises:
            PermissionError: conversation_id가 다른 actor 소유인 경우
        """
        logger.info(
            "MultiturnRAGService.answer: query=%r conversation_id=%s actor_type=%s",
            query[:50],
            conversation_id,
            actor_type,
        )

        # ── 단발 쿼리 모드 (S1 하위호환) ───────────────────────────────
        if conversation_id is None:
            return await self._single_turn_answer(
                query=query,
                top_k=top_k,
                document_type=document_type,
                actor_role=actor_role,
            )

        # ── 멀티턴 모드 ─────────────────────────────────────────────────
        # IDOR 방지: conversation_id 소유권 검증
        if not self._cache.verify_ownership(conversation_id, actor_id):
            logger.warning(
                "CitationCache ownership violation: conversation=%s actor_id=%s",
                conversation_id,
                actor_id,
            )
            raise PermissionError(
                f"conversation_id {conversation_id!r}에 대한 접근 권한이 없습니다."
            )

        turn_number = self._cache.get_turn_number(conversation_id)
        history = self._cache.get_history(conversation_id)

        # 1. 쿼리 재작성
        rewritten_query = await self._rewriter.rewrite_query(query, history)

        # 2. 대화 압축 (이력이 길 경우) — 압축 요약을 rewriter 컨텍스트로 활용
        context_compressed = False
        if len(history) > _COMPRESS_THRESHOLD:
            compressed_context = await self._compressor.compress(
                history, strategy="summarize"
            )
            context_compressed = True
            # 압축된 요약을 단일 system 메시지로 변환하여 rewriter에 전달
            from app.schemas.conversation import ConversationMessage, MessageRole
            history = [
                ConversationMessage(
                    role=MessageRole.SYSTEM,
                    content=compressed_context,
                    turn_number=0,
                )
            ]
            # 압축된 컨텍스트로 쿼리 재작성 재수행
            rewritten_query = await self._rewriter.rewrite_query(query, history)

        # 3. 검색 (재작성된 쿼리 사용)
        search_query = rewritten_query or query
        answer_text, citation_infos = await self._generate_answer(
            query=search_query,
            top_k=top_k,
            document_type=document_type,
            actor_role=actor_role,
        )

        # 4. 대화 이력에 이번 턴 저장
        raw_citations = [ci.citation for ci in citation_infos]
        turn = ConversationTurn(
            turn_number=turn_number,
            query=query,
            rewritten_query=rewritten_query if rewritten_query != query else None,
            citations=raw_citations,
            answer=answer_text,
        )
        self._cache.add_turn(conversation_id, turn, actor_id=actor_id)

        return RAGResponse(
            answer=answer_text,
            citations=citation_infos,
            rewritten_query=rewritten_query if rewritten_query != query else None,
            context_compressed=context_compressed,
            turn_number=turn_number,
        )

    async def _single_turn_answer(
        self,
        query: str,
        top_k: int,
        document_type: Optional[str],
        actor_role: Optional[str],
    ) -> RAGResponse:
        """단발 쿼리 모드 — S1 RAGService에 위임한다."""
        from app.services.rag_service import rag_service

        # S1 prepare_context 호출 (DB 작업만)
        context, chunks, messages = rag_service.prepare_context(
            self._conn,
            query,
            actor_role=actor_role,
            document_type=document_type,
        )

        # S1 LLM 호출
        from app.services.rag_service import get_llm_provider, build_system_prompt, CitationLinker
        llm = get_llm_provider()
        system_prompt = build_system_prompt(context, document_type)
        answer_text, _tokens = await llm.complete(
            system_prompt=system_prompt,
            messages=messages,
        )

        # S1 Citation 추출 → S2 RAGCitationInfo 변환
        linker = CitationLinker()
        s1_citations = linker.extract_citations(answer_text, chunks)

        s2_citations = self._convert_s1_citations(s1_citations, chunks)

        return RAGResponse(
            answer=answer_text,
            citations=s2_citations,
            rewritten_query=None,
            context_compressed=False,
            turn_number=1,
        )

    async def _generate_answer(
        self,
        query: str,
        top_k: int,
        document_type: Optional[str],
        actor_role: Optional[str],
    ) -> tuple[str, List[RAGCitationInfo]]:
        """검색 + LLM 답변 생성."""
        from app.services.rag_service import rag_service, get_llm_provider, build_system_prompt, CitationLinker

        context, chunks, messages = rag_service.prepare_context(
            self._conn,
            query,
            actor_role=actor_role,
            document_type=document_type,
        )
        llm = get_llm_provider()
        system_prompt = build_system_prompt(context, document_type)
        answer_text, _tokens = await llm.complete(
            system_prompt=system_prompt,
            messages=messages,
        )
        linker = CitationLinker()
        s1_citations = linker.extract_citations(answer_text, chunks)
        s2_citations = self._convert_s1_citations(s1_citations, chunks)
        return answer_text, s2_citations

    @staticmethod
    def _convert_s1_citations(s1_citations, chunks) -> List[RAGCitationInfo]:
        """S1 Citation 리스트를 S2 RAGCitationInfo 리스트로 변환한다."""
        from app.services.retrieval.citation_builder import CitationBuilder
        import uuid

        result = []
        for s1_cit in s1_citations:
            # S1 Citation에서 필요한 필드 추출
            doc_id_str = getattr(s1_cit, "document_id", None) or ""
            node_id_str = getattr(s1_cit, "node_id", None) or ""
            source_text = getattr(s1_cit, "source_text", "")

            try:
                doc_id = uuid.UUID(doc_id_str) if doc_id_str else uuid.UUID(int=0)
                node_id = uuid.UUID(node_id_str) if node_id_str else None
            except (ValueError, AttributeError):
                doc_id = uuid.UUID(int=0)
                node_id = None

            citation_5tuple = CitationBuilder.build(
                document_id=doc_id,
                version_id=uuid.UUID(int=0),  # S1에는 version_id 없음 — nil UUID
                node_id=node_id,
                source_text=source_text,
            )
            result.append(RAGCitationInfo(
                index=getattr(s1_cit, "index", len(result) + 1),
                citation=citation_5tuple,
                snippet=source_text[:200],
            ))
        return result
