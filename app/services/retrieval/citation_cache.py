"""Citation 캐시 — 대화별 인메모리 Citation + 이력 저장.

각 턴의 검색 결과(Citation 리스트)를 저장하여 다음 턴에서
QueryRewriter가 이전 Citation 정보를 활용할 수 있게 한다.
최대 10턴 슬라이딩 윈도우로 메모리 사용량을 제한한다.

NOTE: 인메모리 싱글톤이므로 FastAPI 재시작 시 대화 이력이 초기화된다.
Phase 3(Conversation)에서 DB 기반 영속 저장으로 교체 예정.

보안:
  - 소유권 검증: 대화 생성 actor_id를 기록하고, 이후 접근 시 일치 여부 확인 (IDOR 방지)
  - 최대 대화 수 제한: DoS 방어 (_MAX_CONVERSATIONS)
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Dict, List, Optional
from uuid import UUID

from app.schemas.citation import Citation
from app.schemas.conversation import ConversationMessage, MessageRole

logger = logging.getLogger(__name__)

_MAX_TURNS = 10          # 슬라이딩 윈도우 최대 턴 수
_MAX_CONVERSATIONS = 50_000  # 인메모리 대화 최대 수 (DoS 방어)


class ConversationTurn:
    """단일 대화 턴 기록."""

    def __init__(
        self,
        turn_number: int,
        query: str,
        rewritten_query: Optional[str],
        citations: List[Citation],
        answer: str,
    ) -> None:
        self.turn_number = turn_number
        self.query = query
        self.rewritten_query = rewritten_query
        self.citations = citations
        self.answer = answer

    def to_message_pair(self) -> List[ConversationMessage]:
        """(사용자 질의, 어시스턴트 답변) 메시지 쌍으로 변환한다."""
        return [
            ConversationMessage(
                role=MessageRole.USER,
                content=self.rewritten_query or self.query,
                turn_number=self.turn_number,
            ),
            ConversationMessage(
                role=MessageRole.ASSISTANT,
                content=self.answer,
                turn_number=self.turn_number,
            ),
        ]


class CitationCache:
    """대화별 Citation + 이력 인메모리 캐시.

    conversation_id 키로 각 대화의 턴 이력을 관리한다.
    각 대화는 최대 _MAX_TURNS (10)개 턴을 유지한다.

    보안:
      - 소유권 추적: 첫 번째 턴을 생성한 actor_id를 기록한다.
        이후 동일 conversation_id로 접근 시 verify_ownership()으로 검증해야 한다.
      - 전체 대화 수 상한: _MAX_CONVERSATIONS 초과 시 오래된 항목 제거 (DoS 방어).
    """

    def __init__(self) -> None:
        # conversation_id(str) → deque[ConversationTurn]
        self._store: Dict[str, deque] = {}
        # conversation_id(str) → owner actor_id (Optional[str])
        self._owners: Dict[str, Optional[str]] = {}
        # 삽입 순서 추적 (오래된 항목 제거용)
        self._insertion_order: deque = deque()

    def verify_ownership(
        self,
        conversation_id: UUID,
        actor_id: Optional[str],
    ) -> bool:
        """대화 소유권을 검증한다.

        Args:
            conversation_id: 대화 ID
            actor_id: 요청 actor ID

        Returns:
            True — 소유권 확인됨 (또는 대화가 아직 없어 새로 생성 가능).
            False — 소유권 불일치 (다른 actor가 생성한 대화).
        """
        key = str(conversation_id)
        if key not in self._store:
            return True  # 새 대화 — 허용
        owner = self._owners.get(key)
        if owner is None:
            return True  # 소유자 미기록 — 하위호환 허용
        return owner == actor_id

    def add_turn(
        self,
        conversation_id: UUID,
        turn: ConversationTurn,
        actor_id: Optional[str] = None,
    ) -> None:
        """새 턴을 저장한다.

        Args:
            conversation_id: 대화 ID
            turn: 저장할 턴 데이터
            actor_id: 요청 actor ID (소유권 기록용)
        """
        key = str(conversation_id)

        # 신규 대화 등록
        if key not in self._store:
            # 최대 대화 수 초과 시 가장 오래된 항목 제거
            while len(self._store) >= _MAX_CONVERSATIONS and self._insertion_order:
                oldest = self._insertion_order.popleft()
                self._store.pop(oldest, None)
                self._owners.pop(oldest, None)
                logger.warning(
                    "CitationCache: evicted oldest conversation=%s (limit=%d)",
                    oldest,
                    _MAX_CONVERSATIONS,
                )
            self._store[key] = deque(maxlen=_MAX_TURNS)
            self._owners[key] = actor_id
            self._insertion_order.append(key)

        self._store[key].append(turn)
        logger.debug(
            "CitationCache: conversation=%s turn=%d stored",
            conversation_id,
            turn.turn_number,
        )

    def get_history(
        self,
        conversation_id: UUID,
    ) -> List[ConversationMessage]:
        """대화 이력을 ConversationMessage 리스트로 반환한다."""
        turns = self._store.get(str(conversation_id), deque())
        messages = []
        for turn in turns:
            messages.extend(turn.to_message_pair())
        return messages

    def get_citations(
        self,
        conversation_id: UUID,
    ) -> List[Citation]:
        """이전 턴에서 사용된 모든 Citation 리스트를 반환한다."""
        turns = self._store.get(str(conversation_id), deque())
        citations: List[Citation] = []
        for turn in turns:
            citations.extend(turn.citations)
        return citations

    def get_turn_number(self, conversation_id: UUID) -> int:
        """현재 대화의 다음 턴 번호를 반환한다 (다음에 기록될 턴 번호)."""
        return len(self._store.get(str(conversation_id), [])) + 1

    def clear(self, conversation_id: UUID) -> None:
        """대화 이력을 초기화한다."""
        key = str(conversation_id)
        self._store.pop(key, None)
        self._owners.pop(key, None)


# 모듈 수준 싱글톤 (FastAPI 앱 수명 동안 유지)
_citation_cache = CitationCache()


def get_citation_cache() -> CitationCache:
    """전역 CitationCache 인스턴스를 반환한다."""
    return _citation_cache
