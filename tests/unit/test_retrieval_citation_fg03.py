"""
S3 Phase 0 / FG 0-3 후속 S5-B — `app.services.retrieval.citation_builder` + `citation_cache` 유닛 테스트.

두 모듈 모두 LLM/DB 의존 없는 순수 로직. 인메모리 캐시의 IDOR / DoS 방어도 포함.

커버:
  - CitationBuilder.build (happy, node_id None→nil, invalid UUID, empty source)
  - CitationBuilder.from_retrieved_chunk (dataclass-like object, 필수 속성 누락)
  - ConversationTurn.to_message_pair (rewritten vs original query)
  - CitationCache.verify_ownership (신규/매칭/불일치/owner 없음)
  - CitationCache.add_turn + get_history + get_citations + get_turn_number + clear
  - CitationCache 슬라이딩 윈도우 (10턴 초과 제거)
  - get_citation_cache 싱글톤
"""
from __future__ import annotations

import uuid as uuidlib
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


DOC_UUID = uuidlib.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
VER_UUID = uuidlib.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
NODE_UUID = uuidlib.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
CONV_UUID = uuidlib.UUID("11111111-1111-1111-1111-111111111111")
CONV_UUID_B = uuidlib.UUID("22222222-2222-2222-2222-222222222222")


# --------------------------------------------------------------------------- #
# 1) CitationBuilder.build
# --------------------------------------------------------------------------- #


class TestCitationBuilderBuild:
    def test_happy_path(self):
        from app.services.retrieval.citation_builder import CitationBuilder

        c = CitationBuilder.build(
            document_id=DOC_UUID,
            version_id=VER_UUID,
            node_id=NODE_UUID,
            source_text="본문 텍스트",
        )
        assert c.document_id == DOC_UUID
        assert c.version_id == VER_UUID
        assert c.node_id == NODE_UUID
        # content_hash 는 SHA-256 (16진수 64자)
        assert len(c.content_hash) == 64

    def test_node_id_none_replaced_with_nil_uuid(self):
        from app.services.retrieval.citation_builder import CitationBuilder

        c = CitationBuilder.build(
            document_id=DOC_UUID, version_id=VER_UUID,
            node_id=None, source_text="text",
        )
        assert c.node_id == uuidlib.UUID(int=0)

    def test_empty_source_raises_value_error(self):
        from app.services.retrieval.citation_builder import CitationBuilder

        with pytest.raises(ValueError, match="source_text"):
            CitationBuilder.build(
                document_id=DOC_UUID, version_id=VER_UUID,
                node_id=NODE_UUID, source_text="",
            )

    def test_invalid_node_id_raises_value_error(self):
        from app.services.retrieval.citation_builder import CitationBuilder

        with pytest.raises(ValueError, match="UUID"):
            CitationBuilder.build(
                document_id=DOC_UUID, version_id=VER_UUID,
                node_id="not-a-uuid",
                source_text="text",
            )

    def test_string_uuids_accepted(self):
        from app.services.retrieval.citation_builder import CitationBuilder

        c = CitationBuilder.build(
            document_id=str(DOC_UUID),
            version_id=str(VER_UUID),
            node_id=str(NODE_UUID),
            source_text="text",
        )
        assert str(c.document_id) == str(DOC_UUID)

    def test_span_offset_passed_through(self):
        from app.services.retrieval.citation_builder import CitationBuilder

        c = CitationBuilder.build(
            document_id=DOC_UUID, version_id=VER_UUID,
            node_id=NODE_UUID, source_text="x",
            span_offset=42,
        )
        assert c.span_offset == 42


# --------------------------------------------------------------------------- #
# 2) CitationBuilder.from_retrieved_chunk
# --------------------------------------------------------------------------- #


