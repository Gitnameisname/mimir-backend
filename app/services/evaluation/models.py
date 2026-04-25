"""
평가 결과 데이터 모델 — Phase 7 FG7.2
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from statistics import mean, median, stdev
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator
from app.utils.time import utcnow


class MetricType(str, Enum):
    FAITHFULNESS = "faithfulness"
    ANSWER_RELEVANCE = "answer_relevance"
    CONTEXT_PRECISION = "context_precision"
    CONTEXT_RECALL = "context_recall"
    CITATION_PRESENT = "citation_present_rate"
    HALLUCINATION_RATE = "hallucination_rate"


class TokenMetrics(BaseModel):
    query_tokens: int = Field(ge=0)
    response_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @field_validator("total_tokens")
    @classmethod
    def validate_total(cls, v, info) -> int:
        if "query_tokens" in info.data and "response_tokens" in info.data:
            expected = info.data["query_tokens"] + info.data["response_tokens"]
            if v != expected:
                raise ValueError(f"total_tokens must equal {expected}")
        return v


class LatencyMetrics(BaseModel):
    retrieval_ms: float = Field(ge=0)
    generation_ms: float = Field(ge=0)
    total_ms: float = Field(ge=0)

    @field_validator("total_ms")
    @classmethod
    def validate_total(cls, v, info) -> float:
        if "retrieval_ms" in info.data and "generation_ms" in info.data:
            expected = info.data["retrieval_ms"] + info.data["generation_ms"]
            if abs(v - expected) > 1.0:
                raise ValueError(f"total_ms must be approximately {expected}")
        return v


class CostMetrics(BaseModel):
    query_cost: float = Field(ge=0)
    response_cost: float = Field(ge=0)
    total_cost: float = Field(ge=0)

    @field_validator("total_cost")
    @classmethod
    def validate_total(cls, v, info) -> float:
        if "query_cost" in info.data and "response_cost" in info.data:
            expected = info.data["query_cost"] + info.data["response_cost"]
            if abs(v - expected) > 0.0001:
                raise ValueError(f"total_cost must equal {expected}")
        return v


class ScoreMetrics(BaseModel):
    faithfulness: Optional[float] = Field(None, ge=0.0, le=1.0)
    answer_relevance: Optional[float] = Field(None, ge=0.0, le=1.0)
    context_precision: Optional[float] = Field(None, ge=0.0, le=1.0)
    context_recall: Optional[float] = Field(None, ge=0.0, le=1.0)
    citation_present_rate: Optional[float] = Field(None, ge=0.0, le=1.0)
    hallucination_rate: Optional[float] = Field(None, ge=0.0, le=1.0)

    def average(self, exclude_none: bool = True) -> float:
        raw = [
            self.faithfulness,
            self.answer_relevance,
            self.context_precision,
            self.context_recall,
            self.citation_present_rate,
            (1.0 - self.hallucination_rate) if self.hallucination_rate is not None else None,
        ]
        scores = [s for s in raw if s is not None] if exclude_none else [s or 0.0 for s in raw]
        return sum(scores) / len(scores) if scores else 0.0

    def to_dict(self) -> Dict[str, Optional[float]]:
        return self.model_dump()


class EvaluationResult(BaseModel):
    item_id: str
    question: str
    answer: str
    contexts: List[str]
    expected_answer: Optional[str] = None
    expected_sources: Optional[List[str]] = None

    scores: ScoreMetrics
    token_metrics: Optional[TokenMetrics] = None
    latency_metrics: Optional[LatencyMetrics] = None
    cost_metrics: Optional[CostMetrics] = None

    evaluation_time: datetime = Field(default_factory=lambda: utcnow())
    evaluator_version: str = "1.0"
    notes: Optional[str] = None

    def overall_score(self) -> float:
        return self.scores.average()


class EvaluationReport(BaseModel):
    batch_id: str
    total_items: int = Field(ge=0)
    successful_items: int = Field(ge=0)
    failed_items: int = Field(ge=0)
    scores_summary: Dict[str, Dict[str, Any]]
    total_tokens: int = Field(ge=0)
    total_latency_ms: float = Field(ge=0)
    total_cost: float = Field(ge=0)
    results: List[EvaluationResult]
    created_at: datetime = Field(default_factory=lambda: utcnow())
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None

    def overall_score(self) -> float:
        if not self.results:
            return 0.0
        scores = [r.overall_score() for r in self.results]
        return sum(scores) / len(scores)

    def pass_rate(self, threshold: float = 0.7) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.overall_score() >= threshold) / len(self.results)
