"""
RerankerFactory — 설정 기반 Reranker 동적 생성 (Phase 2 FG2.2)
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from app.services.retrieval.reranker_base import Reranker


class RerankerFactory:
    """Reranker 인스턴스를 설정 기반으로 생성한다."""

    @staticmethod
    def create(
        name: Optional[str],
        params: Optional[Dict[str, Any]] = None,
    ) -> Reranker:
        """Reranker 이름과 파라미터로 인스턴스를 생성한다.

        Args:
            name: "cross_encoder" | "rule_based" | "null" | None
            params: Reranker별 설정 파라미터

        Returns:
            Reranker 구현체 (None 또는 "null" → NullReranker)

        Raises:
            ValueError: 알 수 없는 Reranker 이름
        """
        from app.services.retrieval.null_reranker import NullReranker
        from app.services.retrieval.cross_encoder_reranker import CrossEncoderReranker
        from app.services.retrieval.rule_based_reranker import RuleBasedReranker

        params = params or {}

        # 환경변수로 전체 비활성화 가능 (폐쇄망 / 비용 절감)
        if os.getenv("RERANKER_ENABLED", "true").lower() == "false":
            return NullReranker()

        if name is None or name == "null":
            return NullReranker()

        if name == "cross_encoder":
            return CrossEncoderReranker(
                model_name_or_path=params.get("model"),
            )

        if name == "rule_based":
            return RuleBasedReranker(
                freshness_bonus=params.get("freshness_bonus", 0.05),
                pinned_bonus=params.get("pinned_bonus", 0.10),
            )

        raise ValueError(
            f"Unknown reranker: {name!r}. Choose from: cross_encoder, rule_based, null"
        )
