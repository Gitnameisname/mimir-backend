"""
Golden Set 도메인 모델 단위 테스트 — Phase 7 FG7.1

테스트 범위:
- Pydantic 도메인 모델 생성 및 검증
- GoldenItem expected_source_docs 최소 1개 제약
- GoldenSetCreateRequest / GoldenItemCreateRequest DTO
- Citation5Tuple 필드 구조 (Phase 2 호환)
- GoldenSetResponse 직렬화
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models.golden_set import (
    Citation5Tuple,
    GoldenItem,
    GoldenItemCreateRequest,
    GoldenItemUpdateRequest,
    GoldenSet,
    GoldenSetCreateRequest,
    GoldenSetDomain,
    GoldenSetResponse,
    GoldenSetStatus,
    GoldenSetUpdateRequest,
    SourceRef,
)

_NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# SourceRef
# ---------------------------------------------------------------------------


class TestSourceRef:
    def test_valid(self):
        ref = SourceRef(document_id="doc-1", version_id="v-1", node_id="n-1")
        assert ref.document_id == "doc-1"

    def test_missing_field_raises(self):
        with pytest.raises(ValidationError):
            SourceRef(document_id="doc-1")  # version_id, node_id 누락


# ---------------------------------------------------------------------------
# Citation5Tuple
# ---------------------------------------------------------------------------


class TestCitation5Tuple:
    def test_valid_with_offset(self):
        c = Citation5Tuple(
            document_id="doc-1",
            version_id="v-1",
            node_id="n-1",
            span_offset=10,
            content_hash="a" * 64,
        )
        assert c.span_offset == 10

    def test_valid_without_offset(self):
        c = Citation5Tuple(
            document_id="doc-1", version_id="v-1", node_id="n-1",
            span_offset=None, content_hash="abc",
        )
        assert c.span_offset is None

    def test_negative_offset_raises(self):
        with pytest.raises(ValidationError):
            Citation5Tuple(
                document_id="d", version_id="v", node_id="n",
                span_offset=-1, content_hash="x",
            )


# ---------------------------------------------------------------------------
# GoldenItem
# ---------------------------------------------------------------------------


def _make_item(**overrides) -> GoldenItem:
    defaults = dict(
        id="item-1",
        golden_set_id="set-1",
        version=1,
        question="What is Docker?",
        expected_answer="Docker is a container platform.",
        expected_source_docs=[SourceRef(document_id="d", version_id="v", node_id="n")],
        expected_citations=[],
        notes=None,
        created_at=_NOW,
        created_by="user-1",
        updated_at=_NOW,
        updated_by=None,
    )
    defaults.update(overrides)
    return GoldenItem(**defaults)


class TestGoldenItem:
    def test_create_valid(self):
        item = _make_item()
        assert item.id == "item-1"
        assert item.version == 1
        assert len(item.expected_source_docs) == 1

    def test_empty_source_docs_raises(self):
        with pytest.raises(ValidationError, match="at least one"):
            _make_item(expected_source_docs=[])

    def test_question_max_length(self):
        with pytest.raises(ValidationError):
            _make_item(question="x" * 2001)

    def test_expected_answer_max_length(self):
        with pytest.raises(ValidationError):
            _make_item(expected_answer="x" * 5001)

    def test_notes_max_length(self):
        with pytest.raises(ValidationError):
            _make_item(notes="n" * 1001)

    def test_with_citations(self):
        cit = Citation5Tuple(
            document_id="d", version_id="v", node_id="n",
            span_offset=0, content_hash="hash123",
        )
        item = _make_item(expected_citations=[cit])
        assert len(item.expected_citations) == 1
        assert item.expected_citations[0].span_offset == 0


# ---------------------------------------------------------------------------
# GoldenSet
# ---------------------------------------------------------------------------


def _make_set(**overrides) -> GoldenSet:
    defaults = dict(
        id="set-1",
        scope_id="scope-abc",
        name="Tech Docs",
        description="Technical documentation RAG",
        domain=GoldenSetDomain.TECHNICAL_GUIDE,
        status=GoldenSetStatus.DRAFT,
        version=1,
        items=[],
        extra_metadata={},
        created_at=_NOW,
        created_by="user-1",
        updated_at=_NOW,
        updated_by=None,
        deleted_at=None,
        is_deleted=False,
    )
    defaults.update(overrides)
    return GoldenSet(**defaults)


class TestGoldenSet:
    def test_create_valid(self):
        gs = _make_set()
        assert gs.scope_id == "scope-abc"
        assert gs.status == GoldenSetStatus.DRAFT
        assert not gs.is_deleted

    def test_soft_delete_fields(self):
        gs = _make_set()
        gs.is_deleted = True
        gs.deleted_at = _NOW
        assert gs.is_deleted
        assert gs.deleted_at == _NOW

    def test_name_max_length(self):
        with pytest.raises(ValidationError):
            _make_set(name="x" * 201)

    def test_domain_enum(self):
        gs = _make_set(domain=GoldenSetDomain.POLICY)
        assert gs.domain == GoldenSetDomain.POLICY

    def test_status_enum(self):
        gs = _make_set(status=GoldenSetStatus.PUBLISHED)
        assert gs.status == GoldenSetStatus.PUBLISHED

    def test_extra_metadata_default(self):
        gs = _make_set(extra_metadata={})
        assert gs.extra_metadata == {}


# ---------------------------------------------------------------------------
# Request DTOs
# ---------------------------------------------------------------------------


class TestGoldenSetCreateRequest:
    def test_defaults(self):
        req = GoldenSetCreateRequest(name="My Set")
        assert req.domain == GoldenSetDomain.CUSTOM
        assert req.extra_metadata == {}

    def test_full(self):
        req = GoldenSetCreateRequest(
            name="Policy Set",
            description="Policy docs",
            domain=GoldenSetDomain.POLICY,
            extra_metadata={"owner": "legal"},
        )
        assert req.domain == GoldenSetDomain.POLICY
        assert req.extra_metadata["owner"] == "legal"


class TestGoldenSetUpdateRequest:
    def test_partial(self):
        req = GoldenSetUpdateRequest(name="New Name")
        assert req.name == "New Name"
        assert req.domain is None
        assert req.status is None

    def test_status_update(self):
        req = GoldenSetUpdateRequest(status=GoldenSetStatus.PUBLISHED)
        assert req.status == GoldenSetStatus.PUBLISHED


class TestGoldenItemCreateRequest:
    def test_valid(self):
        req = GoldenItemCreateRequest(
            question="What is K8s?",
            expected_answer="Kubernetes is an orchestration platform.",
            expected_source_docs=[
                SourceRef(document_id="d", version_id="v", node_id="n")
            ],
        )
        assert req.question == "What is K8s?"
        assert len(req.expected_source_docs) == 1

    def test_empty_source_docs_raises(self):
        with pytest.raises(ValidationError):
            GoldenItemCreateRequest(
                question="Q",
                expected_answer="A",
                expected_source_docs=[],  # 최소 1개 필요
            )


class TestGoldenItemUpdateRequest:
    def test_all_none(self):
        req = GoldenItemUpdateRequest()
        assert req.question is None
        assert req.expected_answer is None

    def test_partial_update(self):
        req = GoldenItemUpdateRequest(question="Updated Q")
        assert req.question == "Updated Q"


# ---------------------------------------------------------------------------
# Response serialization
# ---------------------------------------------------------------------------


class TestGoldenSetResponse:
    def test_serialize(self):
        resp = GoldenSetResponse(
            id="set-1",
            scope_id="scope-1",
            name="Test",
            description=None,
            domain="custom",
            status="draft",
            version=1,
            item_count=3,
            extra_metadata={"tag": "v1"},
            created_at=_NOW,
            created_by="user-1",
            updated_at=_NOW,
            updated_by=None,
            is_deleted=False,
        )
        data = resp.model_dump()
        assert data["id"] == "set-1"
        assert data["item_count"] == 3
        assert data["extra_metadata"]["tag"] == "v1"
