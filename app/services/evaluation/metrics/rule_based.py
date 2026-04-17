"""
규칙 기반 평가 지표 — Phase 7 FG7.2

외부 LLM 없이 동작하는 Citation Present, Hallucination Rate, Context Recall.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .citation import CitationDetector
from .sentence_splitter import SentenceSplitter

logger = logging.getLogger(__name__)


@dataclass
class MetricScore:
    metric_name: str
    score: float
    details: Dict[str, Any] = field(default_factory=dict)
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(f"Metric score must be between 0.0 and 1.0, got {self.score}")


class MetricCalculator(ABC):
    @abstractmethod
    def compute(self, **kwargs) -> MetricScore:
        pass

    @property
    @abstractmethod
    def metric_name(self) -> str:
        pass


class CitationPresentMetric(MetricCalculator):
    """Citation Present Rate = cited_sentences / total_sentences."""

    def __init__(self, sentence_splitter: Optional[SentenceSplitter] = None) -> None:
        self._splitter = sentence_splitter or SentenceSplitter()
        self._detector = CitationDetector()

    @property
    def metric_name(self) -> str:
        return "citation_present_rate"

    def compute(self, answer_text: str = "", **kwargs) -> MetricScore:
        if not answer_text or not answer_text.strip():
            return MetricScore(
                metric_name=self.metric_name,
                score=0.0,
                details={"num_sentences": 0, "cited_sentences": 0, "uncited_sentences": 0},
            )

        sentences = self._splitter.split(answer_text)
        if not sentences:
            return MetricScore(
                metric_name=self.metric_name,
                score=0.0,
                details={"num_sentences": 0, "cited_sentences": 0, "uncited_sentences": 0},
            )

        cited = sum(1 for s in sentences if self._detector.has_citation(s.text))
        total = len(sentences)
        rate = cited / total

        logger.debug("citation_present_rate: %d/%d sentences cited", cited, total)
        return MetricScore(
            metric_name=self.metric_name,
            score=rate,
            details={
                "num_sentences": total,
                "cited_sentences": cited,
                "uncited_sentences": total - cited,
            },
        )


class HallucinationRateMetric(MetricCalculator):
    """Hallucination Rate = 1.0 - Citation Present Rate."""

    def __init__(self, sentence_splitter: Optional[SentenceSplitter] = None) -> None:
        self._cpm = CitationPresentMetric(sentence_splitter)

    @property
    def metric_name(self) -> str:
        return "hallucination_rate"

    def compute(self, answer_text: str = "", **kwargs) -> MetricScore:
        cpr = self._cpm.compute(answer_text=answer_text)
        rate = 1.0 - cpr.score
        return MetricScore(
            metric_name=self.metric_name,
            score=rate,
            details={"citation_present_rate": cpr.score, **cpr.details},
        )


class ContextRecallMetric(MetricCalculator):
    """Context Recall = |found_sources| / |expected_sources|."""

    @property
    def metric_name(self) -> str:
        return "context_recall"

    def compute(
        self,
        expected_source_docs: List[str] = (),
        retrieved_chunks: List[Any] = (),
        **kwargs,
    ) -> MetricScore:
        if not expected_source_docs:
            return MetricScore(
                metric_name=self.metric_name,
                score=1.0,
                details={"expected_sources": 0, "found_sources": 0},
            )

        retrieved_ids: set[str] = set()
        for chunk in retrieved_chunks:
            if isinstance(chunk, dict) and "source_id" in chunk:
                retrieved_ids.add(chunk["source_id"])
            elif isinstance(chunk, str):
                retrieved_ids.add(chunk)

        expected_set = set(expected_source_docs)
        found = expected_set & retrieved_ids
        recall = len(found) / len(expected_set)

        return MetricScore(
            metric_name=self.metric_name,
            score=recall,
            details={
                "expected_sources": list(expected_set),
                "retrieved_sources": list(retrieved_ids),
                "found_sources": list(found),
                "missing_sources": list(expected_set - retrieved_ids),
                "recall_percentage": recall * 100,
            },
        )


class MetricRegistry:
    def __init__(self) -> None:
        self._calculators: Dict[str, MetricCalculator] = {}

    def register(self, calculator: MetricCalculator) -> None:
        self._calculators[calculator.metric_name] = calculator

    def get(self, metric_name: str) -> Optional[MetricCalculator]:
        return self._calculators.get(metric_name)

    def list_metrics(self) -> List[str]:
        return list(self._calculators.keys())

    def compute_all(self, metrics: List[str], **kwargs) -> Dict[str, MetricScore]:
        results: Dict[str, MetricScore] = {}
        for name in metrics:
            calc = self.get(name)
            if calc is None:
                logger.warning("Unknown metric: %s", name)
                continue
            try:
                results[name] = calc.compute(**kwargs)
            except Exception as exc:
                logger.error("Error computing %s: %s", name, exc)
                results[name] = MetricScore(
                    metric_name=name, score=0.0, details={"error": str(exc)}
                )
        return results


DEFAULT_REGISTRY = MetricRegistry()
DEFAULT_REGISTRY.register(CitationPresentMetric())
DEFAULT_REGISTRY.register(HallucinationRateMetric())
DEFAULT_REGISTRY.register(ContextRecallMetric())
