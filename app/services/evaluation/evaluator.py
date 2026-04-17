"""
통합 평가기 (Evaluator) — Phase 7 FG7.2

6가지 지표를 통합하여 Golden Set 평가 실행.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from datetime import datetime, timezone
from statistics import mean, median, stdev
from typing import Any, Dict, List, Optional

from .metrics.embedding_based import AnswerRelevanceMetric
from .metrics.llm_based import ContextPrecisionMetric, FaithfulnessMetric
from .metrics.rule_based import (
    CitationPresentMetric,
    ContextRecallMetric,
    HallucinationRateMetric,
)
from .models import (
    CostMetrics,
    EvaluationReport,
    EvaluationResult,
    LatencyMetrics,
    ScoreMetrics,
    TokenMetrics,
)

logger = logging.getLogger(__name__)

_COST_INPUT_PER_1K = 0.0005
_COST_OUTPUT_PER_1K = 0.0015


class Evaluator:
    """6가지 지표 통합 평가기."""

    def __init__(
        self,
        faithfulness_metric: Optional[FaithfulnessMetric] = None,
        answer_relevance_metric: Optional[AnswerRelevanceMetric] = None,
        context_precision_metric: Optional[ContextPrecisionMetric] = None,
        context_recall_metric: Optional[ContextRecallMetric] = None,
        citation_present_metric: Optional[CitationPresentMetric] = None,
        hallucination_metric: Optional[HallucinationRateMetric] = None,
    ) -> None:
        self.faithfulness = faithfulness_metric or FaithfulnessMetric()
        self.answer_relevance = answer_relevance_metric or AnswerRelevanceMetric()
        self.context_precision = context_precision_metric or ContextPrecisionMetric()
        self.context_recall = context_recall_metric or ContextRecallMetric()
        self.citation_present = citation_present_metric or CitationPresentMetric()
        self.hallucination = hallucination_metric or HallucinationRateMetric()

    # ── Single item ──────────────────────────────────────────────────────────

    async def evaluate_golden_item_async(
        self,
        item_id: str,
        question: str,
        answer: str,
        contexts: List[str],
        expected_answer: Optional[str] = None,
        expected_sources: Optional[List[str]] = None,
        retrieved_source_ids: Optional[List[str]] = None,
        retrieval_time_ms: float = 0.0,
        generation_time_ms: float = 0.0,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        **_ignored,
    ) -> EvaluationResult:
        loop = asyncio.get_event_loop()

        def _run(metric, **kwargs):
            return loop.run_in_executor(None, functools.partial(metric.compute, **kwargs))

        retrieved = retrieved_source_ids or []

        tasks = [
            _run(self.faithfulness, question=question, answer_text=answer, contexts=contexts),
            _run(self.answer_relevance, question=question, answer_text=answer),
            _run(self.context_precision, question=question, answer_text=answer, contexts=contexts),
            _run(self.context_recall,
                 expected_source_docs=expected_sources or [],
                 retrieved_chunks=[{"source_id": s} for s in retrieved]),
            _run(self.citation_present, answer_text=answer),
            _run(self.hallucination, answer_text=answer),
        ]

        _NAMES = [
            "faithfulness", "answer_relevance", "context_precision",
            "context_recall", "citation_present_rate", "hallucination_rate",
        ]
        metric_results = await asyncio.gather(*tasks, return_exceptions=True)

        score_dict: Dict[str, Optional[float]] = {}
        for name, res in zip(_NAMES, metric_results):
            if isinstance(res, Exception):
                logger.error("Metric %s failed: %s", name, res)
                score_dict[name] = None
            else:
                score_dict[name] = res.score

        scores = ScoreMetrics(**score_dict)

        token_metrics = None
        if input_tokens is not None and output_tokens is not None:
            token_metrics = TokenMetrics(
                query_tokens=input_tokens,
                response_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            )

        latency_metrics = LatencyMetrics(
            retrieval_ms=retrieval_time_ms,
            generation_ms=generation_time_ms,
            total_ms=retrieval_time_ms + generation_time_ms,
        )

        cost_metrics = None
        if token_metrics:
            qc = (token_metrics.query_tokens / 1000) * _COST_INPUT_PER_1K
            rc = (token_metrics.response_tokens / 1000) * _COST_OUTPUT_PER_1K
            cost_metrics = CostMetrics(query_cost=qc, response_cost=rc, total_cost=qc + rc)

        result = EvaluationResult(
            item_id=item_id,
            question=question,
            answer=answer,
            contexts=contexts,
            expected_answer=expected_answer,
            expected_sources=expected_sources,
            scores=scores,
            token_metrics=token_metrics,
            latency_metrics=latency_metrics,
            cost_metrics=cost_metrics,
        )
        logger.info("Evaluated %s score=%.3f", item_id, result.overall_score())
        return result

    def evaluate_golden_item(self, item_id: str, question: str, answer: str,
                              contexts: List[str], **kwargs) -> EvaluationResult:
        return asyncio.run(
            self.evaluate_golden_item_async(
                item_id=item_id, question=question, answer=answer,
                contexts=contexts, **kwargs,
            )
        )

    # ── Batch ────────────────────────────────────────────────────────────────

    async def evaluate_golden_set_async(
        self,
        batch_id: str,
        golden_items: List[Dict[str, Any]],
        max_concurrent: int = 5,
    ) -> EvaluationReport:
        start = datetime.now(timezone.utc)
        semaphore = asyncio.Semaphore(max_concurrent)
        failed_count = 0

        async def _eval_one(item: Dict) -> Optional[EvaluationResult]:
            nonlocal failed_count
            async with semaphore:
                try:
                    return await self.evaluate_golden_item_async(
                        item_id=item.get("id", "?"),
                        question=item["question"],
                        answer=item["answer"],
                        contexts=item.get("contexts", []),
                        expected_answer=item.get("expected_answer"),
                        expected_sources=item.get("expected_sources"),
                        retrieved_source_ids=item.get("retrieved_source_ids"),
                        retrieval_time_ms=item.get("retrieval_time_ms", 0.0),
                        generation_time_ms=item.get("generation_time_ms", 0.0),
                        input_tokens=item.get("input_tokens"),
                        output_tokens=item.get("output_tokens"),
                    )
                except Exception as exc:
                    logger.error("Failed item %s: %s", item.get("id"), exc)
                    failed_count += 1
                    return None

        raw = await asyncio.gather(*[_eval_one(it) for it in golden_items])
        results = [r for r in raw if r is not None]

        total_tokens = sum(r.token_metrics.total_tokens for r in results if r.token_metrics)
        total_latency = sum(r.latency_metrics.total_ms for r in results if r.latency_metrics)
        total_cost = sum(r.cost_metrics.total_cost for r in results if r.cost_metrics)

        completed_at = datetime.now(timezone.utc)
        duration = (completed_at - start).total_seconds()

        report = EvaluationReport(
            batch_id=batch_id,
            total_items=len(golden_items),
            successful_items=len(results),
            failed_items=failed_count,
            scores_summary=self._compute_statistics(results),
            total_tokens=total_tokens,
            total_latency_ms=total_latency,
            total_cost=total_cost,
            results=results,
            completed_at=completed_at,
            duration_seconds=duration,
        )
        logger.info("Batch %s: %d/%d items, score=%.3f",
                    batch_id, len(results), len(golden_items), report.overall_score())
        return report

    def evaluate_golden_set(self, batch_id: str, golden_items: List[Dict[str, Any]],
                             **kwargs) -> EvaluationReport:
        return asyncio.run(
            self.evaluate_golden_set_async(batch_id=batch_id, golden_items=golden_items, **kwargs)
        )

    @staticmethod
    def _compute_statistics(results: List[EvaluationResult]) -> Dict[str, Dict[str, Any]]:
        fields = [
            "faithfulness", "answer_relevance", "context_precision",
            "context_recall", "citation_present_rate", "hallucination_rate",
        ]
        stats: Dict[str, Dict[str, Any]] = {}
        for f in fields:
            scores = [getattr(r.scores, f) for r in results if getattr(r.scores, f) is not None]
            if scores:
                stats[f] = {
                    "min": min(scores),
                    "max": max(scores),
                    "mean": mean(scores),
                    "median": median(scores),
                    "std": stdev(scores) if len(scores) > 1 else 0.0,
                    "count": len(scores),
                }
            else:
                stats[f] = {"min": None, "max": None, "mean": None,
                             "median": None, "std": None, "count": 0}
        return stats
