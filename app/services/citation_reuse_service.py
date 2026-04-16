"""Citation 재활용 서비스 — Task 3-6.

이전 턴의 Citation에서 document_id를 추출하고,
현재 검색 결과에 보너스 가중치를 적용하여 재랭킹한다.

설계 원칙:
  - 이전 턴이 없으면 원본 결과 그대로 반환 (idempotent)
  - 보너스는 점수 비율(×배)로 적용 — 절대값 없음
  - S2 원칙 ⑦: Valkey 없어도 동작
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

from app.models.conversation import Turn

logger = logging.getLogger(__name__)

# 이전 턴에서 인용된 Document에 부여할 점수 보너스 배율
_CITATION_BONUS_MULTIPLIER = 1.5


class CitationReuseService:
    """이전 턴 Citation 재활용 — 검색 결과 보너스 가중치 적용."""

    CITATION_BONUS_MULTIPLIER: float = _CITATION_BONUS_MULTIPLIER

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def extract_cited_document_ids(self, turns: List[Turn]) -> Set[str]:
        """Turn 목록에서 인용된 Document ID 집합을 추출한다.

        Args:
            turns: 이전 Turn 객체 리스트 (retrieval_metadata 포함)

        Returns:
            인용된 document_id 집합 (str UUID)
        """
        doc_ids: Set[str] = set()
        for turn in turns:
            if not turn.retrieval_metadata:
                continue
            citations = turn.retrieval_metadata.get("citations", [])
            for c in citations:
                doc_id = c.get("document_id")
                if doc_id:
                    doc_ids.add(str(doc_id))
        logger.debug("CitationReuseService: extracted %d cited document_ids", len(doc_ids))
        return doc_ids

    def apply_citation_bonus(
        self,
        search_results: List[Dict[str, Any]],
        previous_turns: List[Turn],
    ) -> List[Dict[str, Any]]:
        """이전 턴에서 인용된 Document에 보너스 점수를 적용하고 재정렬한다.

        Args:
            search_results: 현재 검색 결과 리스트 (dict, 'document_id' + 'score' 키 보유)
            previous_turns: 이전 Turn 리스트 (최근 → 오래된 순)

        Returns:
            보너스가 적용되고 점수 기준으로 재정렬된 검색 결과
        """
        if not search_results:
            return search_results

        cited_ids = self.extract_cited_document_ids(previous_turns)
        if not cited_ids:
            return search_results

        boosted: List[Dict[str, Any]] = []
        for result in search_results:
            doc_id = str(result.get("document_id", ""))
            if doc_id and doc_id in cited_ids:
                original = float(result.get("score", 0.0))
                boosted_score = original * self.CITATION_BONUS_MULTIPLIER
                result = {**result, "score": boosted_score, "citation_reused": True}
                logger.info(
                    "CitationReuseService: bonus applied doc_id=%s %.3f→%.3f",
                    doc_id, original, boosted_score,
                )
            boosted.append(result)

        # 점수 내림차순 재정렬
        boosted.sort(key=lambda r: float(r.get("score", 0.0)), reverse=True)
        return boosted

    def get_reused_count(
        self,
        search_results: List[Dict[str, Any]],
        previous_turns: List[Turn],
    ) -> int:
        """보너스가 적용된 결과 수를 반환한다 (디버깅/메트릭용)."""
        cited_ids = self.extract_cited_document_ids(previous_turns)
        return sum(
            1 for r in search_results
            if str(r.get("document_id", "")) in cited_ids
        )
