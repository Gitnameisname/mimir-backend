"""
RAG router — /api/v1/rag

Phase 11: 문서 기반 자연어 질의응답 API.

엔드포인트:
  POST /rag/query                          단건 RAG 질의 (스트리밍 SSE)
  POST /rag/conversations                  대화 세션 생성
  GET  /rag/conversations                  대화 목록 조회
  GET  /rag/conversations/{id}             대화 상세 (메시지 포함)
  GET  /rag/conversations/{id}/messages    메시지 목록
  DELETE /rag/conversations/{id}           대화 삭제

설계:
  - 권한 필터: Retriever 레이어에서 actor_role 강제 적용
  - 스트리밍: StreamingResponse + SSE (text/event-stream)
  - 비스트리밍: 단건 JSON 응답
  - Rate limit: 인증 사용자 30회/분 (LLM 비용 절감)
"""

import json
import logging
import uuid
from typing import Optional, AsyncGenerator  # noqa: F401

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.auth import resolve_current_actor
from app.api.auth.authorization import ResourceRef, authorization_service
from app.api.auth.models import ActorContext
from app.api.rate_limit import limiter
from app.api.responses import success_response
from app.db import get_db
from app.repositories.rag_repository import rag_repository
from app.schemas.rag import (
    RAGQueryRequest,
    RAGQueryResponse,
    ConversationCreate,
    ConversationResponse,
    ConversationListResponse,
    ConversationDetailResponse,
    MessageResponse,
    Citation,
    RetrievedChunk,
)
from app.services.rag_service import rag_service
from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

_RAG_LIMIT = "30/minute"   # LLM 호출 비용 고려


# ---------------------------------------------------------------------------
# POST /rag/query — 단건 RAG 질의 (SSE 스트리밍)
# ---------------------------------------------------------------------------

