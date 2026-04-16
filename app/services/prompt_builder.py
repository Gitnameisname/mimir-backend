"""RAG 프롬프트 구성 — Task 3-5.

멀티턴 컨텍스트 윈도우, 현재 쿼리, 검색 결과를 조합하여
LLM에 전달할 프롬프트를 구성하고 섹션별 토큰을 계산한다.

설계 원칙:
  - 섹션 분리 (system / context / query / search) → 토큰 예산 관리 용이
  - 외부 의존 없이 동작 (S2 원칙 ⑦)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.models.conversation import Turn
from app.services.context_window_manager import count_tokens


_SYSTEM_PROMPT = (
    "당신은 문서 기반 질문-답변 시스템입니다.\n"
    "제공된 검색 결과를 기반으로 정확하고 간결한 답변을 제공하세요.\n"
    "인용 출처는 [숫자] 형식으로 명확히 표시하세요.\n"
    "검색 결과에 없는 내용은 '정보가 없습니다'라고 답하세요."
)


class PromptBuilder:
    """RAG 프롬프트 구성 및 섹션별 토큰 계산.

    Args:
        system_prompt: 시스템 프롬프트 (기본값: 내장 프롬프트)
    """

    def __init__(self, system_prompt: Optional[str] = None) -> None:
        self.system_prompt = system_prompt or _SYSTEM_PROMPT

    def build_prompt(
        self,
        query: str,
        search_results: Optional[List[Dict[str, Any]]] = None,
        context_turns: Optional[List[Turn]] = None,
    ) -> str:
        """전체 프롬프트를 구성한다.

        섹션 순서:
          1. 시스템 지시 (고정)
          2. 대화 이력 (컨텍스트 윈도우 — 선택)
          3. 현재 질문
          4. 참고 자료 (검색 결과)
          5. 응답 지시

        Args:
            query: 현재 쿼리
            search_results: 검색 결과 리스트 (content 키 보유 dict)
            context_turns: 이전 턴 리스트 (turn_number 오름차순)

        Returns:
            최종 프롬프트 문자열
        """
        if search_results is None:
            search_results = []

        parts: List[str] = [self.system_prompt, ""]

        # 대화 이력 섹션
        if context_turns:
            parts.append("=== 대화 이력 ===")
            for turn in context_turns:
                parts.append(f"User: {turn.user_message}")
                parts.append(f"Assistant: {turn.assistant_response}")
            parts.append("")

        # 현재 질문 섹션
        parts.append("=== 현재 질문 ===")
        parts.append(query)
        parts.append("")

        # 참고 자료 섹션
        if search_results:
            parts.append("=== 참고 자료 ===")
            for idx, result in enumerate(search_results, 1):
                content = result.get("content", "")
                parts.append(f"[{idx}] {content}")
            parts.append("")

        # 응답 지시
        parts.append("=== 응답 ===")
        parts.append("위 참고 자료를 기반으로 질문에 답하고, [숫자]로 출처를 표시하세요.")

        return "\n".join(parts)

    def count_prompt_tokens(
        self,
        query: str,
        search_results: Optional[List[Dict[str, Any]]] = None,
        context_turns: Optional[List[Turn]] = None,
    ) -> Dict[str, int]:
        """프롬프트 각 섹션의 토큰 수를 계산한다.

        Returns:
            {"system": int, "context": int, "query": int, "search": int, "total": int}
        """
        if search_results is None:
            search_results = []

        system_tokens = count_tokens(self.system_prompt)
        query_tokens = count_tokens(query)

        context_tokens = 0
        if context_turns:
            for turn in context_turns:
                context_tokens += count_tokens(turn.user_message)
                context_tokens += count_tokens(turn.assistant_response)

        search_tokens = 0
        for result in search_results:
            search_tokens += count_tokens(result.get("content", ""))

        return {
            "system": system_tokens,
            "context": context_tokens,
            "query": query_tokens,
            "search": search_tokens,
            "total": system_tokens + context_tokens + query_tokens + search_tokens,
        }
