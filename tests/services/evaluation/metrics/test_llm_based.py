"""
LLM 기반 평가 지표 단위 테스트 — Phase 7 FG7.2 (task7-5)

테스트 범위:
- FaithfulnessMetric: LLM 성공/실패/비활성화/빈 입력
- ContextPrecisionMetric: LLM 성공/폴백/빈 컨텍스트
- JSONResponseParser: 정상 JSON / 포함 텍스트 / invalid
- FaithfulnessFallback: overlap, ensemble 메서드
- ContextPrecisionFallback: keyword 메서드
"""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from app.services.evaluation.metrics.fallback import (
    ContextPrecisionFallback,
    FaithfulnessFallback,
    KeywordExtractor,
    TextOverlapCalculator,
)
from app.services.evaluation.metrics.llm_based import (
    ContextPrecisionMetric,
    FaithfulnessMetric,
    JSONResponseParser,
    LLMJudge,
)
from app.services.evaluation.prompts.judge_prompts import PromptLanguage


# ---------------------------------------------------------------------------
# Mock judge
# ---------------------------------------------------------------------------

class _MockJudge(LLMJudge):
    def __init__(self, response: dict | str) -> None:
        self._resp = json.dumps(response) if isinstance(response, dict) else response

    def judge_sync(self, prompt: str) -> str:
        return self._resp


# ---------------------------------------------------------------------------
# FaithfulnessMetric
# ---------------------------------------------------------------------------

class TestFaithfulnessMetric:
    CONTEXTS = [
        "Paris is the capital of France.",
        "France is located in Western Europe.",
    ]

    def test_llm_full_score(self):
        mock = _MockJudge({"claims": [{"text": "Paris is the capital", "score": 1}],
                           "overall_faithfulness": 1.0})
        m = FaithfulnessMetric(llm_judge=mock, llm_enabled=True)
        result = m.compute(question="Capital?", answer_text="Paris is capital.", contexts=self.CONTEXTS)
        assert result.score == 1.0
        assert result.details["method"] == "llm_judge"

    def test_llm_zero_score(self):
        mock = _MockJudge({"claims": [], "overall_faithfulness": 0.0})
        m = FaithfulnessMetric(llm_judge=mock, llm_enabled=True)
        result = m.compute(question="Capital?", answer_text="Berlin is capital.", contexts=self.CONTEXTS)
        assert result.score == 0.0

    def test_fallback_when_llm_disabled(self):
        m = FaithfulnessMetric(llm_judge=None, llm_enabled=False)
        result = m.compute(question="Q?", answer_text="Paris capital France.", contexts=self.CONTEXTS)
        assert "fallback" in result.details["method"]
        assert 0.0 <= result.score <= 1.0

    def test_fallback_on_llm_error(self):
        bad_judge = Mock(spec=LLMJudge)
        bad_judge.judge_sync.side_effect = RuntimeError("network error")
        m = FaithfulnessMetric(llm_judge=bad_judge, llm_enabled=True)
        result = m.compute(question="Q?", answer_text="Paris.", contexts=self.CONTEXTS)
        assert "fallback" in result.details["method"]

    def test_empty_answer_zero(self):
        m = FaithfulnessMetric(llm_enabled=False)
        result = m.compute(question="Q?", answer_text="", contexts=self.CONTEXTS)
        assert result.score == 0.0

    def test_empty_contexts_zero(self):
        m = FaithfulnessMetric(llm_enabled=False)
        result = m.compute(question="Q?", answer_text="Some answer.", contexts=[])
        assert result.score == 0.0

    def test_llm_parse_failure_fallback(self):
        mock = _MockJudge("Not JSON at all")
        m = FaithfulnessMetric(llm_judge=mock, llm_enabled=True, fallback_method="overlap")
        result = m.compute(question="Q?", answer_text="Paris capital.", contexts=self.CONTEXTS)
        assert "fallback" in result.details["method"]

    def test_metric_name(self):
        assert FaithfulnessMetric().metric_name == "faithfulness"


# ---------------------------------------------------------------------------
# ContextPrecisionMetric
# ---------------------------------------------------------------------------

class TestContextPrecisionMetric:
    QUESTION = "Who was the first president?"
    ANSWER = "George Washington was the first president."
    CONTEXTS = [
        "George Washington served as the first US president from 1789 to 1797.",
        "The US was founded in 1776.",
    ]

    def test_llm_score(self):
        mock = _MockJudge({
            "context_scores": [{"index": 0, "score": 1}, {"index": 1, "score": 0}],
            "average_precision": 0.5,
        })
        m = ContextPrecisionMetric(llm_judge=mock, llm_enabled=True)
        result = m.compute(question=self.QUESTION, answer_text=self.ANSWER, contexts=self.CONTEXTS)
        assert result.score == 0.5
        assert result.details["method"] == "llm_judge"

    def test_fallback_used_when_disabled(self):
        m = ContextPrecisionMetric(llm_judge=None, llm_enabled=False)
        result = m.compute(question=self.QUESTION, answer_text=self.ANSWER, contexts=self.CONTEXTS)
        assert "fallback" in result.details["method"]
        assert result.details["num_contexts"] == 2

    def test_empty_contexts_returns_one(self):
        m = ContextPrecisionMetric(llm_enabled=False)
        result = m.compute(question="Q?", answer_text="A.", contexts=[])
        assert result.score == 1.0

    def test_fallback_on_llm_error(self):
        bad_judge = Mock(spec=LLMJudge)
        bad_judge.judge_sync.side_effect = RuntimeError("timeout")
        m = ContextPrecisionMetric(llm_judge=bad_judge, llm_enabled=True)
        result = m.compute(question=self.QUESTION, answer_text=self.ANSWER, contexts=self.CONTEXTS)
        assert "fallback" in result.details["method"]

    def test_metric_name(self):
        assert ContextPrecisionMetric().metric_name == "context_precision"


