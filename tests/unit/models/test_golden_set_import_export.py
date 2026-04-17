"""
Golden Set import/export 모델 단위 테스트 — Phase 7 FG7.1 (task7-3)

테스트 범위:
- GoldenSetImportItem 스키마 검증
- GoldenSetImportRequest 파싱 및 최대 항목 수 제한
- ImportValidator 중복 question 감지
- GoldenSetExportResponse 직렬화
- GoldenSetImportResult 통계 계산
- Round-trip JSON 왕복 일관성 (서비스 로직)
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.golden_set import Citation5Tuple, SourceRef
from app.models.golden_set_import_export import (
    GoldenSetExportResponse,
    GoldenSetImportItem,
    GoldenSetImportRequest,
    GoldenSetImportResult,
    ImportItemResult,
    ImportValidator,
)

_SRC = SourceRef(document_id="doc-1", version_id="v-1", node_id="n-1")
_CITE = Citation5Tuple(
    document_id="doc-1", version_id="v-1", node_id="n-1",
    span_offset=0, content_hash="abc123",
)


def _make_item(**kw) -> GoldenSetImportItem:
    defaults = dict(
        question="What is Docker?",
        expected_answer="A container platform.",
        expected_source_docs=[_SRC],
    )
    defaults.update(kw)
    return GoldenSetImportItem(**defaults)


# ---------------------------------------------------------------------------
# GoldenSetImportItem
# ---------------------------------------------------------------------------

class TestGoldenSetImportItem:
    def test_valid_minimal(self):
        item = _make_item()
        assert item.question == "What is Docker?"
        assert item.expected_citations == []

    def test_with_citation(self):
        item = _make_item(expected_citations=[_CITE])
        assert len(item.expected_citations) == 1

    def test_empty_source_docs_raises(self):
        with pytest.raises(ValidationError):
            _make_item(expected_source_docs=[])

    def test_question_too_long_raises(self):
        with pytest.raises(ValidationError):
            _make_item(question="x" * 2001)

    def test_answer_too_long_raises(self):
        with pytest.raises(ValidationError):
            _make_item(expected_answer="a" * 5001)

    def test_notes_too_long_raises(self):
        with pytest.raises(ValidationError):
            _make_item(notes="n" * 1001)

    def test_optional_notes(self):
        item = _make_item(notes=None)
        assert item.notes is None


# ---------------------------------------------------------------------------
# GoldenSetImportRequest
# ---------------------------------------------------------------------------

class TestGoldenSetImportRequest:
    def test_valid(self):
        req = GoldenSetImportRequest(
            items=[_make_item()],
            name="Test Set",
            domain="technical_guide",
        )
        assert req.format_version == "1.0"
        assert len(req.items) == 1

    def test_empty_items_raises(self):
        with pytest.raises(ValidationError):
            GoldenSetImportRequest(items=[])

    def test_default_format_version(self):
        req = GoldenSetImportRequest(items=[_make_item()])
        assert req.format_version == "1.0"

    def test_max_items_exceeded(self):
        items = [
            _make_item(question=f"Q{i}?")
            for i in range(5001)
        ]
        with pytest.raises(ValidationError, match="최대"):
            GoldenSetImportRequest(items=items)

    def test_optional_fields(self):
        req = GoldenSetImportRequest(items=[_make_item()])
        assert req.name is None
        assert req.description is None
        assert req.domain is None


# ---------------------------------------------------------------------------
# ImportValidator
# ---------------------------------------------------------------------------

class TestImportValidator:
    def test_valid_no_duplicates(self):
        req = GoldenSetImportRequest(items=[
            _make_item(question="Q1?"),
            _make_item(question="Q2?"),
        ])
        ok, errors = ImportValidator.validate(req)
        assert ok
        assert errors == []

    def test_duplicate_question_detected(self):
        req = GoldenSetImportRequest(items=[
            _make_item(question="Same Q?"),
            _make_item(question="Same Q?"),
        ])
        ok, errors = ImportValidator.validate(req)
        assert not ok
        assert any("중복" in e for e in errors)

    def test_empty_content_hash_rejected_by_schema(self):
        """content_hash="" 는 Pydantic min_length=1 에 의해 스키마 단에서 거부된다."""
        with pytest.raises(ValidationError):
            Citation5Tuple(
                document_id="d", version_id="v", node_id="n",
                span_offset=None, content_hash="",
            )

    def test_multiple_items_single_duplicate(self):
        req = GoldenSetImportRequest(items=[
            _make_item(question="A?"),
            _make_item(question="B?"),
            _make_item(question="A?"),  # 중복
            _make_item(question="C?"),
        ])
        ok, errors = ImportValidator.validate(req)
        assert not ok
        assert len(errors) == 1  # 중복 1건


# ---------------------------------------------------------------------------
# GoldenSetExportResponse
# ---------------------------------------------------------------------------

class TestGoldenSetExportResponse:
    def test_serialize(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        resp = GoldenSetExportResponse(
            id="set-1",
            scope_id="scope-1",
            name="Test",
            description=None,
            domain="custom",
            status="draft",
            golden_set_version=3,
            created_at=now,
            created_by="user-1",
            updated_at=now,
            updated_by=None,
            items=[_make_item()],
        )
        data = resp.model_dump()
        assert data["format_version"] == "1.0"
        assert data["golden_set_version"] == 3
        assert len(data["items"]) == 1

    def test_round_trip_reimport(self):
        """export 결과를 GoldenSetImportRequest로 재파싱 가능해야 한다."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        items = [
            _make_item(question="Q1?"),
            _make_item(question="Q2?", notes="note2"),
        ]
        resp = GoldenSetExportResponse(
            id="s", scope_id="sc", name="N", description="D",
            domain="custom", status="draft", golden_set_version=1,
            created_at=now, created_by="u", updated_at=now, updated_by=None,
            items=items,
        )
        # import 스키마로 재파싱
        reimport = GoldenSetImportRequest(
            format_version=resp.format_version,
            name=resp.name,
            description=resp.description,
            domain=resp.domain,
            items=resp.items,
        )
        assert len(reimport.items) == 2
        assert reimport.items[0].question == "Q1?"
        assert reimport.items[1].notes == "note2"


# ---------------------------------------------------------------------------
# GoldenSetImportResult
# ---------------------------------------------------------------------------

class TestGoldenSetImportResult:
    def test_success_rate_all_success(self):
        result = GoldenSetImportResult(
            total_items=5, successful_items=5, failed_items=0,
            created_item_ids=["a", "b", "c", "d", "e"],
        )
        assert result.success_rate == 1.0

    def test_success_rate_partial(self):
        result = GoldenSetImportResult(
            total_items=4, successful_items=3, failed_items=1,
            created_item_ids=["a", "b", "c"],
            errors=[ImportItemResult(index=3, question="Q?", success=False, error="DB error")],
        )
        assert result.success_rate == pytest.approx(0.75)

    def test_success_rate_zero_items(self):
        result = GoldenSetImportResult(
            total_items=0, successful_items=0, failed_items=0,
            created_item_ids=[],
        )
        assert result.success_rate == 1.0

    def test_serialize(self):
        result = GoldenSetImportResult(
            total_items=2, successful_items=1, failed_items=1,
            created_item_ids=["item-1"],
            errors=[ImportItemResult(index=1, question="Q?", success=False, error="fail")],
        )
        data = result.model_dump()
        assert data["total_items"] == 2
        assert len(data["errors"]) == 1
        assert data["errors"][0]["index"] == 1