class TestFromRetrievedChunk:
    def test_happy_path_with_node_id(self):
        from app.services.retrieval.citation_builder import CitationBuilder

        chunk = SimpleNamespace(
            document_id=DOC_UUID, version_id=VER_UUID,
            node_id=NODE_UUID, source_text="chunk text",
        )
        c = CitationBuilder.from_retrieved_chunk(chunk)
        assert c.node_id == NODE_UUID

    def test_none_node_id_logs_and_uses_nil(self):
        from app.services.retrieval.citation_builder import CitationBuilder

        chunk = SimpleNamespace(
            document_id=DOC_UUID, version_id=VER_UUID,
            node_id=None, source_text="chunk text",
        )
        c = CitationBuilder.from_retrieved_chunk(chunk)
        assert c.node_id == uuidlib.UUID(int=0)

    def test_missing_required_attribute_raises(self):
        from app.services.retrieval.citation_builder import CitationBuilder

        # document_id 없는 청크
        chunk = SimpleNamespace(version_id=VER_UUID, source_text="t")
        with pytest.raises(ValueError, match="document_id"):
            CitationBuilder.from_retrieved_chunk(chunk)

    def test_empty_source_text_propagates_value_error(self):
        from app.services.retrieval.citation_builder import CitationBuilder

        chunk = SimpleNamespace(
            document_id=DOC_UUID, version_id=VER_UUID,
            node_id=NODE_UUID, source_text="",
        )
        with pytest.raises(ValueError):
            CitationBuilder.from_retrieved_chunk(chunk)


# --------------------------------------------------------------------------- #
# 3) ConversationTurn
# --------------------------------------------------------------------------- #


class TestConversationTurn:
    def test_to_message_pair_uses_rewritten_when_present(self):
        from app.services.retrieval.citation_cache import ConversationTurn
        from app.schemas.conversation import MessageRole

        t = ConversationTurn(
            turn_number=3,
            query="원본 질의",
            rewritten_query="재작성된 질의",
            citations=[],
            answer="답변",
        )
        messages = t.to_message_pair()
        assert len(messages) == 2
        assert messages[0].role == MessageRole.USER
        assert messages[0].content == "재작성된 질의"
        assert messages[0].turn_number == 3
        assert messages[1].role == MessageRole.ASSISTANT
        assert messages[1].content == "답변"

    def test_to_message_pair_falls_back_to_original_query(self):
        from app.services.retrieval.citation_cache import ConversationTurn

        t = ConversationTurn(
            turn_number=1, query="Q", rewritten_query=None,
            citations=[], answer="A",
        )
        messages = t.to_message_pair()
        assert messages[0].content == "Q"


# --------------------------------------------------------------------------- #
# 4) CitationCache
# --------------------------------------------------------------------------- #


class TestCitationCacheOwnership:
    def test_new_conversation_allows_any_actor(self):
        from app.services.retrieval.citation_cache import CitationCache
        cache = CitationCache()
        assert cache.verify_ownership(CONV_UUID, "any-user") is True

    def test_matching_owner_returns_true(self):
        from app.services.retrieval.citation_cache import (
            CitationCache, ConversationTurn,
        )
        cache = CitationCache()
        cache.add_turn(
            CONV_UUID,
            ConversationTurn(turn_number=1, query="Q", rewritten_query=None,
                              citations=[], answer="A"),
            actor_id="user-1",
        )
        assert cache.verify_ownership(CONV_UUID, "user-1") is True

    def test_mismatched_owner_returns_false(self):
        from app.services.retrieval.citation_cache import (
            CitationCache, ConversationTurn,
        )
        cache = CitationCache()
        cache.add_turn(
            CONV_UUID,
            ConversationTurn(turn_number=1, query="Q", rewritten_query=None,
                              citations=[], answer="A"),
            actor_id="user-1",
        )
        assert cache.verify_ownership(CONV_UUID, "attacker") is False

    def test_legacy_none_owner_allows_access(self):
        """소유자 기록 없는 레거시 항목은 모든 접근 허용 (하위호환)."""
        from app.services.retrieval.citation_cache import (
            CitationCache, ConversationTurn,
        )
        cache = CitationCache()
        cache.add_turn(
            CONV_UUID,
            ConversationTurn(turn_number=1, query="Q", rewritten_query=None,
                              citations=[], answer="A"),
            actor_id=None,
        )
        assert cache.verify_ownership(CONV_UUID, "any-user") is True


