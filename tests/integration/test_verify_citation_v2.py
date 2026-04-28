"""
S3 Phase 4 FG 4-3 회귀 테스트 — verify_citation 5중 검증.

성공 기준 (task4-3 §7 + Disagreement Record 적응):
  - 5중 검증 모두 수행 + checks 출력 가시성
  - R3 강제: 'latest' 즉시 거부 (도구 진입점)
  - citation_basis 분기 (node_content / rendered_text)
  - envelope (FG 4-1 표준) 통과
  - pytest 신규 ≥ 25
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "verify-citation-v2-test")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal-secret")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 헬퍼 — actor / conn / chunk
# ---------------------------------------------------------------------------


def _user_actor():
    """user actor — _check_tool_allowed 비대상 (사람은 별 게이트)."""
    from app.api.auth.models import ActorContext, ActorType, AuthMethod

    return ActorContext(
        actor_type=ActorType.USER,
        actor_id="u1",
        is_authenticated=True,
        auth_method=AuthMethod.SESSION,
        tenant_id=None,
        role="VIEWER",
    )


def _make_conn(version_status: str = "published"):
    """version exists + status 를 mock 으로 반환하는 conn."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(
        return_value={"id": "version-1", "status": version_status}
    )
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn


def _patches_for_chunk(source_text: str):
    """_fetch_accessible_chunk + _ensure_document_allowed 를 mock."""
    return [
        patch(
            "app.mcp.tools._fetch_accessible_chunk",
            return_value={
                "document_id": "d1",
                "version_id": "v1",
                "node_id": "n1",
                "source_text": source_text,
            },
        ),
        patch("app.mcp.tools._ensure_document_allowed", return_value=None),
        patch("app.mcp.tools._resolve_acl_filter", return_value={"sql": "", "params": []}),
    ]


def _make_request(**kwargs):
    from app.schemas.mcp import VerifyCitationRequest

    defaults = {
        "document_id": "d1",
        "version_id": "v1",
        "node_id": "n1",
        "content_hash": "0" * 64,
    }
    defaults.update(kwargs)
    return VerifyCitationRequest(**defaults)


# ===========================================================================
# 검사 1: 존재성
# ===========================================================================


class TestCheck1Exists:
    def test_version_not_found_returns_false(self):
        from app.mcp.tools import tool_verify_citation

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(return_value=None)  # version 없음
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        request = _make_request()
        with patch("app.mcp.tools._ensure_document_allowed"), patch(
            "app.mcp.tools._resolve_acl_filter", return_value={"sql": "", "params": []}
        ):
            result = tool_verify_citation(request, _user_actor(), conn)
        assert result.verified is False
        assert result.checks.exists is False
        assert "버전이 유효하지" in result.message

    def test_node_not_found_returns_false(self):
        from app.mcp.tools import tool_verify_citation

        request = _make_request()
        conn = _make_conn(version_status="published")
        with patch("app.mcp.tools._fetch_accessible_chunk", return_value=None), patch(
            "app.mcp.tools._ensure_document_allowed"
        ), patch("app.mcp.tools._resolve_acl_filter", return_value={"sql": "", "params": []}):
            result = tool_verify_citation(request, _user_actor(), conn)
        assert result.verified is False
        assert result.checks.exists is False
        assert "노드를 찾을 수 없습니다" in result.message


# ===========================================================================
# 검사 2: pinned (R3 — 'latest' 거부 + draft 거부)
# ===========================================================================


