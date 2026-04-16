"""QueryRewriter — LLM 기반 쿼리 재작성.

멀티턴 대화의 follow-up 질의를 자립적인 단발 검색 쿼리로 재작성한다.
LLM 호출 실패 시 원본 쿼리를 그대로 반환하여 서비스 연속성을 보장한다 (S2 원칙).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional

from app.schemas.conversation import ConversationMessage

if TYPE_CHECKING:
    from app.services.prompt.registry import PromptRegistry

logger = logging.getLogger(__name__)

_REWRITE_PROMPT_KEY = "query_rewrite_standalone"

_DEFAULT_REWRITE_PROMPT = (
    "당신은 검색 질의 재작성 전문가입니다.\n\n"
    "원본 질의: {original_query}\n"
    "이전 대화 맥락:\n{conversation_history}\n\n"
    "위 맥락을 고려하여, 원본 질의를 더 자립적이고 명확한 단발 검색 쿼리로 다시 작성하세요.\n"
    "검색 엔진이 이해할 수 있도록 주요 키워드를 포함하세요.\n"
    "재작성된 쿼리만 출력하세요 (설명 없이)."
)


class QueryRewriter:
    """멀티턴 대화 컨텍스트 기반 쿼리 재작성.

    LLMProvider의 `complete(system_prompt, messages)` 인터페이스를 사용한다.
    PromptRegistry가 없거나 키 조회 실패 시 내부 기본 프롬프트를 사용한다.

    Attributes:
        _llm: LLMProvider 인스턴스 (rag_service.LLMProvider 프로토콜)
        _prompt_registry: PromptRegistry 인스턴스 (선택)
    """

    def __init__(
        self,
        llm,
        prompt_registry: Optional["PromptRegistry"] = None,
    ) -> None:
        self._llm = llm
        self._prompt_registry = prompt_registry

    async def rewrite_query(
        self,
        original_query: str,
        conversation_history: List[ConversationMessage],
        mode: str = "standalone",
    ) -> str:
        """쿼리를 재작성한다.

        Args:
            original_query: 원본 쿼리
            conversation_history: 이전 턴 메시지 리스트
            mode: "standalone" — 자립적 쿼리로 재작성 (현재 유일한 모드)

        Returns:
            재작성된 쿼리 문자열.
            대화 이력 없을 때 또는 LLM 실패 시 → original_query 그대로 반환.
        """
        # 첫 번째 턴이거나 대화 이력 없으면 재작성 불필요
        if not conversation_history:
            return original_query

        history_text = self._format_history(conversation_history)
        system_prompt = "당신은 검색 질의 재작성 전문가입니다."
        user_content = self._load_prompt_template().format(
            original_query=original_query,
            conversation_history=history_text,
        )

        try:
            content, _tokens = await self._llm.complete(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )
            rewritten = content.strip()
            if not rewritten:
                return original_query
            logger.debug("Query rewritten: %r → %r", original_query, rewritten)
            return rewritten
        except Exception as exc:
            logger.warning(
                "QueryRewriter LLM call failed (%s) — falling back to original query: %r",
                exc,
                original_query,
            )
            return original_query

    def _load_prompt_template(self) -> str:
        """Prompt Registry에서 템플릿 로드. 없으면 기본값 사용."""
        if self._prompt_registry is None:
            return _DEFAULT_REWRITE_PROMPT
        try:
            template = self._prompt_registry.get(_REWRITE_PROMPT_KEY)
            return template if template else _DEFAULT_REWRITE_PROMPT
        except Exception:
            return _DEFAULT_REWRITE_PROMPT

    # 메시지 하나당 최대 길이 (프롬프트 인젝션 / 토큰 비용 제한)
    _MAX_MSG_LEN = 500

    @staticmethod
    def _format_history(messages: List[ConversationMessage]) -> str:
        """대화 이력을 프롬프트용 텍스트로 포맷한다.

        보안:
          - 각 메시지를 명시적 구분자로 감싸서 컨텐츠와 지시문을 분리한다 (LLM01 완화).
          - 개별 메시지를 _MAX_MSG_LEN 자로 잘라 프롬프트 팽창을 방지한다.
        """
        lines = ["=== 이전 대화 이력 시작 (참고용) ==="]
        for msg in messages:
            prefix = "사용자" if msg.role.value == "user" else "어시스턴트"
            # 개별 메시지 길이 제한 (프롬프트 인젝션 표면 축소)
            content = msg.content[: QueryRewriter._MAX_MSG_LEN]
            lines.append(f"[{prefix}] {content}")
        lines.append("=== 이전 대화 이력 끝 ===")
        return "\n".join(lines)
