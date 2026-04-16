"""
RuleBasedReranker — 메타데이터 기반 규칙 재정렬 (Phase 2 FG2.2)

규칙:
  1. 문서 신선도: updated_at이 최근 30일 이내이면 보너스
  2. pinned 문서: metadata["pinned"] == True 이면 스코어 상승

S2 원칙: DocumentType 값을 코드에 하드코딩하지 않음.
규칙은 생성자 파라미터로 주입받으며, 향후 DocumentType 설정에서 로드 가능.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

from app.services.retrieval.base import RetrievalResult
from app.services.retrieval.reranker_base import Reranker

logger = logging.getLogger(__name__)


class RuleBasedReranker(Reranker):
    """메타데이터 기반 휴리스틱 재정렬."""

    def __init__(
        self,
        freshness_bonus: float = 0.05,
        pinned_bonus: float = 0.10,
    ) -> None:
        self._freshness_bonus = freshness_bonus
        self._pinned_bonus = pinned_bonus

    async def rerank(
        self,
        query: str,
        candidates: List[RetrievalResult],
        top_k: int = 10,
    ) -> List[RetrievalResult]:
        candidates = self._truncate_candidates(candidates)
        now = datetime.now(tz=timezone.utc)
        scored = []

        for result in candidates:
            bonus = 0.0

            # 신선도 보너스 (최근 30일 이내 수정)
            updated_at_str = result.metadata.get("updated_at")
            if updated_at_str:
                try:
                    updated_at = datetime.fromisoformat(str(updated_at_str))
                    # timezone-naive → UTC 가정
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=timezone.utc)
                    days_old = (now - updated_at).days
                    if 0 <= days_old <= 30:
                        bonus += self._freshness_bonus * max(0.0, 1.0 - days_old / 30.0)
                except (ValueError, TypeError):
                    pass

            # Pinned 문서 보너스
            if result.metadata.get("pinned") is True:
                bonus += self._pinned_bonus

            scored.append((result.score + bonus, result))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:top_k]]
