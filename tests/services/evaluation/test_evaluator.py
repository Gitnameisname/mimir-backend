"""
Evaluator 통합 테스트 — Phase 7 FG7.2 (task7-6)

테스트 범위:
- AnswerRelevanceMetric: 높은/낮은 관련도, 공백 입력
- Evaluator.evaluate_golden_item: 점수, 토큰, 비용, 지연 시간
- Evaluator.evaluate_golden_set: 배치 평가, 통계
- ScoreMetrics.average: hallucination_rate 반전
- EvaluationReport: overall_score, pass_rate
- 비동기 evaluate_golden_item_async
"""
from __future__ import annotations

import pytest

from app.services.evaluation.evaluator import Evaluator
from app.services.evaluation.metrics.embedding_based import AnswerRelevanceMetric
from app.services.evaluation.models import (
    EvaluationReport,
    EvaluationResult,
    ScoreMetrics,
    TokenMetrics,
    LatencyMetrics,
    CostMetrics,
)

# ---------------------------------------------------------------------------
# Shared sample item
# ---------------------------------------------------------------------------

_CITE = (
    "[CITE: entity_type=chunk, entity_value=Paris, "
    "source_type=file, source_id=doc_001, chunk_id=ch_001]"
)

_SAMPLE = {
    "item_id": "golden_001",
    "question": "What is Paris?",
    "answer": f"Paris is the capital of France. {_CITE}",
    "contexts": [
        "Paris is the capital and largest city of France.",
        "France is a country in Western Europe.",
    ],
    "expected_answer": "The capital of France",
    "expected_sources": ["doc_001"],
    "retrieval_time_ms": 50.0,
    "generation_time_ms": 100.0,
    "input_tokens": 50,
    "output_tokens": 25,
}


# ---------------------------------------------------------------------------
# AnswerRelevanceMetric
# ---------------------------------------------------------------------------

class TestAnswerRelevanceMetric:
    def setup_method(self):
        self.metric = AnswerRelevanceMetric(embedding_enabled=False)

    def test_high_relevance(self):
        result = self.metric.compute(
            question="What is the capital of France?",
            answer_text="Paris is the capital of France.",
        )
        assert result.score > 0.0
        assert "fallback" in result.details["method"]

    def test_low_relevance(self):
        result = self.metric.compute(
            question="What is the capital of France?",
            answer_text="Dogs bark at night.",
        )
        assert result.score < 0.5

    def test_empty_question_zero(self):
        result = self.metric.compute(question="", answer_text="Some answer")
        assert result.score == 0.0

    def test_empty_answer_zero(self):
        result = self.metric.compute(question="What?", answer_text="")
        assert result.score == 0.0

    def test_metric_name(self):
        assert self.metric.metric_name == "answer_relevance"

    def test_embedding_fallback_on_error(self):
        from unittest.mock import Mock
        from app.services.evaluation.metrics.embedding_based import EmbeddingModel
        bad_model = Mock(spec=EmbeddingModel)
        bad_model.embed.side_effect = RuntimeError("embedding service down")
        m = AnswerRelevanceMetric(embedding_model=bad_model, embedding_enabled=True)
        result = m.compute(question="Q?", answer_text="Some answer.")
        assert "fallback" in result.details["method"]


# ---------------------------------------------------------------------------
# Evaluator — single item
# ---------------------------------------------------------------------------

class TestEvaluatorSingleItem:
    def setup_method(self):
        self.ev = Evaluator()

    def test_basic_evaluation(self):
        result = self.ev.evaluate_golden_item(**_SAMPLE)
        assert isinstance(result, EvaluationResult)
        assert result.item_id == "golden_001"
        assert result.scores.faithfulness is not None
        assert result.scores.answer_relevance is not None
        assert result.scores.context_recall is not None
        assert result.scores.citation_present_rate == 1.0
        assert 0.0 <= result.overall_score() <= 1.0

    def test_token_metrics(self):
        result = self.ev.evaluate_golden_item(**_SAMPLE)
        assert result.token_metrics is not None
        assert result.token_metrics.query_tokens == 50
        assert result.token_metrics.response_tokens == 25
        assert result.token_metrics.total_tokens == 75

    def test_cost_metrics(self):
        result = self.ev.evaluate_golden_item(**_SAMPLE)
        assert result.cost_metrics is not None
        assert result.cost_metrics.total_cost > 0.0
        assert result.cost_metrics.query_cost + result.cost_metrics.response_cost == pytest.approx(
            result.cost_metrics.total_cost
        )

    def test_latency_metrics(self):
        result = self.ev.evaluate_golden_item(**_SAMPLE)
        assert result.latency_metrics.retrieval_ms == 50.0
        assert result.latency_metrics.generation_ms == 100.0
        assert result.latency_metrics.total_ms == 150.0

    def test_no_tokens_no_cost(self):
        item = {k: v for k, v in _SAMPLE.items() if k not in ("input_tokens", "output_tokens")}
        result = self.ev.evaluate_golden_item(**item)
        assert result.token_metrics is None
        assert result.cost_metrics is None

    @pytest.mark.asyncio
    async def test_async_evaluation(self):
        result = await self.ev.evaluate_golden_item_async(**_SAMPLE)
        assert isinstance(result, EvaluationResult)
        assert result.item_id == "golden_001"

    def test_context_recall_zero_when_no_retrieved(self):
        """retrieved_source_ids 미제공 시 recall = 0.0."""
        result = self.ev.evaluate_golden_item(**_SAMPLE)
        assert result.scores.context_recall == 0.0

    def test_missing_sources_recall_less_than_one(self):
        item = {**_SAMPLE, "expected_sources": ["doc_001", "doc_002", "doc_003"]}
        result = self.ev.evaluate_golden_item(**item)
        assert result.scores.context_recall < 1.0

    def test_full_context_recall_when_retrieved_provided(self):
        item = {**_SAMPLE, "retrieved_source_ids": ["doc_001"]}
        result = self.ev.evaluate_golden_item(**item)
        assert result.scores.context_recall == 1.0


