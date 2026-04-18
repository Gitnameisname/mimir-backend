"""ConversationCompressor — 대화 이력 압축.

두 가지 전략:
  1. 슬라이딩 윈도우: 최근 N턴만 유지 (빠름, LLM 불필요)
  2. LLM 요약: 전체 대화를 짧은 텍스트로 압축 (느림, 고품질)

LLM 실패 시 슬라이딩 윈도우로 폴백 (S2 원칙).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional

from app.schemas.conversation import ConversationMessage

if TYPE_CHECKING:
    from app.services.prompt.registry import PromptRegistry

logger = logging.getLogger(__name__)

_SUMMARY_PROMPT_KEY = "conversation_summary"

_DEFAULT_SUMMARY_PROMPT = (
    "다음 대화를 300자 이내로 핵심 내용만 요약하세요.\n"
    "검색에 유용한 주요 키워드와 주제를 반드시 포함하세요.\n\n"
    "대화:\n{conversation_text}\n\n"
    "요약:"
)


class ConversationCompressor:
    """대화 이력을 압축하여 컨텍스트 토큰 비용을 줄인다.

    Args:
        llm: LLMProvider 인스턴스 (선택, 없으면 슬라이딩 윈도우만 사용)
        prompt_registry: PromptRegistry 인스턴스 (선택)
        window_size: 슬라이딩 윈도우 크기 (유지할 최근 메시지 수)
    """

    def __init__(
        self,
        llm=None,
        prompt_registry: Optional["PromptRegistry"] = None,
        window_size: int = 10,
    ) -> None:
        self._llm = llm
        self._prompt_registry = prompt_registry
        self._window_size = window_size

    async def compress(
        self,
        messages: List[ConversationMessage],
        max_tokens: int = 1000,
        strategy: str = "sliding_window",
    ) -> str:
        """대화 이력을 압축하여 문자열로 반환한다.

        Args:
            messages: 전체 대화 메시지 리스트
            max_tokens: 최대 허용 토큰 수 (LLM 요약 시 목표, 현재는 참고용)
            strategy: "sliding_window" | "summarize"

        Returns:
            압축된 대화 컨텍스트 문자열
        """
        if not messages:
            return ""

        if strategy == "summarize" and self._llm is not None:
            return await self._llm_summarize(messages)
        return self._sliding_window(messages)

    # 메시지 하나당 최대 길이 (프롬프트 인젝션 / 토큰 비용 제한)
    _MAX_MSG_LEN = 500

    def _sliding_window(self, messages: List[ConversationMessage]) -> str:
        """최근 window_size개 메시지만 유지한다.

        보안:
          - 각 메시지를 명시적 구분자로 감싸서 컨텐츠와 지시문을 분리한다 (LLM01 완화).
          - 개별 메시지를 _MAX_MSG_LEN 자로 잘라 프롬프트 팽창을 방지한다.
        """
        recent = messages[-self._window_size:]
        lines = ["=== 대화 이력 시작 (참고용) ==="]
        for msg in recent:
            prefix = "사용자" if msg.role.value == "user" else "어시스턴트"
            content = msg.content[: self._MAX_MSG_LEN]
            lines.append(f"[{prefix}] {content}")
        lines.append("=== 대화 이력 끝 ===")
        return "\n".join(lines)

    async def _llm_summarize(self, messages: List[ConversationMessage]) -> str:
        """LLM으로 대화를 요약한다. 실패 시 슬라이딩 윈도우로 폴백."""
        conversation_text = self._sliding_window(messages)
        template = self._load_summary_prompt()
        user_content = template.format(conversation_text=conversation_text)

        try:
            content, _tokens = await self._llm.complete(
                system_prompt="당신은 대화 요약 전문가입니다.",
                messages=[{"role": "user", "content": user_content}],
            )
            summary = content.strip()
            if not summary:
                return self._sliding_window(messages)
            return summary
        except Exception as exc:
            logger.warning(
                "ConversationCompressor LLM summarize failed (%s) — fallback to sliding window",
                exc,
            )
            return self._sliding_window(messages)

    def _load_summary_prompt(self) -> str:
        if self._prompt_registry is None:
            return _DEFAULT_SUMMARY_PROMPT
        try:
            template = self._prompt_registry.get(_SUMMARY_PROMPT_KEY)
            return template if template else _DEFAULT_SUMMARY_PROMPT
        except Exception as exc:
            logger.warning("ConversationCompressor 프롬프트 템플릿 로드 실패, 기본값 사용: %s", exc)
            return _DEFAULT_SUMMARY_PROMPT
