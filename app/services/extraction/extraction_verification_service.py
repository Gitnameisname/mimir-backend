"""
ExtractionVerificationService — Phase 8 FG8.3 (task8-9).

동일 조건 재추출 후 결과를 비교 검증한다.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.models.extraction_record import (
    DiffDetail,
    ExtractionRecord,
    MatchStatus,
    VerificationResult,
)
from app.services.extraction.diff_calculator import DiffCalculator
from app.utils.time import utcnow
from app.utils.converters import uuid_str_or_none

logger = logging.getLogger(__name__)


class ExtractionVerificationService:
    """
    ExtractionRecord를 기반으로 재추출 + diff 검증을 수행한다.

    결정론적 모드 강제화:
      - temperature 오버라이드 기본값 = 0.0
      - seed 파라미터 전달 (모델 지원 시)
    """

    def __init__(self, diff_calculator: Optional[DiffCalculator] = None):
        self._diff = diff_calculator or DiffCalculator()

    def verify(
        self,
        original_record: ExtractionRecord,
        new_extracted_result: Dict[str, Any],
        fields_to_verify: Optional[List[str]] = None,
        verified_by: str = "system",
        actor_type: str = "user",
    ) -> VerificationResult:
        """
        original_record의 extracted_result와 new_extracted_result를 비교한다.

        실제 LLM 재호출은 호출자가 담당하며, 이 메서드는 순수한 비교 로직만 실행한다.
        """
        original = original_record.extracted_result or {}
        new = new_extracted_result or {}

        match_status, diffs = self._diff.compare(original, new, fields_to_verify)

        total = len(set(original.keys()) | set(new.keys()))
        if fields_to_verify:
            total = len([k for k in fields_to_verify if k in original or k in new])

        exact_count = sum(1 for d in diffs if d.match_type == "exact")
        field_accuracy = exact_count / total if total > 0 else 1.0

        return VerificationResult(
            extraction_candidate_id=original_record.extraction_candidate_id,
            verified_at=utcnow(),
            match_status=match_status,
            field_match_count=exact_count,
            field_total_count=total,
            field_accuracy=field_accuracy,
            diff_details=diffs,
            verified_by=verified_by,
            actor_type=actor_type,
        )

    def build_audit_trail(
        self,
        record: ExtractionRecord,
        verification_results: List[VerificationResult],
    ) -> Dict[str, Any]:
        """추출 이력(audit trail)을 dict 형태로 반환한다."""
        return {
            "extraction_candidate_id": str(record.extraction_candidate_id),
            "document_id": str(record.document_id),
            "document_version": record.document_version,
            "document_content_hash": record.document_content_hash,
            "extraction_model": record.extraction_model,
            "extraction_mode": record.extraction_mode,
            "temperature": record.temperature,
            "schema_id": record.extraction_schema_id,
            "schema_version": record.extraction_schema_version,
            "extracted_at": record.extracted_timestamp.isoformat()
            if record.extracted_timestamp else None,
            "extracted_field_count": len(record.extracted_result) if record.extracted_result else 0,
            "extracted_result_hash": self.compute_document_hash(
                str(sorted(record.extracted_result.items()) if record.extracted_result else "")
            ),
            "verification_count": len(verification_results),
            "verifications": [
                {
                    "id": uuid_str_or_none(vr.id),
                    "verified_at": vr.verified_at.isoformat(),
                    "match_status": vr.match_status.value,
                    "field_accuracy": vr.field_accuracy,
                }
                for vr in verification_results
            ],
        }

    @staticmethod
    def compute_document_hash(document_text: str) -> str:
        return hashlib.sha256(document_text.encode("utf-8")).hexdigest()
