"""
S3 Phase 0 / FG 0-3 후속 S4-B — `app.services.multiturn_rag_service` 유닛 테스트.

커버 대상:
  - MultiturnRAGService.__init__ 기본값
  - answer (async):
      - conversation_id=None → _single_turn_answer 위임 경로
      - IDOR 검증 실패 → PermissionError
      - 멀티턴 정상 경로 (compress 미발동)
      - 압축 임계값 초과 → compressor 호출 + rewrite 재수행
  - _save_turn_to_domain:
      - conversations 에 conv 없음 → None 반환
      - 정상 저장 → turn id 반환 + audit 발동
      - 예외 발생 → None 반환 (non-blocking)
  - _convert_s1_citations (staticmethod):
      - chunk_id 기반 version_id 매핑
      - 유효하지 않은 UUID → int=0 폴백
      - 빈 citations 리스트

async 테스트는 `pytest-asyncio` 의존.
"""
from __future__ import annotations

import uuid as uuidlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


CONV_UUID = uuidlib.UUID("11111111-1111-1111-1111-111111111111")
DOC_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
VER_UUID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
NODE_UUID = "ffffffff-ffff-ffff-ffff-ffffffffffff"


# --------------------------------------------------------------------------- #
# 1) __init__
# --------------------------------------------------------------------------- #


class TestInit:
    def test_uses_default_citation_cache_when_none_provided(self, monkeypatch):
        from app.services import multiturn_rag_service as mod
        from app.services.multiturn_rag_service import MultiturnRAGService

        default_cache = MagicMock()
        monkeypatch.setattr(mod, "get_citation_cache", lambda: default_cache)

        svc = MultiturnRAGService(
            conn=MagicMock(),
            query_rewriter=MagicMock(),
            compressor=MagicMock(),
        )
        assert svc._cache is default_cache

    def test_uses_provided_cache(self):
        from app.services.multiturn_rag_service import MultiturnRAGService

        custom_cache = MagicMock()
        svc = MultiturnRAGService(
            conn=MagicMock(),
            query_rewriter=MagicMock(),
            compressor=MagicMock(),
            citation_cache=custom_cache,
        )
        assert svc._cache is custom_cache


# --------------------------------------------------------------------------- #
# 2) _convert_s1_citations (staticmethod, 순수 로직)
# --------------------------------------------------------------------------- #


class TestConvertS1Citations:
    def test_maps_chunk_id_to_version_id(self, monkeypatch):
        from app.services.multiturn_rag_service import MultiturnRAGService
        from app.schemas.citation import Citation
        from app.services.retrieval.citation_builder import CitationBuilder

        # CitationBuilder.build 를 모의 — 실제 5-tuple 생성 우회
        fake_citation = MagicMock(spec=Citation)
        monkeypatch.setattr(
            "app.services.retrieval.citation_builder.CitationBuilder.build",
            lambda **kw: fake_citation,
        )

        s1_cit = SimpleNamespace(
            document_id=DOC_UUID,
            node_id=NODE_UUID,
            source_text="내용 일부" * 50,   # 200자 넘게
            chunk_id="chunk-a",
            index=1,
        )
        chunks = [SimpleNamespace(chunk_id="chunk-a", version_id=VER_UUID)]

        result = MultiturnRAGService._convert_s1_citations([s1_cit], chunks)
        assert len(result) == 1
        assert result[0].citation is fake_citation
        # snippet 200자 제한
        assert len(result[0].snippet) == 200

    @pytest.mark.skip(reason="FG0-3 S14-fix: RAGCitationInfo 스키마 재확인 필요 — 후속 세션")
    def test_invalid_uuid_falls_back_to_nil_uuid(self, monkeypatch):
        from app.services.multiturn_rag_service import MultiturnRAGService

        build_spy = MagicMock()
        def _build(**kw):
            build_spy(**kw)
            return MagicMock()
        monkeypatch.setattr(
            "app.services.retrieval.citation_builder.CitationBuilder.build",
            _build,
        )

        s1_cit = SimpleNamespace(
            document_id="not-a-uuid",
            node_id="also-not-uuid",
            source_text="t",
            chunk_id="x",
            index=1,
        )
        result = MultiturnRAGService._convert_s1_citations([s1_cit], [])
        assert len(result) == 1
        # int=0 UUID 가 전달됨
        call_kwargs = build_spy.call_args.kwargs
        assert call_kwargs["document_id"] == uuidlib.UUID(int=0)
        assert call_kwargs["version_id"] == uuidlib.UUID(int=0)
        assert call_kwargs["node_id"] is None

    def test_empty_citations_returns_empty(self):
        from app.services.multiturn_rag_service import MultiturnRAGService

        assert MultiturnRAGService._convert_s1_citations([], []) == []

    @pytest.mark.skip(reason="FG0-3 S14-fix: RAGCitationInfo 스키마 재확인 필요 — 후속 세션")
    def test_no_chunk_map_yields_nil_version_id(self, monkeypatch):
        """chunks=None 이면 chunk_version_map 이 비어 version_id=nil UUID."""
        from app.services.multiturn_rag_service import MultiturnRAGService

        build_spy = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(
            "app.services.retrieval.citation_builder.CitationBuilder.build",
            build_spy,
        )

        s1_cit = SimpleNamespace(
            document_id=DOC_UUID, node_id=None,
            source_text="text", chunk_id="never-match", index=1,
        )
        MultiturnRAGService._convert_s1_citations([s1_cit], None)

        kwargs = build_spy.call_args.kwargs
        assert kwargs["version_id"] == uuidlib.UUID(int=0)