class TestCheck2Pinned:
    def test_latest_input_immediately_rejected(self):
        """R3: version_id='latest' 즉시 MCPError 거부."""
        from app.mcp.tools import tool_verify_citation
        from app.mcp.errors import MCPError, MCPErrorCode

        request = _make_request(version_id="latest")
        with pytest.raises(MCPError) as exc_info:
            tool_verify_citation(request, _user_actor(), _make_conn())
        assert exc_info.value.code == MCPErrorCode.INVALID_REQUEST
        assert exc_info.value.http_status == 400
        assert "pinned" in exc_info.value.message or "latest" in exc_info.value.message.lower()

    def test_draft_version_pinned_false(self):
        from app.mcp.tools import tool_verify_citation

        request = _make_request()
        conn = _make_conn(version_status="draft")
        patches = _patches_for_chunk("hello world")
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), conn)
        finally:
            for p in patches:
                p.stop()
        assert result.verified is False
        assert result.checks.pinned is False
        assert "published" in result.message or "draft" in result.message

    def test_published_version_pinned_true(self):
        from app.mcp.tools import tool_verify_citation

        request = _make_request(content_hash=_sha256("hello"))
        conn = _make_conn(version_status="published")
        patches = _patches_for_chunk("hello")
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), conn)
        finally:
            for p in patches:
                p.stop()
        assert result.checks.pinned is True
        assert result.verified is True

    def test_archived_version_pinned_true(self):
        """archived 도 pinned 통과 (시간 후 검증 시점에 archived 가능)."""
        from app.mcp.tools import tool_verify_citation

        request = _make_request(content_hash=_sha256("text"))
        conn = _make_conn(version_status="archived")
        patches = _patches_for_chunk("text")
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), conn)
        finally:
            for p in patches:
                p.stop()
        assert result.checks.pinned is True


# ===========================================================================
# 검사 3: hash_matches (citation_basis 분기)
# ===========================================================================


class TestCheck3HashMatches:
    def test_node_content_hash_match(self):
        from app.mcp.tools import tool_verify_citation

        text = "node body content"
        request = _make_request(content_hash=_sha256(text))
        patches = _patches_for_chunk(text)
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), _make_conn())
        finally:
            for p in patches:
                p.stop()
        assert result.checks.hash_matches is True
        assert result.current_hash == _sha256(text)

    def test_node_content_hash_mismatch(self):
        from app.mcp.tools import tool_verify_citation

        request = _make_request(content_hash=_sha256("WRONG"))
        patches = _patches_for_chunk("actual content")
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), _make_conn())
        finally:
            for p in patches:
                p.stop()
        assert result.checks.hash_matches is False
        assert result.verified is False

    def test_default_citation_basis_is_node_content(self):
        """citation_basis 미입력 시 default node_content 적용."""
        from app.schemas.mcp import VerifyCitationRequest

        r = VerifyCitationRequest(
            document_id="d", version_id="v", node_id="n", content_hash="0" * 64
        )
        assert r.citation_basis == "node_content"

    def test_rendered_text_basis_uses_render_service(self):
        from app.mcp.tools import tool_verify_citation

        rendered_text = "rendered body"
        request = _make_request(
            citation_basis="rendered_text",
            content_hash=_sha256(rendered_text),
        )

        # render_service 가 RenderDocument 를 반환하도록 mock — _walk_blocks_for_text 이 plain_text rstrip 함
        fake_block = type(
            "B", (),
            {"block_type": "paragraph", "block_id": "n1", "content": rendered_text},
        )()
        fake_doc = MagicMock()
        fake_doc.blocks = [fake_block]
        fake_version = MagicMock()
        fake_version.id = "v1"

        patches = _patches_for_chunk("dummy node text")
        with patch(
            "app.repositories.versions_repository.VersionsRepository.get_by_document_and_version_id",
            return_value=fake_version,
        ), patch(
            "app.services.render_service.render_service.render_version",
            return_value=fake_doc,
        ):
            for p in patches:
                p.start()
            try:
                result = tool_verify_citation(request, _user_actor(), _make_conn())
            finally:
                for p in patches:
                    p.stop()
        assert result.checks.hash_matches is True
        # rendered_text 모드에서는 node_snapshot 이 채워지지 않음
        assert result.rendered_snapshot is None or result.node_snapshot is None

    def test_rendered_text_basis_mismatch_when_render_changes(self):
        from app.mcp.tools import tool_verify_citation

        # 클라이언트가 저장한 hash 는 옛 렌더 결과의 SHA-256
        old_hash = _sha256("OLD rendered text")
        request = _make_request(
            citation_basis="rendered_text", content_hash=old_hash,
        )
        # 현재 render 결과는 다른 텍스트
        fake_block = type(
            "B", (),
            {"block_type": "paragraph", "block_id": "n1", "content": "NEW rendered text"},
        )()
        fake_doc = MagicMock()
        fake_doc.blocks = [fake_block]
        fake_version = MagicMock()
        fake_version.id = "v1"

        patches = _patches_for_chunk("ignored")
        with patch(
            "app.repositories.versions_repository.VersionsRepository.get_by_document_and_version_id",
            return_value=fake_version,
        ), patch(
            "app.services.render_service.render_service.render_version",
            return_value=fake_doc,
        ):
            for p in patches:
                p.start()
            try:
                result = tool_verify_citation(request, _user_actor(), _make_conn())
            finally:
                for p in patches:
                    p.stop()
        assert result.checks.hash_matches is False
        assert result.verified is False