@router.post("/query")
@limiter.limit(_RAG_LIMIT)
async def rag_query(
    request: Request,
    body: RAGQueryRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """RAG 질의 — 스트리밍(기본) 또는 단건 JSON 응답."""
    # RAG-006: RBAC 매트릭스를 통한 권한 검사
    authorization_service.authorize(
        actor, "rag.query", ResourceRef(resource_type="rag"),
    )

    user_id = actor.actor_id

    # 대화 세션 확보 (없으면 새로 생성)
    with get_db() as conn:
        if body.conversation_id:
            conv = rag_repository.get_conversation(conn, body.conversation_id, user_id)
            if not conv:
                raise HTTPException(status_code=404, detail="대화 세션을 찾을 수 없습니다.")
            conversation_id = body.conversation_id
        else:
            conv = rag_repository.create_conversation(
                conn,
                user_id=user_id,
                document_id=body.document_id,
            )
            conversation_id = conv["id"]
            conn.commit()

    # 스트리밍 응답
    if body.stream:
        return StreamingResponse(
            _stream_rag(
                question=body.question,
                conversation_id=conversation_id,
                user_id=user_id,
                actor_role=actor.role,
                document_id=body.document_id,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # 비스트리밍 응답
    with get_db() as conn:
        history = rag_repository.get_history_for_llm(
            conn, conversation_id, max_turns=settings.rag_max_history_turns
        )
        result = await rag_service.query(
            conn,
            body.question,
            conversation_id=conversation_id,
            actor_role=actor.role,
            document_id=body.document_id,
            history=history,
        )
        # 사용자 메시지 저장
        user_msg_id = str(uuid.uuid4())
        rag_repository.add_message(
            conn,
            message_id=user_msg_id,
            conversation_id=conversation_id,
            role="user",
            content=body.question,
        )
        # 어시스턴트 응답 저장
        rag_repository.add_message(
            conn,
            message_id=result.message_id,
            conversation_id=conversation_id,
            role="assistant",
            content=result.answer,
            citations=[_citation_to_dict(c) for c in result.citations],
            context_chunks=[_chunk_to_dict(ch) for ch in result.context_chunks],
            token_used=result.token_used,
            model=result.model,
        )
        rag_repository.touch_conversation(
            conn, conversation_id,
            title=body.question[:50] if not conv.get("title") else None,
        )
        conn.commit()

    return success_response(RAGQueryResponse(
        answer=result.answer,
        citations=[_to_citation_schema(c) for c in result.citations],
        context_chunks=[_to_chunk_schema(ch) for ch in result.context_chunks],
        conversation_id=conversation_id,
        message_id=result.message_id,
        model=result.model,
        token_used=result.token_used,
    ))


async def _stream_rag(
    question: str,
    conversation_id: str,
    user_id: str,
    actor_role: Optional[str],
    document_id: Optional[str],
) -> AsyncGenerator[bytes, None]:
    """SSE 스트리밍 생성기.

    RAG-002: DB 연결을 컨텍스트 준비 단계에서만 사용하고 LLM 스트리밍 전에 해제한다.
    커넥션 풀 점유 시간 = Retrieve/Rerank/ContextBuild 시간만 (LLM 30-60s 미포함).
    RAG-001: 내부 예외 메시지를 클라이언트에게 노출하지 않는다.
    """
    try:
        # --- DB 단계 1: 이력 조회 ---
        with get_db() as conn:
            history = rag_repository.get_history_for_llm(
                conn, conversation_id, max_turns=settings.rag_max_history_turns
            )

        # --- DB 단계 2: 사용자 메시지 저장 ---
        user_msg_id = str(uuid.uuid4())
        with get_db() as conn:
            rag_repository.add_message(
                conn,
                message_id=user_msg_id,
                conversation_id=conversation_id,
                role="user",
                content=question,
            )
            conn.commit()

        # --- DB 단계 3: Retrieve + Rerank + ContextBuild ---
        with get_db() as conn:
            context, included_chunks, messages = rag_service.prepare_context(
                conn, question,
                actor_role=actor_role,
                document_id=document_id,
                history=history,
            )
        # conn이 이 지점에서 반환됨 — 이후 LLM 스트리밍 중 DB 연결 미점유

        # --- LLM 스트리밍 단계 (DB 연결 없음) ---
        message_id = str(uuid.uuid4())
        full_answer = ""
        final_citations: list = []
        final_chunks: list = []

        async for sse_line in rag_service.stream_answer(
            context, included_chunks, messages,
            conversation_id=conversation_id,
            message_id=message_id,
        ):
            yield sse_line.encode("utf-8")

            # done/citation 이벤트에서 저장용 데이터 수집
            if sse_line.startswith("data: "):
                try:
                    payload = json.loads(sse_line[6:])
                    event = payload.get("event")
                    if event == "done":
                        full_answer = payload["data"].get("answer", full_answer)
                    elif event == "citation":
                        final_citations = payload["data"].get("citations", [])
                        final_chunks = payload["data"].get("context_chunks", [])
                except Exception:
                    pass

        # --- DB 단계 4: 어시스턴트 응답 저장 ---
        if full_answer:
            with get_db() as conn:
                rag_repository.add_message(
                    conn,
                    message_id=message_id,
                    conversation_id=conversation_id,
                    role="assistant",
                    content=full_answer,
                    citations=final_citations,
                    context_chunks=final_chunks,
                    token_used=None,
                    model=None,
                )
                rag_repository.touch_conversation(conn, conversation_id)
                conn.commit()

    except Exception as exc:
        logger.error("RAG 스트리밍 오류: %s", exc, exc_info=True)
        # RAG-001: 내부 오류 상세를 클라이언트에게 노출하지 않는다
        error_line = f"data: {json.dumps({'event': 'error', 'data': {'message': '요청 처리 중 오류가 발생했습니다.'}}, ensure_ascii=False)}\n\n"
        yield error_line.encode("utf-8")


# ---------------------------------------------------------------------------
# POST /rag/conversations — 대화 세션 생성
# ---------------------------------------------------------------------------

@router.post("/conversations", status_code=201)
def create_conversation(
    body: ConversationCreate,
    actor: ActorContext = Depends(resolve_current_actor),
):
    authorization_service.authorize(
        actor, "rag.conversation.write", ResourceRef(resource_type="rag"),
    )

    with get_db() as conn:
        conv = rag_repository.create_conversation(
            conn,
            user_id=actor.actor_id,
            title=body.title,
            document_id=body.document_id,
        )
        conn.commit()

    return success_response(_to_conv_schema(conv))


# ---------------------------------------------------------------------------
# GET /rag/conversations — 대화 목록 조회
# ---------------------------------------------------------------------------

@router.get("/conversations")
def list_conversations(
    page: int = 1,
    limit: int = 20,
    actor: ActorContext = Depends(resolve_current_actor),
):
    authorization_service.authorize(
        actor, "rag.conversation.read", ResourceRef(resource_type="rag"),
    )
    # RAG-005: limit 상한 — 클라이언트가 임의로 큰 값을 보내도 100으로 제한
    limit = min(limit, 100)
    offset = (page - 1) * limit
    with get_db() as conn:
        convs, total = rag_repository.list_conversations(
            conn, actor.actor_id, limit=limit, offset=offset
        )

    return success_response(ConversationListResponse(
        conversations=[_to_conv_schema(c) for c in convs],
        total=total,
    ))


# ---------------------------------------------------------------------------
# GET /rag/conversations/{id} — 대화 상세 (메시지 포함)
# ---------------------------------------------------------------------------

@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    authorization_service.authorize(
        actor, "rag.conversation.read", ResourceRef(resource_type="rag"),
    )

    with get_db() as conn:
        conv = rag_repository.get_conversation(conn, conversation_id, actor.actor_id)
        if not conv:
            raise HTTPException(status_code=404, detail="대화 세션을 찾을 수 없습니다.")
        messages = rag_repository.list_messages(conn, conversation_id)

    return success_response(ConversationDetailResponse(
        conversation=_to_conv_schema(conv),
        messages=[_to_msg_schema(m) for m in messages],
    ))


# ---------------------------------------------------------------------------
# GET /rag/conversations/{id}/messages — 메시지 목록
# ---------------------------------------------------------------------------

@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    authorization_service.authorize(
        actor, "rag.conversation.read", ResourceRef(resource_type="rag"),
    )

    with get_db() as conn:
        conv = rag_repository.get_conversation(conn, conversation_id, actor.actor_id)
        if not conv:
            raise HTTPException(status_code=404, detail="대화 세션을 찾을 수 없습니다.")
        messages = rag_repository.list_messages(conn, conversation_id)

    return success_response({"messages": [_to_msg_schema(m) for m in messages]})


# ---------------------------------------------------------------------------
# DELETE /rag/conversations/{id} — 대화 삭제
# ---------------------------------------------------------------------------

@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    authorization_service.authorize(
        actor, "rag.conversation.delete", ResourceRef(resource_type="rag"),
    )

    with get_db() as conn:
        deleted = rag_repository.delete_conversation(conn, conversation_id, actor.actor_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="대화 세션을 찾을 수 없습니다.")
        conn.commit()


# ---------------------------------------------------------------------------
# 내부 변환 유틸
# ---------------------------------------------------------------------------

def _to_conv_schema(c: dict) -> ConversationResponse:
    return ConversationResponse(
        id=c["id"],
        user_id=c["user_id"],
        title=c.get("title"),
        document_id=c.get("document_id"),
        created_at=c["created_at"],
        updated_at=c["updated_at"],
    )


def _to_msg_schema(m: dict) -> MessageResponse:
    citations = [Citation(**c) for c in (m.get("citations") or [])]
    chunks = [RetrievedChunk(**ch) for ch in (m.get("context_chunks") or [])]
    return MessageResponse(
        id=m["id"],
        conversation_id=m["conversation_id"],
        role=m["role"],
        content=m["content"],
        citations=citations,
        context_chunks=chunks,
        token_used=m.get("token_used"),
        model=m.get("model"),
        created_at=m["created_at"],
    )


def _citation_to_dict(c) -> dict:
    return {
        "index": c.index,
        "chunk_id": c.chunk_id,
        "document_id": c.document_id,
        "document_title": c.document_title,
        "node_id": c.node_id,
        "node_path": c.node_path,
        "source_text": c.source_text,
        "similarity": c.similarity,
    }


def _chunk_to_dict(ch) -> dict:
    return {
        "chunk_id": ch.chunk_id,
        "document_id": ch.document_id,
        "document_title": ch.document_title,
        "node_id": ch.node_id,
        "source_text": ch.source_text[:200],
        "similarity": ch.similarity,
        "chunk_index": ch.chunk_index,
    }


def _to_citation_schema(c) -> Citation:
    return Citation(
        index=c.index,
        chunk_id=c.chunk_id,
        document_id=c.document_id,
        document_title=c.document_title,
        node_id=c.node_id,
        node_path=c.node_path,
        source_text=c.source_text,
        similarity=c.similarity,
    )


def _to_chunk_schema(ch) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=ch.chunk_id,
        document_id=ch.document_id,
        document_title=ch.document_title,
        node_id=ch.node_id,
        source_text=ch.source_text[:200],
        similarity=ch.similarity,
        chunk_index=ch.chunk_index,
    )
