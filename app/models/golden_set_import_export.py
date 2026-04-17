"""
Golden Set import/export JSON 스키마 — Phase 7 FG7.1

Import 형식:
{
  "format_version": "1.0",
  "name": "기술 문서 RAG",   (optional — 메타데이터 갱신용)
  "description": "...",       (optional)
  "domain": "technical_guide",(optional)
  "items": [
    {
      "question": "...",
      "expected_answer": "...",
      "expected_source_docs": [{document_id, version_id, node_id}],
      "expected_citations": [{...5-tuple...}],   (optional)
      "notes": "..."                             (optional)
    }
  ]
}

Export 형식 = Import 형식 + id/scope_id/status/version/timestamps.
"""
from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator

from app.models.golden_set import Citation5Tuple, SourceRef

_MAX_IMPORT_ITEMS = 5000  # 단일 import 최대 항목 수


# ---------------------------------------------------------------------------
# Import item / request
# ---------------------------------------------------------------------------

class GoldenSetImportItem(BaseModel):
    """Import/export 공통 Q&A 항목."""
    question: str = Field(..., min_length=1, max_length=2000)
    expected_answer: str = Field(..., min_length=1, max_length=5000)
    expected_source_docs: list[SourceRef] = Field(..., min_length=1)
    expected_citations: list[Citation5Tuple] = Field(default_factory=list)
    notes: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("expected_source_docs")
    @classmethod
    def require_source_docs(cls, v: list[SourceRef]) -> list[SourceRef]:
        if not v:
            raise ValueError("expected_source_docs must contain at least one reference")
        return v


class GoldenSetImportRequest(BaseModel):
    """JSON import 요청 스키마."""
    format_version: str = Field(default="1.0")
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    domain: Optional[str] = None
    items: list[GoldenSetImportItem] = Field(..., min_length=1)

    @field_validator("items")
    @classmethod
    def check_max_items(cls, v: list) -> list:
        if len(v) > _MAX_IMPORT_ITEMS:
            raise ValueError(f"items는 최대 {_MAX_IMPORT_ITEMS}개까지 허용됩니다.")
        return v


# ---------------------------------------------------------------------------
# Export response
# ---------------------------------------------------------------------------

class GoldenSetExportResponse(BaseModel):
    """JSON export 응답 (import와 왕복 호환)."""
    format_version: str = "1.0"
    id: str
    scope_id: str
    name: str
    description: Optional[str]
    domain: str
    status: str
    golden_set_version: int
    created_at: str   # ISO 8601
    created_by: str
    updated_at: str   # ISO 8601
    updated_by: Optional[str]
    items: list[GoldenSetImportItem]


# ---------------------------------------------------------------------------
# Import result
# ---------------------------------------------------------------------------

class ImportItemResult(BaseModel):
    index: int
    question: str
    success: bool
    error: Optional[str] = None


class GoldenSetImportResult(BaseModel):
    total_items: int
    successful_items: int
    failed_items: int
    created_item_ids: list[str]
    errors: list[ImportItemResult] = Field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.total_items == 0:
            return 1.0
        return self.successful_items / self.total_items


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class ImportValidator:
    @staticmethod
    def validate(request: GoldenSetImportRequest) -> tuple[bool, list[str]]:
        """중복 question 감지 및 데이터 의미론 검증."""
        errors: list[str] = []
        seen: set[str] = set()

        for idx, item in enumerate(request.items):
            if item.question in seen:
                errors.append(f"index {idx}: 중복된 question — '{item.question[:60]}'")
            seen.add(item.question)

            for ci, cite in enumerate(item.expected_citations):
                if cite.content_hash == "":
                    errors.append(f"index {idx}, citation {ci}: content_hash가 비어있습니다.")

        return len(errors) == 0, errors