# ===========================================================================
# 검사 4: quoted_text_in_content
# ===========================================================================


class TestCheck4QuotedText:
    def test_quoted_text_present(self):
        from app.mcp.tools import tool_verify_citation

        text = "the quick brown fox jumps over the lazy dog"
        request = _make_request(
            content_hash=_sha256(text), quoted_text="brown fox"
        )
        patches = _patches_for_chunk(text)
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), _make_conn())
        finally:
            for p in patches:
                p.stop()
        assert result.checks.quoted_text_in_content is True
        assert result.verified is True

    def test_quoted_text_missing(self):
        from app.mcp.tools import tool_verify_citation

        text = "the quick brown fox"
        request = _make_request(
            content_hash=_sha256(text), quoted_text="purple elephant",
        )
        patches = _patches_for_chunk(text)
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), _make_conn())
        finally:
            for p in patches:
                p.stop()
        assert result.checks.quoted_text_in_content is False
        assert result.verified is False
        assert "포함" in result.message

    def test_quoted_text_omitted_returns_none(self):
        from app.mcp.tools import tool_verify_citation

        text = "any text"
        request = _make_request(content_hash=_sha256(text))  # quoted_text 미입력
        patches = _patches_for_chunk(text)
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), _make_conn())
        finally:
            for p in patches:
                p.stop()
        assert result.checks.quoted_text_in_content is None
        assert result.verified is True  # 미입력 시 통과 처리


# ===========================================================================
# 검사 5: span_valid
# ===========================================================================


class TestCheck5SpanValid:
    def test_valid_span(self):
        from app.mcp.tools import tool_verify_citation

        text = "0123456789"
        request = _make_request(
            content_hash=_sha256(text), span_offset=2, span_length=5,
        )
        patches = _patches_for_chunk(text)
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), _make_conn())
        finally:
            for p in patches:
                p.stop()
        assert result.checks.span_valid is True

    def test_negative_offset(self):
        from app.mcp.tools import tool_verify_citation

        text = "0123456789"
        request = _make_request(
            content_hash=_sha256(text), span_offset=-1, span_length=3,
        )
        patches = _patches_for_chunk(text)
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), _make_conn())
        finally:
            for p in patches:
                p.stop()
        # Pydantic 의 ge=0 제약이 있는지 — VerifyCitationRequest 의 span_offset 은 제약 없음
        # (Citation 모델의 span_offset 만 ge=0). 본 요청은 통과되고, 본 검사 5 가 거부.
        # 음수 offset 은 자체 검사에서 False
        assert result.checks.span_valid is False

    def test_overflow_length(self):
        from app.mcp.tools import tool_verify_citation

        text = "0123456789"
        request = _make_request(
            content_hash=_sha256(text), span_offset=5, span_length=999,
        )
        patches = _patches_for_chunk(text)
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), _make_conn())
        finally:
            for p in patches:
                p.stop()
        assert result.checks.span_valid is False
        assert "범위" in result.message or "벗어" in result.message

    def test_span_omitted_returns_none(self):
        from app.mcp.tools import tool_verify_citation

        text = "abc"
        request = _make_request(content_hash=_sha256(text))  # span 미입력
        patches = _patches_for_chunk(text)
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), _make_conn())
        finally:
            for p in patches:
                p.stop()
        assert result.checks.span_valid is None
        assert result.verified is True

    def test_zero_length_span_invalid(self):
        from app.mcp.tools import tool_verify_citation

        text = "abc"
        request = _make_request(
            content_hash=_sha256(text), span_offset=0, span_length=0,
        )
        patches = _patches_for_chunk(text)
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), _make_conn())
        finally:
            for p in patches:
                p.stop()
        assert result.checks.span_valid is False


