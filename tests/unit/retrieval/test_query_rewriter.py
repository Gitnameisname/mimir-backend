"""
Task 2-7: QueryRewriter + ConversationCompressor + PromptRegistry 단위 테스트
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.schemas.conversation import ConversationMessage, MessageRole
from app.services.retrieval.query_rewriter import QueryRewriter, _DEFAULT_REWRITE_PROMPT
from app.services.retrieval.conversation_compressor import ConversationCompressor, _DEFAULT_SUMMARY_PROMPT


# ── 헬퍼 ────────────────────────────────────────────────────────────────────

def _msg(role: str, content: str, turn: int = 0) -> ConversationMessage:
    return ConversationMessage(role=MessageRole(role), content=content, turn_number=turn)


def _mock_llm(content: str = "재작성된 쿼리") -> MagicMock:
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=(content, 10))
    return llm


# ── ConversationMessage 스키마 ───────────────────────────────────────────────

def test_conversation_message_role_enum():
    msg = _msg("user", "안녕하세요")
    assert msg.role == MessageRole.USER
    assert msg.content == "안녕하세요"
    assert msg.turn_number == 0


def test_conversation_message_all_roles():
    for role in ("user", "assistant", "system"):
        msg = _msg(role, "test")
        assert msg.role.value == role


def test_conversation_message_invalid_role_raises():
    with pytest.raises(Exception):
        ConversationMessage(role="unknown_role", content="test")


# ── QueryRewriter — 빈 이력 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rewrite_returns_original_when_no_history():
    """대화 이력 없으면 원본 쿼리 반환 (LLM 미호출)."""
    llm = MagicMock()
    rewriter = QueryRewriter(llm)
    result = await rewriter.rewrite_query("테스트 쿼리", [])
    assert result == "테스트 쿼리"
    llm.complete.assert_not_called()


# ── QueryRewriter — 정상 재작성 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rewrite_calls_llm_with_history():
    """대화 이력 있으면 LLM 호출 후 재작성된 쿼리 반환."""
    llm = _mock_llm("Kubernetes 배포 전략은?")
    history = [
        _msg("user", "Kubernetes에 대해 알려줘"),
        _msg("assistant", "Kubernetes는 컨테이너 오케스트레이션 플랫폼입니다."),
    ]
    rewriter = QueryRewriter(llm)
    result = await rewriter.rewrite_query("이게 뭐야?", history)
    assert result == "Kubernetes 배포 전략은?"
    llm.complete.assert_called_once()


@pytest.mark.asyncio
async def test_rewrite_llm_called_with_correct_messages():
    """LLM에 올바른 메시지 형식으로 전달되어야 한다."""
    llm = _mock_llm("재작성됨")
    history = [_msg("user", "이전 내용")]
    rewriter = QueryRewriter(llm)
    await rewriter.rewrite_query("원본 쿼리", history)

    call_kwargs = llm.complete.call_args
    assert "system_prompt" in call_kwargs.kwargs or len(call_kwargs.args) >= 1
    messages = call_kwargs.kwargs.get("messages") or call_kwargs.args[1]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"


# ── QueryRewriter — LLM 폴백 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rewrite_fallback_on_llm_error():
    """LLM 오류 시 원본 쿼리 반환 (예외 미발생)."""
    llm = MagicMock()
    llm.complete = AsyncMock(side_effect=Exception("LLM unavailable"))
    history = [_msg("user", "이전 대화")]
    rewriter = QueryRewriter(llm)
    result = await rewriter.rewrite_query("원본 쿼리", history)
    assert result == "원본 쿼리"


@pytest.mark.asyncio
async def test_rewrite_fallback_on_empty_llm_response():
    """LLM이 빈 응답 반환 시 원본 쿼리 반환."""
    llm = _mock_llm("")  # 빈 content
    history = [_msg("user", "이전 대화")]
    rewriter = QueryRewriter(llm)
    result = await rewriter.rewrite_query("원본 쿼리", history)
    assert result == "원본 쿼리"


# ── QueryRewriter — Prompt Registry 통합 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_rewrite_uses_registry_template():
    """Prompt Registry에서 커스텀 템플릿을 로드해야 한다."""
    from app.services.prompt.registry import PromptRegistry
    registry = PromptRegistry.__new__(PromptRegistry)
    registry._cache = {
        "query_rewrite_standalone": "Custom: {original_query} History: {conversation_history}"
    }

    llm = _mock_llm("커스텀 재작성")
    history = [_msg("user", "이전")]
    rewriter = QueryRewriter(llm, prompt_registry=registry)
    await rewriter.rewrite_query("쿼리", history)

    # LLM에 커스텀 프롬프트 내용이 전달되어야 함
    messages_arg = llm.complete.call_args.kwargs.get("messages") or llm.complete.call_args.args[1]
    assert "Custom:" in messages_arg[0]["content"]


@pytest.mark.asyncio
async def test_rewrite_falls_back_to_default_when_registry_has_no_key():
    """Registry에 키 없으면 기본 프롬프트 사용."""
    from app.services.prompt.registry import PromptRegistry
    registry = PromptRegistry.__new__(PromptRegistry)
    registry._cache = {}

    llm = _mock_llm("기본 재작성")
    history = [_msg("user", "이전")]
    rewriter = QueryRewriter(llm, prompt_registry=registry)
    await rewriter.rewrite_query("쿼리", history)

    messages_arg = llm.complete.call_args.kwargs.get("messages") or llm.complete.call_args.args[1]
    # 기본 프롬프트의 일부가 포함되어야 함
    assert "검색 질의" in messages_arg[0]["content"] or "쿼리" in messages_arg[0]["content"]


# ── ConversationCompressor — 슬라이딩 윈도우 ─────────────────────────────────

@pytest.mark.asyncio
async def test_compressor_sliding_window_keeps_recent():
    """슬라이딩 윈도우: 최근 N개 메시지만 포함해야 한다."""
    compressor = ConversationCompressor(window_size=2)
    messages = [_msg("user", f"메시지 {i}") for i in range(5)]
    result = await compressor.compress(messages, strategy="sliding_window")
    assert "메시지 4" in result
    assert "메시지 3" in result
    assert "메시지 0" not in result
    assert "메시지 1" not in result


@pytest.mark.asyncio
async def test_compressor_sliding_window_empty():
    """빈 이력 → 빈 문자열 반환."""
    compressor = ConversationCompressor()
    result = await compressor.compress([], strategy="sliding_window")
    assert result == ""


@pytest.mark.asyncio
async def test_compressor_sliding_window_fewer_than_window():
    """메시지 수 < window_size이면 전부 포함해야 한다."""
    compressor = ConversationCompressor(window_size=10)
    messages = [_msg("user", f"msg {i}") for i in range(3)]
    result = await compressor.compress(messages, strategy="sliding_window")
    assert "msg 0" in result
    assert "msg 2" in result


# ── ConversationCompressor — LLM 요약 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_compressor_llm_summarize_calls_llm():
    """LLM 요약 전략: LLM을 호출해야 한다."""
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=("요약된 내용", 20))
    compressor = ConversationCompressor(llm=llm, window_size=5)
    messages = [_msg("user", "내용")]
    result = await compressor.compress(messages, strategy="summarize")
    assert result == "요약된 내용"
    llm.complete.assert_called_once()


@pytest.mark.asyncio
async def test_compressor_fallback_on_llm_error():
    """LLM 요약 실패 시 슬라이딩 윈도우로 폴백."""
    llm = MagicMock()
    llm.complete = AsyncMock(side_effect=Exception("LLM error"))
    compressor = ConversationCompressor(llm=llm, window_size=3)
    messages = [_msg("user", f"msg {i}") for i in range(5)]
    result = await compressor.compress(messages, strategy="summarize")
    # 폴백: 최근 3개 포함
    assert "msg 4" in result
    assert "msg 3" in result
    assert "msg 2" in result
    assert "msg 0" not in result


@pytest.mark.asyncio
async def test_compressor_summarize_without_llm_uses_sliding_window():
    """LLM 없이 summarize 전략 호출 시 슬라이딩 윈도우 사용."""
    compressor = ConversationCompressor(llm=None, window_size=2)
    messages = [_msg("user", f"msg {i}") for i in range(5)]
    result = await compressor.compress(messages, strategy="summarize")
    assert "msg 4" in result
    assert "msg 0" not in result


# ── PromptRegistry — 시드 로드 ─────────────────────────────────────────────

def test_prompt_registry_loads_seeds():
    """시드 JSON 파일이 정상 로드되어야 한다."""
    from app.services.prompt.registry import PromptRegistry
    registry = PromptRegistry()
    # 두 시드 파일이 모두 등록되어야 함
    assert registry.get("query_rewrite_standalone") is not None
    assert registry.get("conversation_summary") is not None


def test_prompt_registry_missing_key_returns_none():
    """없는 키는 None을 반환해야 한다."""
    from app.services.prompt.registry import PromptRegistry
    registry = PromptRegistry()
    assert registry.get("nonexistent_key_xyz") is None


def test_prompt_registry_singleton():
    """instance()는 동일 객체를 반환해야 한다."""
    from app.services.prompt.registry import PromptRegistry
    PromptRegistry._instance = None  # 초기화
    r1 = PromptRegistry.instance()
    r2 = PromptRegistry.instance()
    assert r1 is r2


def test_prompt_registry_register_overrides():
    """register()로 템플릿을 재정의할 수 있어야 한다."""
    from app.services.prompt.registry import PromptRegistry
    registry = PromptRegistry()
    registry.register("test_key", "테스트 템플릿 {var}")
    assert registry.get("test_key") == "테스트 템플릿 {var}"


def test_prompt_registry_query_rewrite_template_has_variables():
    """query_rewrite_standalone 템플릿에 필요한 변수가 있어야 한다."""
    from app.services.prompt.registry import PromptRegistry
    registry = PromptRegistry()
    template = registry.get("query_rewrite_standalone")
    assert "{original_query}" in template
    assert "{conversation_history}" in template


def test_prompt_registry_summary_template_has_variables():
    """conversation_summary 템플릿에 필요한 변수가 있어야 한다."""
    from app.services.prompt.registry import PromptRegistry
    registry = PromptRegistry()
    template = registry.get("conversation_summary")
    assert "{conversation_text}" in template