# ---------------------------------------------------------------------------
# JSONResponseParser
# ---------------------------------------------------------------------------

class TestJSONResponseParser:
    def test_valid_faithfulness_json(self):
        resp = json.dumps({"claims": [], "overall_faithfulness": 0.8})
        parsed = JSONResponseParser.parse_faithfulness_response(resp)
        assert parsed is not None
        assert parsed["overall_faithfulness"] == 0.8

    def test_json_embedded_in_text(self):
        inner = json.dumps({"claims": [], "overall_faithfulness": 0.5})
        resp = f"Some preamble text\n{inner}\nTrailing text"
        parsed = JSONResponseParser.parse_faithfulness_response(resp)
        assert parsed is not None
        assert parsed["overall_faithfulness"] == 0.5

    def test_invalid_returns_none(self):
        parsed = JSONResponseParser.parse_faithfulness_response("No JSON here")
        assert parsed is None

    def test_context_precision_json(self):
        resp = json.dumps({"context_scores": [], "average_precision": 0.7})
        parsed = JSONResponseParser.parse_context_precision_response(resp)
        assert parsed["average_precision"] == 0.7


# ---------------------------------------------------------------------------
# FaithfulnessFallback
# ---------------------------------------------------------------------------

class TestFaithfulnessFallback:
    def test_overlap_high_similarity(self):
        answer = "Paris is the capital of France"
        contexts = ["Paris is the capital of France and a beautiful city"]
        score = FaithfulnessFallback.calculate_faithfulness(answer, contexts, method="overlap")
        assert score > 0.7

    def test_overlap_low_similarity(self):
        answer = "Python is a programming language"
        contexts = ["The Eiffel Tower is in Paris"]
        score = FaithfulnessFallback.calculate_faithfulness(answer, contexts, method="overlap")
        assert score < 0.3

    def test_ensemble_returns_valid_range(self):
        score = FaithfulnessFallback.calculate_faithfulness(
            "Some answer", ["Some context"], method="ensemble"
        )
        assert 0.0 <= score <= 1.0

    def test_empty_answer_returns_zero(self):
        score = FaithfulnessFallback.calculate_faithfulness("", ["context"])
        assert score == 0.0

    def test_keyword_method(self):
        answer = "Docker container platform"
        contexts = ["Docker is a container platform for running applications"]
        score = FaithfulnessFallback.calculate_faithfulness(answer, contexts, method="keyword")
        assert score > 0.0

    def test_unknown_method_uses_overlap(self):
        score = FaithfulnessFallback.calculate_faithfulness(
            "Paris capital", ["Paris is capital"], method="unknown_method"
        )
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# ContextPrecisionFallback
# ---------------------------------------------------------------------------

class TestContextPrecisionFallback:
    def test_keyword_method(self):
        score, chunks = ContextPrecisionFallback.calculate_context_precision(
            question="Who was first president?",
            answer="George Washington",
            contexts=[
                "George Washington was first US president",
                "Eiffel Tower is in Paris",
            ],
            method="keyword",
        )
        assert len(chunks) == 2
        assert 0.0 <= score <= 1.0

    def test_empty_contexts(self):
        score, chunks = ContextPrecisionFallback.calculate_context_precision(
            question="Q?", answer="A", contexts=[]
        )
        assert score == 1.0
        assert chunks == []

    def test_overlap_method(self):
        score, chunks = ContextPrecisionFallback.calculate_context_precision(
            question="Q?",
            answer="Paris capital France",
            contexts=["Paris is the capital of France"],
            method="overlap",
        )
        assert chunks[0] == 1.0

    def test_ensemble_method(self):
        score, chunks = ContextPrecisionFallback.calculate_context_precision(
            question="Q?", answer="answer text", contexts=["answer context text"],
            method="ensemble",
        )
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# TextOverlapCalculator & KeywordExtractor
# ---------------------------------------------------------------------------

class TestTextOverlapCalculator:
    def test_identical_texts(self):
        score = TextOverlapCalculator.calculate_word_overlap("hello world", "hello world")
        assert score == 1.0

    def test_no_overlap(self):
        score = TextOverlapCalculator.calculate_word_overlap("foo bar", "baz qux")
        assert score == 0.0

    def test_empty_text1(self):
        score = TextOverlapCalculator.calculate_word_overlap("", "hello")
        assert score == 0.0

    def test_sequence_similarity(self):
        score = TextOverlapCalculator.calculate_sequence_similarity("abc", "abc")
        assert score == 1.0


class TestKeywordExtractor:
    def test_extracts_keywords(self):
        kws = KeywordExtractor.extract_keywords("Docker is a container platform for running apps")
        assert "docker" in kws or "container" in kws

    def test_stopwords_removed(self):
        kws = KeywordExtractor.extract_keywords("the and or is are")
        assert kws == []

    def test_keyword_overlap(self):
        score = KeywordExtractor.keyword_overlap(
            "Docker container", "Docker is a container runtime"
        )
        assert score > 0.0
