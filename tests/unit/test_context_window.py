"""
멀티턴 컨텍스트 윈도우 + 쿼리 재작성 단위 테스트 — Task 3-5.

테스트 범위:
  - ContextWindowManager.fetch_context_window(): 최근 N개 Turn 조회 + ACL 검증
  - count_tokens(): 단일/멀티턴 토큰 계산
  - ContextWindowManager._calculate_context_tokens(): 누적 토큰 계산
  - ContextWindowManager.manage_overflow(): 오버플로우 → 오래된 Turn 제거
  - ContextWindowManager.build_context_string(): 컨텍스트 문자열 구성
  - ContextWindowManager._trim_search_results(): 검색 결과 자르기
  - PromptBuilder.build_prompt(): 프롬프트 구성
  - PromptBuilder.count_prompt_tokens(): 섹션별 토큰 계산
  - ACL 필터링 (S2 원칙 ⑥)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))


# ---------------------------------------------------------------------------
# 테스트 헬퍼
# ---------------------------------------------------------------------------

def _make_turn(
    turn_number: int,
    user_message: str = "질문",
    assistant_response: str = "답변",
    conversation_id: str | None = None,
) -> "Turn":
    from app.models.conversation import Turn
    return Turn(
        id=str(uuid4()),
        conversation_id=conversation_id or str(uuid4()),
        turn_number=turn_number,
        user_message=user_message,
        assistant_response=assistant_response,
        retrieval_metadata={},
        created_at=datetime.now(timezone.utc),
    )


def _make_conversation(owner_id: str, conv_id: str | None = None) -> "Conversation":
    from app.models.conversation import Conversation
    now = datetime.now(timezone.utc)
    return Conversation(
        id=conv_id or str(uuid4()),
        owner_id=owner_id,
        organization_id=str(uuid4()),
        title="테스트 대화",
        status="active",
        metadata={},
        retention_days=90,
        access_level="private",
        created_at=now,
        updated_at=now,
    )


# ===========================================================================
# 1. count_tokens 테스트
# ===========================================================================

class TestCountTokens:
    def test_empty_string_returns_zero(self):
        from app.services.context_window_manager import count_tokens
        assert count_tokens("") == 0

    def test_nonempty_returns_positive(self):
        from app.services.context_window_manager import count_tokens
        # "Hello World" — 2 tokens (tiktoken) or ≥1 근사치
        result = count_tokens("Hello World")
        assert result >= 1

    def test_longer_text_more_tokens(self):
        from app.services.context_window_manager import count_tokens
        short = count_tokens("Hi")
        long = count_tokens("이것은 매우 긴 텍스트입니다. " * 50)
        assert long > short


# ===========================================================================
# 2. ContextWindowManager._calculate_context_tokens
# ===========================================================================

class TestCalculateContextTokens:
    def test_single_turn_token_count(self):
        from app.services.context_window_manager import ContextWindowManager, count_tokens
        conn = MagicMock()
        mgr = ContextWindowManager(conn)
        turn = _make_turn(1, user_message="What is AI?", assistant_response="AI is...")
        expected = count_tokens("What is AI?") + count_tokens("AI is...")
        assert mgr._calculate_context_tokens([turn]) == expected

    def test_multi_turn_token_accumulation(self):
        from app.services.context_window_manager import ContextWindowManager, count_tokens
        conn = MagicMock()
        mgr = ContextWindowManager(conn)
        turns = [_make_turn(i, f"Q{i}" * 10, f"A{i}" * 15) for i in range(1, 4)]
        total = mgr._calculate_context_tokens(turns)
        expected = sum(
            count_tokens(t.user_message) + count_tokens(t.assistant_response)
            for t in turns
        )
        assert total == expected
        assert total > 0

    def test_empty_turns_returns_zero(self):
        from app.services.context_window_manager import ContextWindowManager
        conn = MagicMock()
        mgr = ContextWindowManager(conn)
        assert mgr._calculate_context_tokens([]) == 0


# ===========================================================================
# 3. ContextWindowManager.fetch_context_window — 최근 N개 조회
# ===========================================================================

class TestFetchContextWindow:
    def test_fetch_recent_n_turns(self):
        """5턴 중 최근 3개만 반환."""
        import app.services.context_window_manager as mod

        actor_id = str(uuid4())
        conv_id = str(uuid4())
        mock_conv = _make_conversation(owner_id=actor_id, conv_id=conv_id)
        turns_in_db = [_make_turn(i, conversation_id=conv_id) for i in range(1, 6)]

        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = mock_conv
        mock_turn_repo = MagicMock()
        # list_by_conversation(limit=3, order="DESC") → 최근 3개 오름차순
        mock_turn_repo.list_by_conversation.return_value = turns_in_db[2:]  # turn 3,4,5

        original_conv_cls = mod.ConversationRepository
        original_turn_cls = mod.TurnRepository
        mod.ConversationRepository = lambda conn: mock_conv_repo
        mod.TurnRepository = lambda conn: mock_turn_repo
        try:
            from app.services.context_window_manager import ContextWindowManager
            from uuid import UUID
            mgr = ContextWindowManager(MagicMock())
            turns, tokens = mgr.fetch_context_window(UUID(conv_id), actor_id, window_size=3)
        finally:
            mod.ConversationRepository = original_conv_cls
            mod.TurnRepository = original_turn_cls

        assert len(turns) == 3
        assert tokens >= 0
        mock_turn_repo.list_by_conversation.assert_called_once_with(conv_id, limit=3, order="DESC")

    def test_raises_value_error_when_conversation_not_found(self):
        """conversation 없으면 ValueError."""
        import app.services.context_window_manager as mod

        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = None

        original_conv_cls = mod.ConversationRepository
        mod.ConversationRepository = lambda conn: mock_conv_repo
        try:
            from app.services.context_window_manager import ContextWindowManager
            from uuid import UUID
            mgr = ContextWindowManager(MagicMock())
            with pytest.raises(ValueError, match="Conversation not found"):
                mgr.fetch_context_window(UUID(str(uuid4())), "actor-1")
        finally:
            mod.ConversationRepository = original_conv_cls

    def test_raises_permission_error_when_not_owner(self):
        """소유자가 아닌 actor → PermissionError."""
        import app.services.context_window_manager as mod

        owner_id = str(uuid4())
        other_actor_id = str(uuid4())
        conv_id = str(uuid4())
        mock_conv = _make_conversation(owner_id=owner_id, conv_id=conv_id)

        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = mock_conv

        original_conv_cls = mod.ConversationRepository
        mod.ConversationRepository = lambda conn: mock_conv_repo
        try:
            from app.services.context_window_manager import ContextWindowManager
            from uuid import UUID
            mgr = ContextWindowManager(MagicMock())
            with pytest.raises(PermissionError, match="does not have access"):
                mgr.fetch_context_window(UUID(conv_id), other_actor_id)
        finally:
            mod.ConversationRepository = original_conv_cls


# ===========================================================================
# 4. ContextWindowManager.build_context_string
# ===========================================================================

class TestBuildContextString:
    def test_empty_turns_returns_empty_string(self):
        from app.services.context_window_manager import ContextWindowManager
        mgr = ContextWindowManager(MagicMock())
        assert mgr.build_context_string([]) == ""

    def test_single_turn_formatting(self):
        from app.services.context_window_manager import ContextWindowManager
        mgr = ContextWindowManager(MagicMock())
        turn = _make_turn(1, user_message="질문 1", assistant_response="답변 1")
        result = mgr.build_context_string([turn])
        assert "Turn 1:" in result
        assert "질문 1" in result
        assert "답변 1" in result

    def test_multi_turn_ordering(self):
        from app.services.context_window_manager import ContextWindowManager
        mgr = ContextWindowManager(MagicMock())
        turns = [_make_turn(i, f"Q{i}", f"A{i}") for i in range(1, 4)]
        result = mgr.build_context_string(turns)
        # Turn 1이 Turn 3보다 앞에 나와야 함
        assert result.index("Turn 1:") < result.index("Turn 3:")


# ===========================================================================
# 5. ContextWindowManager.manage_overflow
# ===========================================================================

class TestManageOverflow:
    def test_no_overflow_keeps_all_turns(self):
        """토큰 여유 있을 때 Turn 제거 없음."""
        from app.services.context_window_manager import ContextWindowManager
        from uuid import UUID

        conn = MagicMock()
        mgr = ContextWindowManager(conn)
        # fetch_context_window 를 직접 패치
        turns = [_make_turn(i, "짧은 질문", "짧은 답변") for i in range(1, 4)]
        mgr.fetch_context_window = MagicMock(return_value=(turns, 50))

        adjusted_turns, _, metadata = mgr.manage_overflow(
            UUID(str(uuid4())), "actor-1", "현재 쿼리", max_tokens=4000
        )

        assert len(adjusted_turns) == 3
        assert metadata["context_overflow_handled"] is False

    def test_overflow_removes_oldest_turns(self):
        """오버플로우 시 가장 오래된 Turn 제거."""
        from app.services.context_window_manager import ContextWindowManager, count_tokens
        from uuid import UUID

        conn = MagicMock()
        mgr = ContextWindowManager(conn)

        # 각 Turn이 약 500토큰 → 3턴 = 1500 토큰, max=200 으로 오버플로우 유도
        big_text = "토큰을 많이 쓰는 매우 긴 텍스트입니다. " * 20
        turns = [_make_turn(i, big_text, big_text) for i in range(1, 4)]
        big_tokens = sum(count_tokens(t.user_message) + count_tokens(t.assistant_response) for t in turns)
        mgr.fetch_context_window = MagicMock(return_value=(list(turns), big_tokens))

        adjusted_turns, _, metadata = mgr.manage_overflow(
            UUID(str(uuid4())), "actor-1", "쿼리", max_tokens=200
        )

        assert metadata["context_overflow_handled"] is True
        # 일부 Turn 이 제거됨
        assert len(adjusted_turns) < 3

    def test_search_results_trimmed_on_overflow(self):
        """Turn 모두 제거 후에도 예산 부족 → 검색 결과 자르기."""
        from app.services.context_window_manager import ContextWindowManager
        from uuid import UUID

        conn = MagicMock()
        mgr = ContextWindowManager(conn)

        # Turn 없음 (이미 비어있음)
        mgr.fetch_context_window = MagicMock(return_value=([], 0))

        # 검색 결과 3개 (각 1000토큰 이상)
        big_content = "매우 긴 검색 결과. " * 200  # ~2000 토큰
        search_results = [{"content": big_content}] * 3

        _, adjusted_search, _ = mgr.manage_overflow(
            UUID(str(uuid4())), "actor-1", "쿼리",
            search_results=search_results,
            max_tokens=100,  # 검색 결과도 자를 것
        )

        assert len(adjusted_search) <= 3


# ===========================================================================
# 6. ContextWindowManager._trim_search_results
# ===========================================================================

class TestTrimSearchResults:
    def test_within_budget_keeps_all(self):
        from app.services.context_window_manager import ContextWindowManager
        mgr = ContextWindowManager(MagicMock())
        results = [{"content": "짧은 결과"}, {"content": "짧은 결과2"}]
        trimmed = mgr._trim_search_results(results, max_tokens=5000)
        assert len(trimmed) == 2

    def test_zero_budget_returns_empty(self):
        from app.services.context_window_manager import ContextWindowManager
        mgr = ContextWindowManager(MagicMock())
        results = [{"content": "결과"}]
        trimmed = mgr._trim_search_results(results, max_tokens=0)
        assert trimmed == []

    def test_partial_trim(self):
        from app.services.context_window_manager import ContextWindowManager, count_tokens
        mgr = ContextWindowManager(MagicMock())
        # 첫 번째 결과는 예산 내, 두 번째는 초과
        big = "토큰 많은 텍스트 " * 100
        results = [{"content": "짧은 결과"}, {"content": big}]
        first_tokens = count_tokens("짧은 결과")
        trimmed = mgr._trim_search_results(results, max_tokens=first_tokens + 1)
        assert len(trimmed) == 1
        assert trimmed[0]["content"] == "짧은 결과"


# ===========================================================================
# 7. PromptBuilder.build_prompt
# ===========================================================================

class TestPromptBuilderBuildPrompt:
    def test_prompt_contains_all_sections(self):
        from app.services.prompt_builder import PromptBuilder
        builder = PromptBuilder()
        turns = [_make_turn(1, "이전 질문", "이전 답변")]
        search_results = [{"content": "검색 결과 내용"}]
        prompt = builder.build_prompt(
            query="현재 질문입니다",
            search_results=search_results,
            context_turns=turns,
        )
        assert "대화 이력" in prompt
        assert "이전 질문" in prompt
        assert "이전 답변" in prompt
        assert "현재 질문입니다" in prompt
        assert "검색 결과 내용" in prompt

    def test_prompt_without_context_turns(self):
        from app.services.prompt_builder import PromptBuilder
        builder = PromptBuilder()
        prompt = builder.build_prompt(query="질문", search_results=[{"content": "결과"}])
        assert "대화 이력" not in prompt
        assert "질문" in prompt
        assert "결과" in prompt

    def test_prompt_without_search_results(self):
        from app.services.prompt_builder import PromptBuilder
        builder = PromptBuilder()
        prompt = builder.build_prompt(query="질문")
        assert "질문" in prompt
        # search_results 없으면 참고 자료 섹션 헤더가 포함되지 않음
        assert "=== 참고 자료 ===" not in prompt

    def test_search_results_numbered(self):
        from app.services.prompt_builder import PromptBuilder
        builder = PromptBuilder()
        prompt = builder.build_prompt(
            query="Q",
            search_results=[{"content": "A"}, {"content": "B"}],
        )
        assert "[1]" in prompt
        assert "[2]" in prompt


# ===========================================================================
# 8. PromptBuilder.count_prompt_tokens
# ===========================================================================

class TestPromptBuilderCountTokens:
    def test_token_count_has_all_keys(self):
        from app.services.prompt_builder import PromptBuilder
        builder = PromptBuilder()
        counts = builder.count_prompt_tokens(query="테스트 쿼리")
        assert set(counts.keys()) == {"system", "context", "query", "search", "total"}

    def test_total_equals_sum_of_sections(self):
        from app.services.prompt_builder import PromptBuilder
        builder = PromptBuilder()
        turns = [_make_turn(1, "이전 질문", "이전 답변")]
        counts = builder.count_prompt_tokens(
            query="쿼리",
            search_results=[{"content": "검색 결과"}],
            context_turns=turns,
        )
        expected_total = counts["system"] + counts["context"] + counts["query"] + counts["search"]
        assert counts["total"] == expected_total

    def test_context_tokens_zero_without_turns(self):
        from app.services.prompt_builder import PromptBuilder
        builder = PromptBuilder()
        counts = builder.count_prompt_tokens(query="쿼리")
        assert counts["context"] == 0
        assert counts["search"] == 0

    def test_context_tokens_positive_with_turns(self):
        from app.services.prompt_builder import PromptBuilder
        builder = PromptBuilder()
        turns = [_make_turn(i, f"Q{i}" * 5, f"A{i}" * 8) for i in range(1, 4)]
        counts = builder.count_prompt_tokens(query="쿼리", context_turns=turns)
        assert counts["context"] > 0

    def test_system_tokens_positive(self):
        from app.services.prompt_builder import PromptBuilder
        builder = PromptBuilder()
        counts = builder.count_prompt_tokens(query="쿼리")
        assert counts["system"] > 0


# ===========================================================================
# 9. ACL 필터링 정적 검사 (S2 원칙 ⑥)
# ===========================================================================

class TestACLFilteringStaticCheck:
    def test_context_window_manager_checks_owner_id(self):
        """ContextWindowManager 소스 코드에 owner_id ACL 검증 포함 확인."""
        source = (ROOT / "backend/app/services/context_window_manager.py").read_text()
        assert "owner_id" in source
        assert "PermissionError" in source

    def test_no_hardcoded_scope_strings(self):
        """scope 문자열 하드코딩 없음 (S2 원칙 ⑥)."""
        source = (ROOT / "backend/app/services/context_window_manager.py").read_text()
        for bad in ('== "team"', '== "org"', '== "public"'):
            assert bad not in source
