"""
S3 Phase 4 FG 4-6 회귀 테스트 — L2 draft 쓰기 도구 (save_draft).

성공 기준 (task4-6 §7):
  - 4 사전 조건 (idempotency / human approval / impact preview / 감사 4종)
  - propose 만 — 자동 머지 0
  - L3/L4 영구 제외 회귀 강화
  - requires_human_approval=True 항상
  - default_enabled=False + ScopeProfile 명시 등록 의무
  - pytest 신규 ≥ 30
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "fg46-save-draft-test")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal-secret")


# ===========================================================================
# manifest + dispatcher 등재
# ===========================================================================


class TestSaveDraftManifest:
    def test_save_draft_in_tool_schemas(self):
        from app.schemas.mcp import TOOL_SCHEMAS

        names = {s["name"] for s in TOOL_SCHEMAS}
        assert "save_draft" in names

    def test_save_draft_is_l2(self):
        from app.schemas.mcp import TOOL_SCHEMAS

        sd = next(s for s in TOOL_SCHEMAS if s["name"] == "save_draft")
        assert sd["risk_tier"] == "L2"
        assert sd["maturity"] == "experimental"
        assert sd["status"] == "enabled"

    def test_save_draft_default_disabled(self):
        """write 도구는 default_enabled=False — 운영자 명시 등록."""
        from app.schemas.mcp import TOOL_SCHEMAS

        sd = next(s for s in TOOL_SCHEMAS if s["name"] == "save_draft")
        assert sd["default_enabled"] is False

    def test_save_draft_policy_profile_write_audited(self):
        from app.schemas.mcp import TOOL_SCHEMAS

        sd = next(s for s in TOOL_SCHEMAS if s["name"] == "save_draft")
        assert sd["policy_profile"] == "write_audited"

    def test_save_draft_in_curated_tools(self):
        from app.api.v1.mcp_router import _CURATED_TOOLS

        assert "save_draft" in _CURATED_TOOLS

    def test_save_draft_default_enabled_excluded_from_use_defaults(self):
        """`use_defaults=True` ScopeProfile 자동 등록에서 save_draft 는 제외."""
        from app.schemas.mcp import default_enabled_tool_names

        assert "save_draft" not in default_enabled_tool_names()


# ===========================================================================
# Schema (입력/출력 + write_envelope)
# ===========================================================================


class TestSchemas:
    def test_save_draft_request_idempotency_key_required(self):
        from app.schemas.mcp import SaveDraftRequest
        from pydantic import ValidationError

        # idempotency_key 누락 → 422
        with pytest.raises(ValidationError):
            SaveDraftRequest(content_snapshot={"type": "doc"})

    def test_save_draft_request_idempotency_key_length_bounds(self):
        from app.schemas.mcp import SaveDraftRequest
        from pydantic import ValidationError

        # 빈 문자열 거부
        with pytest.raises(ValidationError):
            SaveDraftRequest(content_snapshot={"type": "doc"}, idempotency_key="")
        # 128 초과 거부
        with pytest.raises(ValidationError):
            SaveDraftRequest(
                content_snapshot={"type": "doc"},
                idempotency_key="x" * 129,
            )
        # 경계 OK
        SaveDraftRequest(content_snapshot={"type": "doc"}, idempotency_key="x")
        SaveDraftRequest(content_snapshot={"type": "doc"}, idempotency_key="x" * 128)

    def test_write_envelope_r4_instruction_authority_none(self):
        """R4: write_envelope 의 instruction_authority 도 'none' 만 허용."""
        from app.schemas.mcp import MCPWriteEnvelope
        from pydantic import ValidationError

        e = MCPWriteEnvelope()
        assert e.instruction_authority == "none"
        with pytest.raises(ValidationError):
            MCPWriteEnvelope(instruction_authority="system")

    def test_write_envelope_content_role_literal(self):
        from app.schemas.mcp import MCPWriteEnvelope
        from pydantic import ValidationError

        # mutation_proposed 만 허용
        e = MCPWriteEnvelope(content_role="mutation_proposed")
        assert e.content_role == "mutation_proposed"
        with pytest.raises(ValidationError):
            MCPWriteEnvelope(content_role="retrieved_evidence")

    def test_write_envelope_default_requires_human_approval_true(self):
        from app.schemas.mcp import MCPWriteEnvelope

        e = MCPWriteEnvelope()
        # FG 4-6 핵심 — default True
        assert e.requires_human_approval is True

    def test_response_has_separate_envelope_fields(self):
        """MCPResponse 에 envelope (read) + write_envelope (write) 분리."""
        from app.schemas.mcp import MCPResponse, MCPWriteEnvelope, MCPReadEnvelope

        r1 = MCPResponse(success=True, envelope=MCPReadEnvelope())
        assert r1.envelope is not None
        assert r1.write_envelope is None

        r2 = MCPResponse(success=True, write_envelope=MCPWriteEnvelope())
        assert r2.envelope is None
        assert r2.write_envelope is not None

    def test_save_draft_data_required_fields(self):
        from app.schemas.mcp import DraftImpactPreview, SaveDraftData

        d = SaveDraftData(
            proposal_id="p1",
            status="proposed",
            document_id="d1",
            version_id="v1",
            impact=DraftImpactPreview(),
            message="ok",
        )
        # 기본값
        assert d.requires_human_approval is True
        assert d.audit_event == "agent_proposal.requested"


# ===========================================================================
# compute_draft_impact (Phase 4 FG 4-6 §2.1.3)
# ===========================================================================


class TestComputeDraftImpact:
    def _conn_with_no_existing_draft(self):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(return_value=None)
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)
        return conn

    def _conn_with_existing_draft(self, *, draft_text: str):
        import json

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        snapshot = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": draft_text}],
                }
            ],
        }
        cur.fetchone = MagicMock(
            return_value={
                "id": "v-existing",
                "content_snapshot": json.dumps(snapshot),
            }
        )
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)
        return conn

    def test_new_document_impact(self):
        from app.services.agent_proposal_service import agent_proposal_service

        conn = self._conn_with_no_existing_draft()
        snapshot = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Hello"}],
                }
            ],
        }
        result = agent_proposal_service.compute_draft_impact(
            conn, document_id=None, content_snapshot=snapshot
        )
        assert result["target_version_id"] is None
        assert result["overwrites_existing_draft"] is False
        assert result["chars_added"] == 5
        assert result["chars_removed"] == 0
        assert "신규 문서" in result["summary"]

    def test_overwrites_existing_draft(self):
        from app.services.agent_proposal_service import agent_proposal_service

        conn = self._conn_with_existing_draft(draft_text="OLD content")
        new_snapshot = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "NEW content here"}],
                }
            ],
        }
        result = agent_proposal_service.compute_draft_impact(
            conn, document_id="d1", content_snapshot=new_snapshot
        )
        assert result["overwrites_existing_draft"] is True
        assert result["target_version_id"] == "v-existing"
        # 기존 11자 → 새 16자: chars_added = 5
        assert result["chars_added"] == 5
        assert result["chars_removed"] == 0

    def test_chars_removed_when_shorter(self):
        from app.services.agent_proposal_service import agent_proposal_service

        conn = self._conn_with_existing_draft(draft_text="long original text body")
        new_snapshot = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "short"}]},
            ],
        }
        result = agent_proposal_service.compute_draft_impact(
            conn, document_id="d1", content_snapshot=new_snapshot
        )
        # 23 → 5: removed = 18
        assert result["chars_removed"] == 18
        assert result["chars_added"] == 0


# ===========================================================================
# tool_save_draft 동작
# ===========================================================================


def _agent_actor():
    from app.api.auth.models import ActorContext, ActorType, AuthMethod

    return ActorContext(
        actor_type=ActorType.AGENT,
        actor_id="agent-1",
        is_authenticated=True,
        auth_method=AuthMethod.API_KEY,
        tenant_id=None,
        agent_id="agent-1",
        scope_profile_id="sp-1",
    )


def _make_request(**overrides):
    from app.schemas.mcp import SaveDraftRequest

    defaults = {
        "document_id": "d1",
        "content_snapshot": {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Hello"}],
                }
            ],
        },
        "metadata": {},
        "reason": "test",
        "idempotency_key": "key-1",
    }
    defaults.update(overrides)
    return SaveDraftRequest(**defaults)


class TestToolSaveDraft:
    def test_returns_proposed_status_with_human_approval_required(self, monkeypatch):
        from app.mcp.tools import tool_save_draft

        actor = _agent_actor()

        # ACL 게이트 우회 (사람이 아니라 agent — _check_tool_allowed 가 ScopeProfile lookup)
        from app.api.auth.models import ActorContext
        monkeypatch.setattr(
            ActorContext, "can_call_tool",
            lambda self, tool_name, conn=None: True,
        )

        with patch("app.mcp.tools._ensure_document_allowed", return_value=None), patch(
            "app.mcp.tools._resolve_acl_filter",
            return_value={"sql": "", "params": []},
        ), patch(
            "app.services.agent_proposal_service.agent_proposal_service.compute_draft_impact",
            return_value={
                "document_id": "d1",
                "target_version_id": None,
                "overwrites_existing_draft": False,
                "nodes_added": 1,
                "nodes_modified": 0,
                "nodes_deleted": 0,
                "chars_added": 5,
                "chars_removed": 0,
                "summary": "신규 draft",
            },
        ), patch(
            "app.services.agent_proposal_service.agent_proposal_service.propose_draft",
            return_value={
                "draft_id": "v-new",
                "status": "proposed",
                "created_by_agent": True,
                "created_at": None,
                "document_id": "d1",
                "version_id": "v-new",
                "proposal_url": "/documents/d1/versions/v-new",
                "mcp_task_id": "task-1",
            },
        ):
            result = tool_save_draft(_make_request(), actor, MagicMock())

        # FG 4-6 핵심: requires_human_approval=True 항상
        assert result.requires_human_approval is True
        assert result.audit_event == "agent_proposal.requested"
        # propose 만 — 자동 merged 아님
        assert result.status == "proposed"

    def test_idempotent_replay_message(self, monkeypatch):
        """같은 idempotency_key 재호출 → idempotent_replay=True 메시지."""
        from app.mcp.tools import tool_save_draft
        from app.api.auth.models import ActorContext

        actor = _agent_actor()
        monkeypatch.setattr(
            ActorContext, "can_call_tool",
            lambda self, tool_name, conn=None: True,
        )

        with patch("app.mcp.tools._ensure_document_allowed", return_value=None), patch(
            "app.mcp.tools._resolve_acl_filter",
            return_value={"sql": "", "params": []},
        ), patch(
            "app.services.agent_proposal_service.agent_proposal_service.compute_draft_impact",
            return_value={"document_id": "d1", "summary": "x"},
        ), patch(
            "app.services.agent_proposal_service.agent_proposal_service.propose_draft",
            return_value={
                "draft_id": "v-existing",
                "status": "proposed",
                "document_id": "d1",
                "version_id": "v-existing",
                "idempotent_replay": True,  # 핵심 플래그
            },
        ):
            result = tool_save_draft(_make_request(), actor, MagicMock())
        assert "idempotent" in result.message.lower() or "replay" in result.message.lower() or "동일" in result.message

    def test_acl_denied_when_tool_not_allowed(self, monkeypatch):
        """`_check_tool_allowed` 가 거부 → 403."""
        from app.mcp.tools import tool_save_draft
        from app.api.auth.models import ActorContext
        from app.mcp.errors import MCPError, MCPErrorCode

        actor = _agent_actor()
        # ScopeProfile.allowed_tools 에 save_draft 없음
        monkeypatch.setattr(
            ActorContext, "can_call_tool",
            lambda self, tool_name, conn=None: False,
        )
        with pytest.raises(MCPError) as exc_info:
            tool_save_draft(_make_request(), actor, MagicMock())
        assert exc_info.value.code == MCPErrorCode.UNAUTHORIZED

    def test_no_agent_id_rejected(self, monkeypatch):
        """agent_id 없는 actor → 거부."""
        from app.mcp.tools import tool_save_draft
        from app.api.auth.models import ActorContext, ActorType, AuthMethod
        from app.mcp.errors import MCPError, MCPErrorCode

        # actor_type=AGENT 지만 agent_id None — _check_tool_allowed 통과 후 거부
        actor = ActorContext(
            actor_type=ActorType.USER,  # 사람 actor (agent_id None)
            actor_id=None,
            is_authenticated=True,
            auth_method=AuthMethod.SESSION,
            tenant_id=None,
        )
        monkeypatch.setattr(
            ActorContext, "can_call_tool",
            lambda self, tool_name, conn=None: True,
        )
        with pytest.raises(MCPError) as exc_info:
            tool_save_draft(_make_request(), actor, MagicMock())
        assert exc_info.value.code == MCPErrorCode.UNAUTHORIZED
        assert exc_info.value.http_status == 403


# ===========================================================================
# write_envelope (FG 4-6 §2.1.6)
# ===========================================================================


class TestWriteEnvelope:
    def test_build_write_envelope_for_save_draft(self):
        from app.api.v1.mcp_router import _build_write_envelope

        raw = {
            "proposal_id": "p1",
            "impact": {
                "document_id": "d1",
                "nodes_added": 1,
                "nodes_modified": 0,
                "nodes_deleted": 0,
                "chars_added": 5,
                "chars_removed": 0,
                "overwrites_existing_draft": False,
                "summary": "신규",
            },
            "requires_human_approval": True,
            "audit_event": "agent_proposal.requested",
        }
        env = _build_write_envelope("save_draft", raw)
        assert env.content_role == "mutation_proposed"
        assert env.instruction_authority == "none"
        assert env.requires_human_approval is True
        assert env.proposal_id == "p1"
        assert env.audit_chain == ["agent_proposal.requested"]
        assert env.impact is not None
        assert env.impact.chars_added == 5

    def test_unknown_write_tool_safe_default(self):
        from app.api.v1.mcp_router import _build_write_envelope

        env = _build_write_envelope("__unknown_write_tool__", {})
        assert env.requires_human_approval is True
        assert env.instruction_authority == "none"


# ===========================================================================
# L3/L4 영구 제외 (FG 4-4 contract drift 회귀 강화)
# ===========================================================================


class TestL3L4PermanentExclusion:
    def test_no_l2_other_than_save_draft_yet(self):
        """현 시점 L2 도구는 save_draft 단일 — 다른 L2 가 들어오면 별 검토."""
        from app.schemas.mcp import TOOL_SCHEMAS

        l2 = [s for s in TOOL_SCHEMAS if s.get("risk_tier") == "L2"]
        l2_names = {s["name"] for s in l2}
        assert l2_names == {"save_draft"}, (
            f"L2 도구가 save_draft 외 등록됨 — 검토 필요: {l2_names}"
        )

    def test_no_l3_tools(self):
        """R1 강화: L3 도구 절대 등재 0."""
        from app.schemas.mcp import TOOL_SCHEMAS

        l3 = [s for s in TOOL_SCHEMAS if s.get("risk_tier") == "L3"]
        assert l3 == [], f"L3 도구 등재 — Phase 4 영구 제외 위반: {[s['name'] for s in l3]}"

    def test_no_l4_tools(self):
        """R1 강화: L4 도구 절대 등재 0."""
        from app.schemas.mcp import TOOL_SCHEMAS

        l4 = [s for s in TOOL_SCHEMAS if s.get("risk_tier") == "L4"]
        assert l4 == [], f"L4 도구 등재 — R1 위반: {[s['name'] for s in l4]}"

    def test_is_tool_mcp_exposed_blocks_l3(self):
        """가짜 L3 도구 — 차단."""
        from app.schemas.mcp import is_tool_mcp_exposed

        fake_l3 = {
            "name": "fake_publish",
            "risk_tier": "L3",
            "maturity": "stable",
            "status": "not_exposed",
            "exposure_policy": "REST_ADMIN_ONLY",
        }
        # 현재 is_tool_mcp_exposed 는 status=not_exposed 만 검사 — L3 자체는 통과 가능
        # 하지만 L3 도구는 status=not_exposed 강제되어야 함 (도구등급화_매핑.md §2.3)
        assert is_tool_mcp_exposed(fake_l3) is False  # not_exposed 로 거부


# ===========================================================================
# Repository — agent_proposals.idempotency_key
# ===========================================================================


class TestRepositoryIdempotency:
    def test_alembic_revision_file_exists(self):
        from pathlib import Path
        ROOT = Path(__file__).resolve().parents[3]
        revision = (
            ROOT / "backend/app/db/migrations/versions"
            / "20260428_1400_s3_p4_agent_prop_idempotency.py"
        )
        assert revision.exists()
        content = revision.read_text(encoding="utf-8")
        assert "idempotency_key VARCHAR(128)" in content
        assert "UNIQUE INDEX" in content

    def test_lookup_idempotent_proposal_returns_existing(self):
        from app.services.agent_proposal_service import agent_proposal_service

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(
            return_value={
                "proposal_id": "ap-1",
                "version_id": "v-1",
                "status": "pending",
                "created_at": None,
                "document_id": "d-1",
                "workflow_status": "proposed",
            }
        )
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)
        result = agent_proposal_service._lookup_idempotent_proposal(
            conn, "agent-1", "key-1"
        )
        assert result is not None
        assert result["idempotent_replay"] is True
        assert result["version_id"] == "v-1"

    def test_lookup_returns_none_when_absent(self):
        from app.services.agent_proposal_service import agent_proposal_service

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(return_value=None)
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)
        assert agent_proposal_service._lookup_idempotent_proposal(
            conn, "agent-1", "missing"
        ) is None


# ===========================================================================
# audit chain (4 events 등재 확인)
# ===========================================================================


class TestAuditEvents:
    def test_event_types_md_lists_four_new_events(self):
        from pathlib import Path

        ROOT = Path(__file__).resolve().parents[3]
        et_md = ROOT / "backend/app/audit/event_types.md"
        content = et_md.read_text(encoding="utf-8")
        for ev in (
            "agent_proposal.requested",
            "agent_proposal.approved",
            "agent_proposal.merged",
            "agent_proposal.rolled_back",
        ):
            assert ev in content, f"event_types.md 에 {ev} 누락"


# ===========================================================================
# Schema parametric — Pydantic Literal 거부
# ===========================================================================


class TestPydanticContracts:
    def test_save_draft_request_required_content_snapshot(self):
        from app.schemas.mcp import SaveDraftRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SaveDraftRequest(idempotency_key="x")  # content_snapshot 누락

    def test_draft_impact_preview_default_zeros(self):
        from app.schemas.mcp import DraftImpactPreview

        d = DraftImpactPreview()
        assert d.nodes_added == 0
        assert d.chars_added == 0
        assert d.overwrites_existing_draft is False

    def test_save_draft_data_status_string(self):
        from app.schemas.mcp import DraftImpactPreview, SaveDraftData

        d = SaveDraftData(
            proposal_id="p",
            status="proposed",
            document_id="d",
            version_id="v",
            impact=DraftImpactPreview(),
            message="m",
        )
        # 프로파일 변경 시 새 status 도 받을 수 있도록 string (자유)
        d2 = SaveDraftData(
            proposal_id="p",
            status="approved",
            document_id="d",
            version_id="v",
            impact=DraftImpactPreview(),
            message="m",
        )
        assert d2.status == "approved"
