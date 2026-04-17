"""
DiffCalculator — Phase 8 FG8.3 (task8-9).

두 추출 결과 사이의 필드별 차이점을 계산한다.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.models.extraction_record import DiffDetail, MatchStatus

logger = logging.getLogger(__name__)

_FLOAT_EPSILON = 1e-6


def _levenshtein_similarity(a: str, b: str) -> float:
    """두 문자열의 유사도 (0~1, Levenshtein 기반)."""
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 0.0

    # DP (공간 최적화)
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        curr = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr

    edit_dist = prev[lb]
    return 1.0 - edit_dist / max(la, lb)


def _span_iou(span_a: Tuple[int, int], span_b: Tuple[int, int]) -> float:
    """두 오프셋 구간의 IoU(Intersection over Union)."""
    start_a, end_a = span_a
    start_b, end_b = span_b

    intersection = max(0, min(end_a, end_b) - max(start_a, start_b))
    union = (end_a - start_a) + (end_b - start_b) - intersection
    if union <= 0:
        return 0.0
    return intersection / union


_MAX_RECURSION_DEPTH = 10


class DiffCalculator:
    """
    두 dict(추출 결과) 사이의 필드별 차이점을 계산한다.

    - 문자열: exact match → fuzzy (Levenshtein)
    - 숫자: floating point tolerance
    - 배열/dict: deep diff (최대 _MAX_RECURSION_DEPTH 깊이)
    """

    def __init__(self, fuzzy_threshold: float = 0.8):
        self._fuzzy_threshold = fuzzy_threshold

    def compare(
        self,
        original: Dict[str, Any],
        new: Dict[str, Any],
        fields_filter: Optional[List[str]] = None,
        _depth: int = 0,
    ) -> Tuple[MatchStatus, List[DiffDetail]]:
        """original과 new를 비교하여 (MatchStatus, diff 목록)을 반환한다."""
        if _depth > _MAX_RECURSION_DEPTH:
            return MatchStatus.MISMATCH, []

        all_keys = set(original.keys()) | set(new.keys())
        if fields_filter:
            all_keys = all_keys & set(fields_filter)

        diffs: List[DiffDetail] = []
        match_count = 0

        for key in sorted(all_keys):
            orig_val = original.get(key)
            new_val = new.get(key)

            if key not in original:
                diffs.append(DiffDetail(
                    field_name=key,
                    original_value=None,
                    new_value=new_val,
                    match_type="missing",
                    similarity=0.0,
                ))
                continue

            if key not in new:
                diffs.append(DiffDetail(
                    field_name=key,
                    original_value=orig_val,
                    new_value=None,
                    match_type="missing",
                    similarity=0.0,
                ))
                continue

            detail = self._compare_values(key, orig_val, new_val)
            diffs.append(detail)
            if detail.similarity is not None and detail.similarity >= self._fuzzy_threshold:
                match_count += 1
            elif detail.similarity is None and detail.match_type == "exact":
                match_count += 1

        total = len(all_keys)
        if total == 0:
            return MatchStatus.IDENTICAL, []

        exact_matches = sum(
            1 for d in diffs if d.match_type == "exact"
        )

        if exact_matches == total:
            status = MatchStatus.IDENTICAL
        elif match_count > 0:
            status = MatchStatus.PARTIAL
        else:
            status = MatchStatus.MISMATCH

        return status, diffs

    def _compare_values(
        self,
        field_name: str,
        a: Any,
        b: Any,
    ) -> DiffDetail:
        # None 체크
        if a is None and b is None:
            return DiffDetail(field_name=field_name, original_value=a, new_value=b,
                              match_type="exact", similarity=1.0)
        if a is None or b is None:
            return DiffDetail(field_name=field_name, original_value=a, new_value=b,
                              match_type="mismatch", similarity=0.0)

        # 타입 불일치
        if type(a) != type(b):
            # 숫자 계열끼리는 허용
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                pass
            else:
                return DiffDetail(field_name=field_name, original_value=a, new_value=b,
                                  match_type="type_mismatch", similarity=0.0)

        # 정확 일치
        if a == b:
            return DiffDetail(field_name=field_name, original_value=a, new_value=b,
                              match_type="exact", similarity=1.0)

        # 숫자: epsilon 비교
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(float(a) - float(b)) <= _FLOAT_EPSILON:
                return DiffDetail(field_name=field_name, original_value=a, new_value=b,
                                  match_type="exact", similarity=1.0)
            max_val = max(abs(float(a)), abs(float(b)))
            sim = 1.0 - (abs(float(a) - float(b)) / max_val) if max_val > 0 else 0.0
            return DiffDetail(field_name=field_name, original_value=a, new_value=b,
                              match_type="fuzzy", similarity=max(0.0, sim))

        # 문자열: fuzzy
        if isinstance(a, str) and isinstance(b, str):
            sim = _levenshtein_similarity(a, b)
            match_type = "exact" if sim >= 1.0 else "fuzzy"
            return DiffDetail(field_name=field_name, original_value=a, new_value=b,
                              match_type=match_type, similarity=sim)

        # 리스트: 원소 단위 비교
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                return DiffDetail(field_name=field_name, original_value=a, new_value=b,
                                  match_type="mismatch", similarity=0.0)
            similarities = []
            for ai, bi in zip(a, b):
                sub = self._compare_values(field_name, ai, bi)
                similarities.append(sub.similarity or (1.0 if sub.match_type == "exact" else 0.0))
            sim = sum(similarities) / len(similarities) if similarities else 1.0
            return DiffDetail(field_name=field_name, original_value=a, new_value=b,
                              match_type="fuzzy" if sim < 1.0 else "exact", similarity=sim)

        # dict: 재귀
        if isinstance(a, dict) and isinstance(b, dict):
            _, sub_diffs = self.compare(a, b, _depth=_depth + 1)
            if not sub_diffs:
                return DiffDetail(field_name=field_name, original_value=a, new_value=b,
                                  match_type="exact", similarity=1.0)
            sims = [d.similarity or 0.0 for d in sub_diffs]
            avg = sum(sims) / len(sims)
            return DiffDetail(field_name=field_name, original_value=a, new_value=b,
                              match_type="fuzzy" if avg < 1.0 else "exact", similarity=avg)

        return DiffDetail(field_name=field_name, original_value=a, new_value=b,
                          match_type="mismatch", similarity=0.0)


class SpanBasedDiffCalculator:
    """IoU(Intersection over Union) 기반 span 비교."""

    def compare_spans(
        self,
        original_spans: List[Tuple[int, int]],
        new_spans: List[Tuple[int, int]],
    ) -> float:
        """두 스팬 목록 사이의 평균 IoU를 반환한다."""
        if not original_spans and not new_spans:
            return 1.0
        if not original_spans or not new_spans:
            return 0.0

        # 각 original span에 대해 best-matching new span의 IoU
        ious = []
        for orig in original_spans:
            best = max(_span_iou(orig, n) for n in new_spans)
            ious.append(best)

        return sum(ious) / len(ious)