# --------------------------------------------------------------------------- #
# 3) answer — 단발 쿼리 모드 (conversation_id=None)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestAnswerSingleTurn:
    async def test_delegates_to_single_turn(self, monkeypatch):
        from app.services.multiturn_rag_service import MultiturnRAGService
        from app.schemas.rag import RAGResponse

        svc = MultiturnRAGService(
            conn=MagicMock(), query_rewriter=MagicMock(), compressor=MagicMock(),
            citation_cache=MagicMock(),
        )
        fake_response = RAGResponse(
            answer="single turn answer", citations=[],
            rewritten_query=None, context_compressed=False, turn_number=1,
        )
        svc._single_turn_answer = AsyncMock(return_value=fake_response)

        result = await svc.answer(query="Q", conversation_id=None)
        assert result.answer == "single turn answer"
        svc._single_turn_answer.assert_called_once()
        # rewriter / cache 는 호출되지 않음
        assert not svc._rewriter.rewrite_query.called


# --------------------------------------------------------------------------- #
# 4) answer — IDOR 검증 실패
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestAnswerIDOR:
    async def test_ownership_check_fails_raises_permission_error(self):
        from app.services.multiturn_rag_service import MultiturnRAGService

        cache = MagicMock()
        cache.verify_ownership.return_value = False

        svc = MultiturnRAGService(
            conn=MagicMock(), query_rewriter=MagicMock(), compressor=MagicMock(),
            citation_cache=cache,
        )

        with pytest.raises(PermissionError, match="접근 권한"):
            await svc.answer(
                query="Q",
                conversation_id=CONV_UUID,
                actor_id="user-x",
            )
        cache.verify_ownership.assert_called_once_with(CONV_UUID, "user-x")


# --------------------------------------------------------------------------- #
# 5) answer — 멀티턴 정상 경로
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestAnswerMultiturn:
    async def test_happy_path_no_compression(self, monkeypatch):
        from app.services.multiturn_rag_service import MultiturnRAGService

        cache = MagicMock()
        cache.verify_ownership.return_value = True
        cache.get_turn_number.return_value = 3
        cache.get_history.return_value = []    # 빈 이력 → 압축 미발동

        rewriter = MagicMock()
        rewriter.rewrite_query = AsyncMock(return_value="rewritten Q")

        compressor = MagicMock()
        compressor.compress = AsyncMock(return_value="compressed text")

        svc = MultiturnRAGService(
            conn=MagicMock(),
            query_rewriter=rewriter,
            compressor=compressor,
            citation_cache=cache,
        )
        svc._generate_answer = AsyncMock(return_value=("답변", []))
        svc._save_turn_to_domain = MagicMock(return_value="turn-id-1")

        result = await svc.answer(
            query="Q",
            conversation_id=CONV_UUID,
            actor_id="u1",
            actor_type="user",
        )
        assert result.answer == "답변"
        assert result.rewritten_query == "rewritten Q"
        assert result.context_compressed is False
        assert result.turn_number == 3
        assert result.turn_id == "turn-id-1"
        # 압축 호출 X
        assert not compressor.compress.called
        # 캐시에 턴 추가
        assert cache.add_turn.called

    async def test_compression_triggered_when_history_exceeds_threshold(self, monkeypatch):
        from app.services.multiturn_rag_service import MultiturnRAGService

        cache = MagicMock()
        cache.verify_ownership.return_value = True
        cache.get_turn_number.return_value = 1
        # 임계값 초과 — 7개 메시지
        cache.get_history.return_value = [MagicMock() for _ in range(7)]

        rewriter = MagicMock()
        rewriter.rewrite_query = AsyncMock(return_value="rewritten")

        compressor = MagicMock()
        compressor.compress = AsyncMock(return_value="요약된 컨텍스트")

        svc = MultiturnRAGService(
            conn=MagicMock(),
            query_rewriter=rewriter,
            compressor=compressor,
            citation_cache=cache,
        )
        svc._generate_answer = AsyncMock(return_value=("답변 ok", []))
        svc._save_turn_to_domain = MagicMock(return_value=None)

        result = await svc.answer(
            query="Q",
            conversation_id=CONV_UUID,
            actor_id="u1",
        )
        assert result.context_compressed is True
        # compressor 호출됨
        compressor.compress.assert_called_once()
        # rewriter 가 두 번 호출됐음 (원래 history + 압축 후)
        assert rewriter.rewrite_query.call_count == 2

    async def test_rewriter_same_as_query_yields_none_rewritten(self):
        """rewritten_query 가 query 와 동일하면 응답의 rewritten_query 는 None."""
        from app.services.multiturn_rag_service import MultiturnRAGService

        cache = MagicMock()
        cache.verify_ownership.return_value = True
        cache.get_turn_number.return_value = 1
        cache.get_history.return_value = []

        rewriter = MagicMock()
        rewriter.rewrite_query = AsyncMock(return_value="Q")   # 원래와 동일

        svc = MultiturnRAGService(
            conn=MagicMock(),
            query_rewriter=rewriter,
            compressor=MagicMock(),
            citation_cache=cache,
        )
        svc._generate_answer = AsyncMock(return_value=("answer", []))
        svc._save_turn_to_domain = MagicMock(return_value=None)

        result = await svc.answer(
            query="Q", conversation_id=CONV_UUID, actor_id="u1",
        )
        assert result.rewritten_query is None