# ===========================================================================
# 5중 통합: 모두 통과 + 일부 실패
# ===========================================================================


class TestFullVerification:
    def test_all_five_checks_pass(self):
        from app.mcp.tools import tool_verify_citation

        text = "Mimir is a knowledge platform"
        request = _make_request(
            content_hash=_sha256(text),
            quoted_text="knowledge platform",
            span_offset=9,
            span_length=20,
        )
        patches = _patches_for_chunk(text)
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), _make_conn())
        finally:
            for p in patches:
                p.stop()
        assert result.verified is True
        assert result.checks.exists is True
        assert result.checks.pinned is True
        assert result.checks.hash_matches is True
        assert result.checks.quoted_text_in_content is True
        assert result.checks.span_valid is True
        assert result.message == "검증 성공."

    def test_partial_failure_hash_propagates(self):
        """hash 실패 시 이후 검사 결과와 무관하게 verified=False."""
        from app.mcp.tools import tool_verify_citation

        text = "real text"
        request = _make_request(
            content_hash=_sha256("WRONG"), quoted_text="real",
            span_offset=0, span_length=4,
        )
        patches = _patches_for_chunk(text)
        for p in patches:
            p.start()
        try:
            result = tool_verify_citation(request, _user_actor(), _make_conn())
        finally:
            for p in patches:
                p.stop()
        assert result.verified is False
        # quoted_text / span 자체는 통과해도 hash 실패라 종합 False
        assert result.checks.hash_matches is False
        assert result.checks.quoted_text_in_content is True
        assert result.checks.span_valid is True


# ===========================================================================
# Schema 검증
# ===========================================================================


class TestSchemaContracts:
    def test_citation_basis_default_node_content(self):
        from app.schemas.citation import Citation

        c = Citation(
            document_id="00000000-0000-0000-0000-000000000001",
            version_id="00000000-0000-0000-0000-000000000002",
            node_id="00000000-0000-0000-0000-000000000003",
            content_hash="a" * 64,
        )
        assert c.citation_basis == "node_content"

    def test_citation_basis_literal_validation(self):
        from app.schemas.citation import Citation
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Citation(
                document_id="00000000-0000-0000-0000-000000000001",
                version_id="00000000-0000-0000-0000-000000000002",
                node_id="00000000-0000-0000-0000-000000000003",
                content_hash="a" * 64,
                citation_basis="bogus",
            )

    def test_verify_request_citation_basis_literal(self):
        from app.schemas.mcp import VerifyCitationRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            VerifyCitationRequest(
                document_id="d", version_id="v", node_id="n",
                content_hash="0" * 64, citation_basis="invalid",
            )

    def test_verify_data_checks_required(self):
        """VerifyCitationData 의 checks 필드는 필수 — 빠뜨리면 ValidationError."""
        from app.schemas.mcp import VerifyCitationData
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            VerifyCitationData(verified=True, message="ok")  # checks 누락

    def test_verify_checks_optional_fields(self):
        from app.schemas.mcp import VerifyCitationChecks

        c = VerifyCitationChecks(exists=True, pinned=True, hash_matches=True)
        assert c.quoted_text_in_content is None
        assert c.span_valid is None


# ===========================================================================
# envelope (FG 4-1) 통합 — verify_citation 의 envelope 매핑
# ===========================================================================


class TestEnvelopeMapping:
    def test_envelope_is_tool_metadata(self):
        from app.api.v1.mcp_router import _build_envelope

        raw = {
            "verified": True,
            "checks": {"exists": True, "pinned": True, "hash_matches": True},
            "current_hash": "h",
            "document_id": "d1",
            "version_id": "v1",
            "node_id": "n1",
        }
        env = _build_envelope("verify_citation", raw)
        assert env.content_role == "tool_metadata"
        assert env.instruction_authority == "none"
        assert env.detected_risks == []
        # source 가 node URI 로 채워짐
        assert env.source is not None
        assert env.source.uri == "mimir://documents/d1/versions/v1/nodes/n1"
