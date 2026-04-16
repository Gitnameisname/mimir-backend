"""
세션 기반 RAG API 단위 테스트 — Task 3-4.

테스트 범위:
  - RAGRequest.actor_type 필드 유효성 검사
  - RAGResponse.turn_id 필드 포함 여부
  - MultiturnRAGService._save_turn_to_domain(): 대화 존재/미존재 분기
  - actor_type 감사 로그 기록 (S2 원칙 ⑤)
  - 하위호환성: conversation_id=None 단발 쿼리에 turn_id=None
  - rag.py: actor_type 결합 로직 (request body 우선)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))


# ===========================================================================
# 1. RAGRequest 스키마 테스트
# ===========================================================================

class TestRAGRequestSchema:
    def test_actor_type_default_none(self):
        from app.schemas.rag import RAGRequest

        req = RAGRequest(query="테스트 질의")
        assert req.actor_type is None

    def test_actor_type_user_valid(self):
        from app.schemas.rag import RAGRequest

        req = RAGRequest(query="테스트", actor_type="user")
        assert req.actor_type == "user"

    def test_actor_type_agent_valid(self):
        from app.schemas.rag import RAGRequest

        req = RAGRequest(query="테스트", actor_type="agent")
        assert req.actor_type == "agent"

    def test_actor_type_invalid_rejected(self):
        from app.schemas.rag import RAGRequest

        with pytest.raises(ValidationError):
            RAGRequest(query="테스트", actor_type="system")

    def test_actor_type_invalid_service_rejected(self):
        from app.schemas.rag import RAGRequest

        with pytest.raises(ValidationError):
            RAGRequest(query="테스트", actor_type="service")

    def test_backward_compatible_without_actor_type(self):
        """actor_type 없어도 기존 쿼리와 동일하게 동작해야 함 (하위호환성)."""
        from app.schemas.rag import RAGRequest

        req = RAGRequest(query="이전 클라이언트 쿼리", top_k=5)
        assert req.actor_type is None
        assert req.conversation_id is None
        assert req.top_k == 5


class TestRAGResponseSchema:
    def test_turn_id_field_exists(self):
        from app.schemas.rag import RAGResponse

        resp = RAGResponse(answer="답변", citations=[], turn_number=1)
        assert resp.turn_id is None

    def test_turn_id_set_when_provided(self):
        from app.schemas.rag import RAGResponse

        tid = str(uuid4())
        resp = RAGResponse(answer="답변", citations=[], turn_number=1, turn_id=tid)
        assert resp.turn_id == tid


# ===========================================================================
# 2. MultiturnRAGService._save_turn_to_domain() 테스트
# ===========================================================================

class TestSaveTurnToDomain:
    """_save_turn_to_domain() 테스트.

    module-level ConversationRepository/TurnRepository/audit_emitter 를 직접 패치.
    """

    def _make_service_and_patch_context(self, mock_conv_repo, mock_turn_repo=None):
        """서비스 인스턴스 + 모듈 수준 패치 컨텍스트 반환."""
        import app.services.multiturn_rag_service as svc_mod
        from app.services.multiturn_rag_service import MultiturnRAGService

        mock_conn = MagicMock()
        svc = MultiturnRAGService(
            conn=mock_conn,
            query_rewriter=MagicMock(),
            compressor=MagicMock(),
            citation_cache=MagicMock(),
        )

        # module-level 심볼 교체 (가장 안정적인 방법)
        original_conv_cls = svc_mod.ConversationRepository
        original_turn_cls = svc_mod.TurnRepository
        original_audit = svc_mod.audit_emitter

        svc_mod.ConversationRepository = lambda conn: mock_conv_repo
        if mock_turn_repo is not None:
            svc_mod.TurnRepository = lambda conn: mock_turn_repo

        return svc, mock_conn, (svc_mod, original_conv_cls, original_turn_cls, original_audit)

    def _restore(self, ctx):
        svc_mod, orig_conv, orig_turn, orig_audit = ctx
        svc_mod.ConversationRepository = orig_conv
        svc_mod.TurnRepository = orig_turn
        svc_mod.audit_emitter = orig_audit

    def test_skips_if_conversation_not_in_db(self):
        """conversation_id가 conversations 테이블에 없으면 저장 생략."""
        from uuid import UUID

        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = None

        svc, mock_conn, ctx = self._make_service_and_patch_context(mock_conv_repo)
        try:
            result = svc._save_turn_to_domain(
                conversation_id=UUID(str(uuid4())),
                turn_number=1, query="질문", answer_text="답변",
                rewritten_query=None, citation_infos=[], actor_type="user",
            )
        finally:
            self._restore(ctx)

        assert result is None
        mock_conn.commit.assert_not_called()

    def test_saves_turn_when_conversation_exists(self):
        """conversations 테이블에 해당 대화가 있으면 Turn 저장."""
        from uuid import UUID
        from datetime import datetime, timezone
        import app.services.multiturn_rag_service as svc_mod
        from app.models.conversation import Conversation, Turn

        now = datetime.now(timezone.utc)
        conv_id = str(uuid4())
        mock_conv = Conversation(
            id=conv_id, owner_id=str(uuid4()), organization_id=str(uuid4()),
            title="테스트", status="active", metadata={}, retention_days=90,
            access_level="private", created_at=now, updated_at=now,
            expires_at=None, deleted_at=None,
        )
        mock_turn = Turn(
            id=str(uuid4()), conversation_id=conv_id, turn_number=1,
            user_message="질문", assistant_response="답변",
            retrieval_metadata={}, created_at=now,
        )

        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = mock_conv
        mock_turn_repo = MagicMock()
        mock_turn_repo.create.return_value = mock_turn
        mock_audit = MagicMock()

        svc, mock_conn, ctx = self._make_service_and_patch_context(mock_conv_repo, mock_turn_repo)
        original_audit = svc_mod.audit_emitter
        svc_mod.audit_emitter = mock_audit
        try:
            result = svc._save_turn_to_domain(
                conversation_id=UUID(conv_id), turn_number=1,
                query="질문", answer_text="답변", rewritten_query=None,
                citation_infos=[], actor_type="user",
            )
        finally:
            svc_mod.audit_emitter = original_audit
            self._restore(ctx)

        assert result == mock_turn.id
        mock_conn.commit.assert_called_once()
        mock_audit.emit.assert_called_once()

    def test_actor_type_agent_in_audit_log(self):
        """actor_type=agent 로 Turn 저장 시 감사 로그에 agent 기록."""
        from uuid import UUID
        from datetime import datetime, timezone
        import app.services.multiturn_rag_service as svc_mod
        from app.models.conversation import Conversation, Turn

        now = datetime.now(timezone.utc)
        conv_id = str(uuid4())
        mock_conv = Conversation(
            id=conv_id, owner_id="svc-001", organization_id=str(uuid4()),
            title="에이전트 대화", status="active", metadata={}, retention_days=90,
            access_level="private", created_at=now, updated_at=now,
            expires_at=None, deleted_at=None,
        )
        mock_turn = Turn(
            id=str(uuid4()), conversation_id=conv_id, turn_number=1,
            user_message="쿼리", assistant_response="응답",
            retrieval_metadata={}, created_at=now,
        )

        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = mock_conv
        mock_turn_repo = MagicMock()
        mock_turn_repo.create.return_value = mock_turn
        mock_audit = MagicMock()

        svc, mock_conn, ctx = self._make_service_and_patch_context(mock_conv_repo, mock_turn_repo)
        original_audit = svc_mod.audit_emitter
        svc_mod.audit_emitter = mock_audit
        try:
            svc._save_turn_to_domain(
                conversation_id=UUID(conv_id), turn_number=1,
                query="쿼리", answer_text="응답", rewritten_query=None,
                citation_infos=[], actor_type="agent",
            )
        finally:
            svc_mod.audit_emitter = original_audit
            self._restore(ctx)

        emit_kwargs = mock_audit.emit.call_args[1]
        assert emit_kwargs["actor_type"] == "agent"

    def test_db_failure_returns_none_non_blocking(self):
        """DB 저장 실패 시 None 반환 (non-blocking)."""
        from uuid import UUID

        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.side_effect = Exception("DB down")

        svc, mock_conn, ctx = self._make_service_and_patch_context(mock_conv_repo)
        try:
            result = svc._save_turn_to_domain(
                conversation_id=UUID(str(uuid4())),
                turn_number=1, query="질문", answer_text="답변",
                rewritten_query=None, citation_infos=[], actor_type="user",
            )
        finally:
            self._restore(ctx)

        assert result is None


# ===========================================================================
# 3. actor_type 결합 로직 정적 검사
# ===========================================================================

class TestActorTypeResolution:
    def test_request_body_actor_type_takes_priority(self):
        """RAGRequest.actor_type 이 있으면 auth context보다 우선."""
        rag_py = (ROOT / "backend/app/api/v1/rag.py").read_text()
        # body.actor_type 를 우선 사용하는 로직 확인
        assert "body.actor_type or" in rag_py

    def test_audit_emit_called_in_rag_answer(self):
        """rag_answer 엔드포인트에서 audit_emitter.emit() 호출 확인."""
        rag_py = (ROOT / "backend/app/api/v1/rag.py").read_text()
        assert "audit_emitter.emit" in rag_py
        assert '"rag.answer"' in rag_py

    def test_turn_id_in_rag_response_schema(self):
        """RAGResponse에 turn_id 필드 확인."""
        rag_py = (ROOT / "backend/app/schemas/rag.py").read_text()
        assert "turn_id" in rag_py

    def test_actor_type_field_in_rag_request_schema(self):
        """RAGRequest에 actor_type 필드 확인."""
        rag_py = (ROOT / "backend/app/schemas/rag.py").read_text()
        assert "actor_type" in rag_py
        assert 'pattern="^(user|agent)$"' in rag_py


# ===========================================================================
# 4. 하위호환성 테스트
# ===========================================================================

class TestBackwardCompatibility:
    def test_rag_request_without_new_fields(self):
        """S1 클라이언트처럼 새 필드 없이 요청해도 동작."""
        from app.schemas.rag import RAGRequest

        req = RAGRequest(query="S1 쿼리", top_k=10)
        assert req.actor_type is None
        assert req.conversation_id is None
        assert req.query == "S1 쿼리"

    def test_rag_response_turn_id_optional(self):
        """S1 응답처럼 turn_id 없어도 동작."""
        from app.schemas.rag import RAGResponse

        resp = RAGResponse(answer="응답", turn_number=1)
        assert resp.turn_id is None
        # model_dump에 turn_id 포함 (None으로)
        d = resp.model_dump()
        assert "turn_id" in d
        assert d["turn_id"] is None
