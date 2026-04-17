"""
임베딩 기반 평가 지표 — Phase 7 FG7.2

Answer Relevance: 질문과 답변의 의미적 관련도 (코사인 유사도 + 키워드 폴백).
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import List, Optional

import numpy as np

from .fallback import KeywordExtractor
from .rule_based import MetricCalculator, MetricScore

logger = logging.getLogger(__name__)


class EmbeddingModel(ABC):
    @abstractmethod
    def embed(self, text: str) -> List[float]:
        pass

    @abstractmethod
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        pass


class AnswerRelevanceMetric(MetricCalculator):
    """Answer Relevance = cosine_similarity(embed(question), embed(answer))."""

    def __init__(
        self,
        embedding_model: Optional[EmbeddingModel] = None,
        embedding_enabled: Optional[bool] = None,
        fallback_method: str = "keyword",
    ) -> None:
        self._model = embedding_model
        self._embedding_enabled: bool = (
            embedding_enabled
            if embedding_enabled is not None
            else os.getenv("EVAL_EMBEDDING_ENABLED", "true").lower() == "true"
        )
        self._fallback_method = fallback_method

    @property
    def metric_name(self) -> str:
        return "answer_relevance"

    def compute(self, question: str = "", answer_text: str = "", **kwargs) -> MetricScore:
        if not question or not answer_text:
            return MetricScore(
                metric_name=self.metric_name,
                score=0.0,
                details={"reason": "missing question or answer"},
            )

        if self._embedding_enabled and self._model:
            try:
                q_emb = self._model.embed(question)
                a_emb = self._model.embed(answer_text)
                score = self._cosine_similarity(q_emb, a_emb)
                return MetricScore(
                    metric_name=self.metric_name,
                    score=score,
                    details={"method": "embedding"},
                )
            except Exception as exc:
                logger.warning("Embedding failed, using fallback: %s", exc)

        score = self._fallback_relevance(question, answer_text)
        return MetricScore(
            metric_name=self.metric_name,
            score=score,
            details={"method": f"fallback_{self._fallback_method}"},
        )

    @staticmethod
    def _cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
        if not vec1 or not vec2:
            return 0.0
        v1 = np.array(vec1, dtype=np.float32)
        v2 = np.array(vec2, dtype=np.float32)
        n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
        if n1 == 0 or n2 == 0:
            return 0.0
        return float(max(0.0, min(np.dot(v1, v2) / (n1 * n2), 1.0)))

    @staticmethod
    def _fallback_relevance(question: str, answer: str) -> float:
        q_kw = set(KeywordExtractor.extract_keywords(question, top_k=10))
        a_kw = set(KeywordExtractor.extract_keywords(answer, top_k=10))
        if not q_kw:
            return 0.5
        return min(len(q_kw & a_kw) / len(q_kw), 1.0)