class TestCitationCacheOperations:
    def test_add_turn_and_get_history(self):
        from app.services.retrieval.citation_cache import (
            CitationCache, ConversationTurn,
        )
        cache = CitationCache()
        cache.add_turn(
            CONV_UUID,
            ConversationTurn(turn_number=1, query="Q1", rewritten_query=None,
                              citations=[], answer="A1"),
        )
        cache.add_turn(
            CONV_UUID,
            ConversationTurn(turn_number=2, query="Q2", rewritten_query="R2",
                              citations=[], answer="A2"),
        )
        history = cache.get_history(CONV_UUID)
        # 2 턴 × 2 메시지 = 4
        assert len(history) == 4
        # 두 번째 턴의 user 메시지는 rewritten 사용
        assert history[2].content == "R2"

    def test_get_citations_aggregates_all_turns(self):
        from app.schemas.citation import Citation
        from app.services.retrieval.citation_cache import (
            CitationCache, ConversationTurn,
        )
        cache = CitationCache()
        cit1 = Citation.from_chunk(
            document_id=DOC_UUID, version_id=VER_UUID,
            node_id=NODE_UUID, source_text="a",
        )
        cit2 = Citation.from_chunk(
            document_id=DOC_UUID, version_id=VER_UUID,
            node_id=NODE_UUID, source_text="b",
        )
        cache.add_turn(
            CONV_UUID,
            ConversationTurn(1, "Q1", None, [cit1], "A1"),
        )
        cache.add_turn(
            CONV_UUID,
            ConversationTurn(2, "Q2", None, [cit2], "A2"),
        )
        all_citations = cache.get_citations(CONV_UUID)
        assert len(all_citations) == 2

    def test_get_turn_number_returns_next(self):
        from app.services.retrieval.citation_cache import (
            CitationCache, ConversationTurn,
        )
        cache = CitationCache()
        assert cache.get_turn_number(CONV_UUID) == 1  # 빈 상태
        cache.add_turn(CONV_UUID, ConversationTurn(1, "Q", None, [], "A"))
        assert cache.get_turn_number(CONV_UUID) == 2

    def test_sliding_window_keeps_last_10_turns(self):
        from app.services.retrieval.citation_cache import (
            CitationCache, ConversationTurn,
        )
        cache = CitationCache()
        for i in range(15):
            cache.add_turn(
                CONV_UUID,
                ConversationTurn(
                    turn_number=i + 1, query=f"Q{i}", rewritten_query=None,
                    citations=[], answer=f"A{i}",
                ),
            )
        history = cache.get_history(CONV_UUID)
        # 10 턴 × 2 메시지 = 20
        assert len(history) == 20
        # 가장 첫 메시지는 5번째 턴의 것 (오래된 5개 제거됨)
        # turn_number 들은 6 ~ 15 까지
        first_user = history[0]
        assert first_user.turn_number == 6

    def test_clear_removes_conversation(self):
        from app.services.retrieval.citation_cache import (
            CitationCache, ConversationTurn,
        )
        cache = CitationCache()
        cache.add_turn(
            CONV_UUID, ConversationTurn(1, "Q", None, [], "A"), actor_id="u",
        )
        cache.clear(CONV_UUID)
        assert cache.get_turn_number(CONV_UUID) == 1
        # owner 도 제거 — 다시 다른 actor 접근 가능
        assert cache.verify_ownership(CONV_UUID, "another") is True


class TestCitationCacheSingleton:
    def test_get_citation_cache_returns_same_instance(self):
        from app.services.retrieval.citation_cache import get_citation_cache

        a = get_citation_cache()
        b = get_citation_cache()
        assert a is b
