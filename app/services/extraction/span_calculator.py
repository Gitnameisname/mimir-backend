"""
SpanCalculator — Phase 8 FG8.3 (task8-8).

LLM 추출 결과에서 원문 위치(SourceSpan)를 계산한다.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.models.extraction_span import (
    ExtractedFieldWithAttribution,
    ExtractionResultWithAttribution,
    SourceSpan,
    SpanHighlight,
)

logger = logging.getLogger(__name__)


class SpanCalculator:
    """
    문서 텍스트에서 추출값의 character offset을 계산한다.

    - find_text_in_document: 첫 번째 출현 위치 탐색
    - find_all_occurrences: 전체 출현 위치 탐색
    - verify_span_text: 스팬 내용 검증
    - merge_overlapping_spans: 겹치는 스팬 병합
    - create_source_span: SourceSpan 객체 생성
    - calculate_content_hash: 텍스트 해시 계산
    """

    def find_text_in_document(
        self,
        document_text: str,
        search_text: str,
        start_hint: int = 0,
    ) -> Optional[Tuple[int, int]]:
        """문서 텍스트에서 search_text의 첫 출현 오프셋을 반환한다."""
        if not search_text or not document_text:
            return None

        idx = document_text.find(search_text, start_hint)
        if idx == -1:
            # 대소문자 무시 재시도
            lower_doc = document_text.lower()
            lower_search = search_text.lower()
            idx = lower_doc.find(lower_search, start_hint)
            if idx == -1:
                return None

        return (idx, idx + len(search_text))

    def find_all_occurrences(
        self,
        document_text: str,
        search_text: str,
    ) -> List[Tuple[int, int]]:
        """문서 텍스트에서 search_text의 모든 출현 오프셋 목록을 반환한다."""
        if not search_text or not document_text:
            return []

        results = []
        start = 0
        while True:
            idx = document_text.find(search_text, start)
            if idx == -1:
                break
            results.append((idx, idx + len(search_text)))
            start = idx + 1
        return results

    def verify_span_text(
        self,
        document_text: str,
        span_offset: Tuple[int, int],
        expected_text: str,
    ) -> bool:
        """스팬 오프셋의 실제 텍스트가 expected_text와 일치하는지 검증한다."""
        start, end = span_offset
        if start < 0 or end > len(document_text) or start >= end:
            return False
        actual = document_text[start:end]
        return actual == expected_text

    def merge_overlapping_spans(
        self,
        spans: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        """겹치거나 인접한 오프셋 튜플들을 병합한다."""
        if not spans:
            return []

        sorted_spans = sorted(spans, key=lambda s: s[0])
        merged = [sorted_spans[0]]

        for current in sorted_spans[1:]:
            last = merged[-1]
            if current[0] <= last[1]:  # 겹치거나 인접
                merged[-1] = (last[0], max(last[1], current[1]))
            else:
                merged.append(current)

        return merged

    def create_source_span(
        self,
        document_id: UUID,
        document_text: str,
        span_offset: Tuple[int, int],
        version_id: Optional[UUID] = None,
        node_id: Optional[UUID] = None,
    ) -> Optional[SourceSpan]:
        """오프셋에서 SourceSpan 객체를 생성한다."""
        start, end = span_offset
        if start < 0 or end > len(document_text) or start >= end:
            return None

        source_text = document_text[start:end]
        return SourceSpan(
            document_id=document_id,
            version_id=version_id,
            node_id=node_id,
            span_offset=span_offset,
            source_text=source_text,
            content_hash=self.calculate_content_hash(source_text),
        )

    def calculate_content_hash(self, text: str) -> str:
        """텍스트의 SHA-256 해시를 반환한다."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def extract_spans_from_value(
        self,
        document_text: str,
        document_id: UUID,
        field_name: str,
        extracted_value: Any,
        version_id: Optional[UUID] = None,
    ) -> List[SourceSpan]:
        """추출값(문자열 또는 문자열 리스트)을 문서에서 찾아 SourceSpan 목록을 반환한다."""
        spans = []

        texts_to_find: List[str] = []
        if isinstance(extracted_value, str) and extracted_value.strip():
            texts_to_find.append(extracted_value.strip())
        elif isinstance(extracted_value, list):
            for item in extracted_value:
                if isinstance(item, str) and item.strip():
                    texts_to_find.append(item.strip())

        for text in texts_to_find:
            offset = self.find_text_in_document(document_text, text)
            if offset:
                span = self.create_source_span(
                    document_id=document_id,
                    document_text=document_text,
                    span_offset=offset,
                    version_id=version_id,
                )
                if span:
                    spans.append(span)

        return spans


class MultiSpanExtractor:
    """
    LLM 응답의 여러 필드에서 SourceSpan을 일괄 추출한다.
    """

    def __init__(self, calculator: Optional[SpanCalculator] = None):
        self._calc = calculator or SpanCalculator()

    def extract(
        self,
        document_text: str,
        document_id: UUID,
        extracted_fields: Dict[str, Any],
        version_id: Optional[UUID] = None,
        extraction_candidate_id: Optional[UUID] = None,
    ) -> ExtractionResultWithAttribution:
        """모든 추출 필드에서 attribution span을 계산한 ExtractionResultWithAttribution을 반환한다."""
        fields_with_attr: List[ExtractedFieldWithAttribution] = []

        from uuid import uuid4
        cid = extraction_candidate_id or uuid4()

        for field_name, value in extracted_fields.items():
            spans = self._calc.extract_spans_from_value(
                document_text=document_text,
                document_id=document_id,
                field_name=field_name,
                extracted_value=value,
                version_id=version_id,
            )

            # 겹치는 오프셋 병합
            if len(spans) > 1:
                offsets = [(s.span_offset[0], s.span_offset[1]) for s in spans]
                merged_offsets = self._calc.merge_overlapping_spans(offsets)
                if len(merged_offsets) < len(spans):
                    merged_spans = []
                    for offset in merged_offsets:
                        span = self._calc.create_source_span(
                            document_id=document_id,
                            document_text=document_text,
                            span_offset=offset,
                            version_id=version_id,
                        )
                        if span:
                            merged_spans.append(span)
                    spans = merged_spans

            fields_with_attr.append(
                ExtractedFieldWithAttribution(
                    field_name=field_name,
                    extracted_value=value,
                    source_spans=spans,
                    spans_merged=False,
                )
            )

        return ExtractionResultWithAttribution(
            extraction_candidate_id=cid,
            document_id=document_id,
            fields=fields_with_attr,
        )


class SpanVisualizationConverter:
    """
    ExtractionResultWithAttribution을 UI 하이라이트 배열로 변환한다.
    """

    def to_highlights(
        self,
        result: ExtractionResultWithAttribution,
    ) -> List[SpanHighlight]:
        """모든 필드의 스팬을 start 순으로 정렬한 SpanHighlight 목록을 반환한다."""
        highlights: List[SpanHighlight] = []

        for field in result.fields:
            for span in field.source_spans:
                highlights.append(
                    SpanHighlight(
                        start=span.span_offset[0],
                        end=span.span_offset[1],
                        field_name=field.field_name,
                        source_text=span.source_text,
                        confidence=field.confidence,
                    )
                )

        highlights.sort(key=lambda h: h.start)
        return highlights

    def to_highlight_dict(
        self,
        result: ExtractionResultWithAttribution,
    ) -> List[Dict[str, Any]]:
        """SpanHighlight 목록을 JSON 직렬화 가능한 dict 목록으로 변환한다."""
        return [h.model_dump() for h in self.to_highlights(result)]
