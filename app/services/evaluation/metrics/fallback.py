"""
규칙 기반 폴백 메커니즘 — Phase 7 FG7.2

LLM 사용 불가 시 텍스트 오버랩 / 키워드 유사도로 대체 평가.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import List, Tuple

logger = logging.getLogger(__name__)


class TextOverlapCalculator:
    @staticmethod
    def calculate_word_overlap(text1: str, text2: str) -> float:
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        if not words1:
            return 0.0
        return min(len(words1 & words2) / len(words1), 1.0)

    @staticmethod
    def calculate_sequence_similarity(text1: str, text2: str) -> float:
        return SequenceMatcher(None, text1.lower(), text2.lower()).ratio()


class KeywordExtractor:
    ENGLISH_STOPWORDS = frozenset({
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "must", "shall", "it", "this", "that",
        "what", "which", "who", "whom", "when", "where", "why", "how",
    })

    @classmethod
    def extract_keywords(cls, text: str, top_k: int = 10) -> List[str]:
        words = [
            w.lower()
            for w in re.findall(r"\b[a-zA-Z]+\b", text)
            if w.lower() not in cls.ENGLISH_STOPWORDS and len(w) > 2
        ]
        return [w for w, _ in Counter(words).most_common(top_k)]

    @classmethod
    def keyword_overlap(cls, text1: str, text2: str) -> float:
        kw1 = set(cls.extract_keywords(text1))
        kw2 = set(cls.extract_keywords(text2))
        if not kw1:
            return 0.0
        return min(len(kw1 & kw2) / len(kw1), 1.0)


class FaithfulnessFallback:
    @staticmethod
    def calculate_faithfulness(
        answer: str,
        contexts: List[str],
        method: str = "overlap",
    ) -> float:
        if not answer or not contexts:
            return 0.0
        ctx = " ".join(contexts)

        if method == "overlap":
            score = TextOverlapCalculator.calculate_word_overlap(answer, ctx)
        elif method == "sequence":
            score = TextOverlapCalculator.calculate_sequence_similarity(answer, ctx)
        elif method == "keyword":
            score = KeywordExtractor.keyword_overlap(answer, ctx)
        elif method == "ensemble":
            o = TextOverlapCalculator.calculate_word_overlap(answer, ctx)
            s = TextOverlapCalculator.calculate_sequence_similarity(answer, ctx)
            k = KeywordExtractor.keyword_overlap(answer, ctx)
            score = (o + s + k) / 3.0
        else:
            logger.warning("Unknown faithfulness method: %s, using overlap", method)
            score = TextOverlapCalculator.calculate_word_overlap(answer, ctx)

        return min(max(score, 0.0), 1.0)


class ContextPrecisionFallback:
    _THRESHOLD = 0.3

    @classmethod
    def calculate_context_precision(
        cls,
        question: str,
        answer: str,
        contexts: List[str],
        method: str = "keyword",
    ) -> Tuple[float, List[float]]:
        if not contexts:
            return 1.0, []

        qa_text = question + " " + answer
        chunk_scores: List[float] = []

        for ctx in contexts:
            if method == "keyword":
                score = KeywordExtractor.keyword_overlap(qa_text, ctx)
            elif method == "overlap":
                score = TextOverlapCalculator.calculate_word_overlap(answer, ctx)
            elif method == "ensemble":
                k = KeywordExtractor.keyword_overlap(qa_text, ctx)
                o = TextOverlapCalculator.calculate_word_overlap(answer, ctx)
                score = (k + o) / 2.0
            else:
                logger.warning("Unknown context precision method: %s", method)
                score = 0.0
            chunk_scores.append(1.0 if score >= cls._THRESHOLD else 0.0)

        avg = sum(chunk_scores) / len(chunk_scores)
        return min(max(avg, 0.0), 1.0), chunk_scores
