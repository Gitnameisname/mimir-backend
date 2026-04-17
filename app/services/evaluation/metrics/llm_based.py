"""
LLM 기반 평가 지표 — Phase 7 FG7.2

Faithfulness, Context Precision을 LLM Judge로 계산.
LLM 불가 시 규칙 기반 폴백 사용 (S2 원칙 ⑦).
"""
from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .fallback import ContextPrecisionFallback, FaithfulnessFallback
from .rule_based import MetricCalculator, MetricScore
from ..prompts.judge_prompts import (
    PromptLanguage,
    get_context_precision_prompt,
    get_faithfulness_prompt,
)

logger = logging.getLogger(__name__)


class LLMJudge(ABC):
    @abstractmethod
    def judge_sync(self, prompt: str) -> str:
        pass


class LLMProviderJudge(LLMJudge):
    def __init__(self, llm_provider: Any, max_retries: int = 3, timeout: int = 30) -> None:
        self._provider = llm_provider
        self._max_retries = max_retries
        self._timeout = timeout

    def judge_sync(self, prompt: str) -> str:
        for attempt in range(self._max_retries):
            try:
                return self._provider.generate(prompt)
            except Exception as exc:
                logger.error("LLM judge attempt %d failed: %s", attempt + 1, exc)
                if attempt < self._max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise


class JSONResponseParser:
    @staticmethod
    def _extract_json(response: str) -> Optional[Dict]:
        start = response.find("{")
        if start == -1:
            return None
        snippet = response[start:]
        end = snippet.rfind("}") + 1
        try:
            return json.loads(snippet[:end])
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("JSON parse failed: %s", exc)
            return None

    @classmethod
    def parse_faithfulness_response(cls, response: str) -> Optional[Dict]:
        return cls._extract_json(response)

    @classmethod
    def parse_context_precision_response(cls, response: str) -> Optional[Dict]:
        return cls._extract_json(response)


class FaithfulnessMetric(MetricCalculator):
    """Faithfulness = verified_claims / total_claims (LLM + fallback)."""

    def __init__(
        self,
        llm_judge: Optional[LLMJudge] = None,
        llm_enabled: Optional[bool] = None,
        language: PromptLanguage = PromptLanguage.ENGLISH,
        fallback_method: str = "ensemble",
    ) -> None:
        self._judge = llm_judge
        self._llm_enabled: bool = (
            llm_enabled
            if llm_enabled is not None
            else os.getenv("EVAL_LLM_ENABLED", "false").lower() == "true"
        )
        self._language = language
        self._fallback_method = fallback_method
        self._prompt = get_faithfulness_prompt(language)

    @property
    def metric_name(self) -> str:
        return "faithfulness"

    def compute(
        self,
        question: str = "",
        answer_text: str = "",
        contexts: List[str] = (),
        **kwargs,
    ) -> MetricScore:
        if not answer_text or not contexts:
            return MetricScore(
                metric_name=self.metric_name,
                score=0.0,
                details={"reason": "missing answer or contexts"},
            )

        if self._llm_enabled and self._judge and self._prompt:
            try:
                prompt = self._prompt.render(question, answer_text, list(contexts))
                raw = self._judge.judge_sync(prompt)
                parsed = JSONResponseParser.parse_faithfulness_response(raw)
                if parsed and "overall_faithfulness" in parsed:
                    score = float(parsed["overall_faithfulness"])
                    return MetricScore(
                        metric_name=self.metric_name,
                        score=min(max(score, 0.0), 1.0),
                        details={
                            "method": "llm_judge",
                            "claims": parsed.get("claims", []),
                        },
                    )
            except Exception as exc:
                logger.warning("LLM faithfulness failed, using fallback: %s", exc)

        score = FaithfulnessFallback.calculate_faithfulness(
            answer_text, list(contexts), method=self._fallback_method
        )
        return MetricScore(
            metric_name=self.metric_name,
            score=score,
            details={"method": f"fallback_{self._fallback_method}"},
        )


class ContextPrecisionMetric(MetricCalculator):
    """Context Precision = relevant_chunks / total_chunks (LLM + fallback)."""

    def __init__(
        self,
        llm_judge: Optional[LLMJudge] = None,
        llm_enabled: Optional[bool] = None,
        language: PromptLanguage = PromptLanguage.ENGLISH,
        fallback_method: str = "ensemble",
    ) -> None:
        self._judge = llm_judge
        self._llm_enabled: bool = (
            llm_enabled
            if llm_enabled is not None
            else os.getenv("EVAL_LLM_ENABLED", "false").lower() == "true"
        )
        self._language = language
        self._fallback_method = fallback_method
        self._prompt = get_context_precision_prompt(language)

    @property
    def metric_name(self) -> str:
        return "context_precision"

    def compute(
        self,
        question: str = "",
        answer_text: str = "",
        contexts: List[str] = (),
        **kwargs,
    ) -> MetricScore:
        ctx_list = list(contexts)
        if not ctx_list:
            return MetricScore(
                metric_name=self.metric_name,
                score=1.0,
                details={"num_contexts": 0},
            )

        if self._llm_enabled and self._judge and self._prompt:
            try:
                prompt = self._prompt.render(question, answer_text, ctx_list)
                raw = self._judge.judge_sync(prompt)
                parsed = JSONResponseParser.parse_context_precision_response(raw)
                if parsed and "average_precision" in parsed:
                    score = float(parsed["average_precision"])
                    return MetricScore(
                        metric_name=self.metric_name,
                        score=min(max(score, 0.0), 1.0),
                        details={
                            "method": "llm_judge",
                            "context_scores": parsed.get("context_scores", []),
                            "num_contexts": len(ctx_list),
                        },
                    )
            except Exception as exc:
                logger.warning("LLM context precision failed, using fallback: %s", exc)

        score, chunk_scores = ContextPrecisionFallback.calculate_context_precision(
            question, answer_text, ctx_list, method=self._fallback_method
        )
        return MetricScore(
            metric_name=self.metric_name,
            score=score,
            details={
                "method": f"fallback_{self._fallback_method}",
                "num_contexts": len(ctx_list),
                "relevant_contexts": sum(1 for s in chunk_scores if s > 0.5),
            },
        )