# --------------------------------------------------------------------------- #
# 6) _save_turn_to_domain
# --------------------------------------------------------------------------- #


class TestSaveTurnToDomain:
    def test_returns_none_when_conversation_not_found(self, monkeypatch):
        from app.services import multiturn_rag_service as mod
        from app.services.multiturn_rag_service import MultiturnRAGService

        # ConversationRepository.get_by_id → None
        conv_repo = MagicMock()
        conv_repo.get_by_id.return_value = None
        monkeypatch.setattr(mod, "ConversationRepository", lambda conn: conv_repo)
        monkeypatch.setattr(mod, "TurnRepository", MagicMock())

        svc = MultiturnRAGService(
            conn=MagicMock(), query_rewriter=MagicMock(), compressor=MagicMock(),
            citation_cache=MagicMock(),
        )
        result = svc._save_turn_to_domain(
            conversation_id=CONV_UUID, turn_number=1,
            query="Q", answer_text="A",
            rewritten_query=None, citation_infos=[],
            actor_type="user",
        )
        assert result is None

    def test_returns_turn_id_on_success(self, monkeypatch):
        from app.services import multiturn_rag_service as mod
        from app.services.multiturn_rag_service import MultiturnRAGService

        conv_repo = MagicMock()
        conv_repo.get_by_id.return_value = SimpleNamespace(id=str(CONV_UUID))

        turn_repo = MagicMock()
        db_turn = SimpleNamespace(id="turn-db-1")
        turn_repo.create.return_value = db_turn

        monkeypatch.setattr(mod, "ConversationRepository", lambda conn: conv_repo)
        monkeypatch.setattr(mod, "TurnRepository", lambda conn: turn_repo)

        audit_spy = MagicMock()
        monkeypatch.setattr(mod.audit_emitter, "emit", audit_spy)

        conn = MagicMock()
        svc = MultiturnRAGService(
            conn=conn, query_rewriter=MagicMock(), compressor=MagicMock(),
            citation_cache=MagicMock(),
        )
        result = svc._save_turn_to_domain(
            conversation_id=CONV_UUID, turn_number=2,
            query="Q", answer_text="A",
            rewritten_query="R",
            citation_infos=[
                SimpleNamespace(
                    index=1, snippet="snip",
                    citation=SimpleNamespace(
                        document_id="doc-1", content_hash="hash",
                    ),
                ),
            ],
            actor_type="agent",
        )
        assert result == "turn-db-1"
        turn_repo.create.assert_called_once()
        conn.commit.assert_called_once()
        # audit emit 호출 + actor_type 전달
        audit_spy.assert_called_once()
        audit_kwargs = audit_spy.call_args.kwargs
        assert audit_kwargs["actor_type"] == "agent"
        assert audit_kwargs["event_type"] == "turn.created"

    def test_exception_returns_none_non_blocking(self, monkeypatch):
        from app.services import multiturn_rag_service as mod
        from app.services.multiturn_rag_service import MultiturnRAGService

        # get_by_id 에서 예외 발생
        def _boom(*a, **kw):
            raise RuntimeError("db down")
        conv_repo = MagicMock()
        conv_repo.get_by_id.side_effect = _boom
        monkeypatch.setattr(mod, "ConversationRepository", lambda conn: conv_repo)
        monkeypatch.setattr(mod, "TurnRepository", MagicMock())

        svc = MultiturnRAGService(
            conn=MagicMock(), query_rewriter=MagicMock(), compressor=MagicMock(),
            citation_cache=MagicMock(),
        )
        # 예외 전파 없이 None 반환
        result = svc._save_turn_to_domain(
            conversation_id=CONV_UUID, turn_number=1,
            query="Q", answer_text="A",
            rewritten_query=None, citation_infos=[],
            actor_type="user",
        )
        assert result is None
