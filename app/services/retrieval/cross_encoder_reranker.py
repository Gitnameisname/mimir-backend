"""
CrossEncoderReranker — sentence-transformers cross-encoder 기반 재정렬 (Phase 2 FG2.2)

폐쇄망 지원:
  - RERANKER_MODEL_PATH 환경변수 설정 시 로컬 모델 사용
  - 미설정 또는 모델 로드/패키지 실패 시 NullReranker로 폴백 (서비스 중단 없음)

S2 원칙: 외부 의존 off 시 서비스 degrade하지만 실패하지 않음.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional

from app.services.retrieval.base import RetrievalResult
from app.services.retrieval.reranker_base import Reranker

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class CrossEncoderReranker(Reranker):
    """Cross-encoder 모델 기반 재정렬."""

    def __init__(self, model_name_or_path: Optional[str] = None) -> None:
        self._model_path = (
            model_name_or_path
            or os.getenv("RERANKER_MODEL_PATH")
            or _DEFAULT_MODEL
        )
        self._model = None
        self._fallback = None
        self._load_model()

    def _load_model(self) -> None:
        """Cross-encoder 모델을 로드한다. 실패 시 경고 후 fallback 모드."""
        from app.services.retrieval.null_reranker import NullReranker

        self._fallback = NullReranker()
        try:
            from sentence_transformers import CrossEncoder  # type: ignore[import]

            self._model = CrossEncoder(self._model_path)
            logger.info("CrossEncoderReranker loaded: %s", self._model_path)
        except ImportError:
            logger.warning(
                "sentence-transformers not installed — CrossEncoderReranker "
                "will use NullReranker fallback"
            )
            self._model = None
        except Exception as exc:
            logger.warning(
                "CrossEncoderReranker failed to load model %r: %s — using NullReranker fallback",
                self._model_path,
                exc,
            )
            self._model = None

    async def rerank(
        self,
        query: str,
        candidates: List[RetrievalResult],
        top_k: int = 10,
    ) -> List[RetrievalResult]:
        if self._model is None:
            return await self._fallback.rerank(query, candidates, top_k)

        candidates = self._truncate_candidates(candidates)
        pairs = [(query, r.content) for r in candidates]

        try:
            # CrossEncoder.predict()는 동기 함수 → asyncio.to_thread로 감싸기
            scores = await asyncio.to_thread(self._model.predict, pairs)
        except Exception as exc:
            logger.warning(
                "CrossEncoderReranker.predict failed: %s — using NullReranker fallback",
                exc,
            )
            return await self._fallback.rerank(query, candidates, top_k)

        scored = sorted(
            zip(scores, candidates),
            key=lambda x: float(x[0]),
            reverse=True,
        )
        return [r for _, r in scored[:top_k]]
