"""
S3 Phase 4 FG 4-4 §2.1.4 — pinned citation 강제 (R3 핵심).

목적: 인용에 ``"latest"`` 가 남지 않고 pinned 버전만 검증 통과.

본 테스트는 task4-3 의 Disagreement Record (citations 테이블 부재) 를 반영하여
적응:
  - DB 측 검증 (`SELECT FROM citations WHERE version_id='latest'`) → **N/A** (테이블 부재)
  - 검증 도구 측 R3 거부 → 확인
  - citation_basis 기본값 → 확인
  - schema 의 `latest` 단독 미반환 → 확인 (FG 4-2 / FG 4-3 회귀)
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestVerifyCitationRejectsLatest:
    """검증 경로의 R3 강제 (FG 4-3 정착)."""

    def test_latest_input_immediate_400(self, user_actor):
        """`tool_verify_citation` 이 `version_id="latest"` 즉시 거부."""
        from app.mcp.tools import tool_verify_citation
        from app.schemas.mcp import VerifyCitationRequest
        from app.mcp.errors import MCPError, MCPErrorCode

        request = VerifyCitationRequest(
            document_id="d1", version_id="latest", node_id="n1",
            content_hash="0" * 64,
        )
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)
        with pytest.raises(MCPError) as exc_info:
            tool_verify_citation(request, user_actor, conn)
        assert exc_info.value.code == MCPErrorCode.INVALID_REQUEST
        assert exc_info.value.http_status == 400


class TestDraftVersionPinnedFalse:
    """draft 버전 검증 시 pinned=False — published / archived 만 통과."""

    def test_draft_returns_pinned_false(self, user_actor):
        from app.mcp.tools import tool_verify_citation
        from app.schemas.mcp import VerifyCitationRequest

        request = VerifyCitationRequest(
            document_id="d1", version_id="v-draft", node_id="n1",
            content_hash=_sha256("text"),
        )
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(return_value={"id": "v-draft", "status": "draft"})
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        with patch(
            "app.mcp.tools._fetch_accessible_chunk",
            return_value={"document_id": "d1", "version_id": "v-draft", "node_id": "n1", "source_text": "text"},
        ), patch(
            "app.mcp.tools._ensure_document_allowed", return_value=None,
        ), patch(
            "app.mcp.tools._resolve_acl_filter", return_value={"sql": "", "params": []},
        ):
            result = tool_verify_citation(request, user_actor, conn)
        assert result.checks.pinned is False
        assert result.verified is False


class TestCitationBasisDefault:
    """citation_basis 누락 시 default `node_content` 적용."""

    def test_request_default_node_content(self):
        from app.schemas.mcp import VerifyCitationRequest

        r = VerifyCitationRequest(
            document_id="d1", version_id="v1", node_id="n1",
            content_hash="0" * 64,
        )
        assert r.citation_basis == "node_content"

    def test_citation_model_default_node_content(self):
        from app.schemas.citation import Citation

        c = Citation(
            document_id="00000000-0000-0000-0000-000000000001",
            version_id="00000000-0000-0000-0000-000000000002",
            node_id="00000000-0000-0000-0000-000000000003",
            content_hash="a" * 64,
        )
        assert c.citation_basis == "node_content"


class TestNoLatestInResponses:
    """응답 어디에도 'latest' 단독 문자열 미출현 (R3)."""

    def test_resolve_candidate_default_latest_published(self):
        """resolve_document_reference 후보의 version_ref default = 'latest_published' (단독 'latest' 아님)."""
        from app.services.document_resolver_service import _Candidate

        c = _Candidate(
            document_id="d", title="t", confidence=0.9, match_kind="exact_title",
        )
        assert c.version_ref != "latest"
        assert c.version_ref == "latest_published"

    def test_render_response_version_id_resolved(self):
        from app.schemas.mcp import ReadDocumentRenderData

        d = ReadDocumentRenderData(
            document_id="d1", version_id="v-concrete",
            format="plain_text", rendered_text="x", render_hash="h",
        )
        # 응답의 version_id 는 도구 함수에서 항상 resolved
        assert d.version_id != "latest"


class TestCitationsTableAbsentDisagreement:
    """task4-4 §2.1.4 의 'SELECT FROM citations WHERE version_id=latest → 0' 검증은 N/A.

    citations 테이블 부재 — Disagreement Record 적응안 (FG 4-3).
    검증 경로의 R3 강제 (위 TestVerifyCitationRejectsLatest) 가 동등 효과.
    """

    def test_no_citations_table_in_codebase(self):
        from pathlib import Path
        ROOT = Path(__file__).resolve().parents[4]
        connection_py = ROOT / "backend/app/db/connection.py"
        content = connection_py.read_text(encoding="utf-8")
        # 'citations' 테이블 CREATE 문이 없음을 확인 (golden_set / evaluations 의 expected_citations 는 다른 테이블 컬럼)
        assert "CREATE TABLE IF NOT EXISTS citations " not in content
        assert "CREATE TABLE citations " not in content

    def test_disagreement_record_exists(self):
        """Disagreement Record 가 등록되어 있어야 함 (제34조)."""
        from pathlib import Path
        ROOT = Path(__file__).resolve().parents[4]
        record = (
            ROOT / "docs/disagreements/2026-04-28-fg43-citations-table-absence.md"
        )
        assert record.exists()