# ---------------------------------------------------------------------------
# Evaluator — batch
# ---------------------------------------------------------------------------

class TestEvaluatorBatch:
    def setup_method(self):
        self.ev = Evaluator()
        self.items = [
            {
                "id": f"item_{i}",
                "question": "What is Paris?",
                "answer": f"Paris is the capital. {_CITE}",
                "contexts": ["Paris is the capital of France."],
                "expected_answer": "Capital of France",
            }
            for i in range(3)
        ]

    def test_batch_evaluation(self):
        report = self.ev.evaluate_golden_set(batch_id="batch_001", golden_items=self.items)
        assert isinstance(report, EvaluationReport)
        assert report.batch_id == "batch_001"
        assert report.total_items == 3
        assert report.successful_items == 3
        assert len(report.results) == 3

    def test_scores_summary_present(self):
        report = self.ev.evaluate_golden_set(batch_id="b", golden_items=self.items)
        assert "faithfulness" in report.scores_summary
        assert "mean" in report.scores_summary["faithfulness"]

    @pytest.mark.asyncio
    async def test_async_batch(self):
        report = await self.ev.evaluate_golden_set_async(
            batch_id="batch_async", golden_items=self.items[:2], max_concurrent=2
        )
        assert report.total_items == 2
        assert report.successful_items == 2

    def test_overall_score_range(self):
        report = self.ev.evaluate_golden_set(batch_id="b", golden_items=self.items)
        assert 0.0 <= report.overall_score() <= 1.0

    def test_pass_rate_range(self):
        report = self.ev.evaluate_golden_set(batch_id="b", golden_items=self.items)
        assert 0.0 <= report.pass_rate(threshold=0.5) <= 1.0


# ---------------------------------------------------------------------------
# ScoreMetrics
# ---------------------------------------------------------------------------

class TestScoreMetrics:
    def test_average_inverts_hallucination(self):
        scores = ScoreMetrics(
            faithfulness=0.8,
            hallucination_rate=0.2,
        )
        avg = scores.average()
        assert avg == pytest.approx((0.8 + 0.8) / 2)

    def test_average_excludes_none(self):
        scores = ScoreMetrics(faithfulness=1.0)
        assert scores.average() == 1.0

    def test_all_none_returns_zero(self):
        scores = ScoreMetrics()
        assert scores.average() == 0.0

    def test_to_dict_keys(self):
        scores = ScoreMetrics(faithfulness=0.5)
        d = scores.to_dict()
        assert "faithfulness" in d
        assert "hallucination_rate" in d


# ---------------------------------------------------------------------------
# Pydantic model validators
# ---------------------------------------------------------------------------

class TestPydanticModels:
    def test_token_metrics_valid(self):
        tm = TokenMetrics(query_tokens=10, response_tokens=5, total_tokens=15)
        assert tm.total_tokens == 15

    def test_token_metrics_invalid_total(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            TokenMetrics(query_tokens=10, response_tokens=5, total_tokens=20)

    def test_latency_metrics_valid(self):
        lm = LatencyMetrics(retrieval_ms=10.0, generation_ms=20.0, total_ms=30.0)
        assert lm.total_ms == 30.0

    def test_cost_metrics_valid(self):
        cm = CostMetrics(query_cost=0.001, response_cost=0.002, total_cost=0.003)
        assert cm.total_cost == pytest.approx(0.003)
