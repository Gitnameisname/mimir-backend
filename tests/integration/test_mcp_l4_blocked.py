"""
S3 Phase 4 FG 4-0 §2.1.4: L4 차단 회귀 테스트.

R1 (Phase 4 개발계획서 §1.2): `risk_tier=L4` 또는 `maturity=forbidden` 도구는
MCP 표면에 절대 노출되지 않는다.

본 테스트는 두 축으로 검증:
  1. 정의 단계 안전망 — TOOL_SCHEMAS 자체에 L4/forbidden 도구가 등재되어 있지 않다.
  2. 동작 단계 차단 — 가짜 L4 도구를 fixture 로 주입했을 때 manifest 필터가 제외한다.

모든 테스트는 import 만 사용 — 실 DB 불필요 (구조/단위 수준).
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "mcp-l4-test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal-secret")


# ===========================================================================
# 정의 단계 안전망
# ===========================================================================


class TestL4DefinitionSafetyNet:
    """TOOL_SCHEMAS 정의 자체에 L4/forbidden 항목이 없음을 검증.

    R1 의 가장 안전한 방어선 — 정의가 들어와도 정의 단계에서 fail.
    """

    def test_no_l4_in_tool_schemas(self):
        """TOOL_SCHEMAS 모든 항목의 risk_tier 가 L4 가 아니다."""
        from app.schemas.mcp import TOOL_SCHEMAS

        l4_tools = [s for s in TOOL_SCHEMAS if s.get("risk_tier") == "L4"]
        assert l4_tools == [], (
            f"TOOL_SCHEMAS 에 L4 도구가 등재되어 있습니다 (R1 위반): "
            f"{[s.get('name') for s in l4_tools]}"
        )

    def test_no_forbidden_maturity_in_tool_schemas(self):
        """TOOL_SCHEMAS 모든 항목의 maturity 가 forbidden 이 아니다."""
        from app.schemas.mcp import TOOL_SCHEMAS

        forbidden_tools = [s for s in TOOL_SCHEMAS if s.get("maturity") == "forbidden"]
        assert forbidden_tools == [], (
            f"TOOL_SCHEMAS 에 maturity=forbidden 도구가 등재되어 있습니다 (R1 위반): "
            f"{[s.get('name') for s in forbidden_tools]}"
        )

    def test_no_not_exposed_in_tool_schemas(self):
        """TOOL_SCHEMAS 모든 항목의 status 가 not_exposed 가 아니다.

        not_exposed 도구는 TOOL_SCHEMAS 에 절대 등재하지 않는다 (도구등급화_매핑.md §3.3).
        """
        from app.schemas.mcp import TOOL_SCHEMAS

        not_exposed = [s for s in TOOL_SCHEMAS if s.get("status") == "not_exposed"]
        assert not_exposed == [], (
            f"TOOL_SCHEMAS 에 status=not_exposed 도구가 등재되어 있습니다 (도구등급화_매핑.md §3.3 위반): "
            f"{[s.get('name') for s in not_exposed]}"
        )

    def test_all_tools_have_required_manifest_fields(self):
        """모든 도구가 manifest 4 필드를 갖는다 (FG 4-0 §2.1.3)."""
        from app.schemas.mcp import TOOL_SCHEMAS

        required_keys = {"risk_tier", "maturity", "status", "exposure_policy"}
        missing: list[str] = []
        for s in TOOL_SCHEMAS:
            missing_keys = required_keys - set(s.keys())
            if missing_keys:
                missing.append(f"{s.get('name', '<unnamed>')}: {sorted(missing_keys)}")
        assert missing == [], f"manifest 필드 누락: {missing}"


# ===========================================================================
# 동작 단계 차단 (manifest 필터)
# ===========================================================================


class TestL4DispatchBlocked:
    """가짜 L4 도구가 TOOL_SCHEMAS 에 들어왔을 때 manifest 필터가 제외함을 검증."""

    def test_filter_excludes_l4_tool(self):
        """`is_tool_mcp_exposed` 가 L4 도구를 거부한다."""
        from app.schemas.mcp import is_tool_mcp_exposed

        fake_l4 = {
            "name": "fake_delete_document",
            "description": "(test) L4 destructive tool",
            "risk_tier": "L4",
            "maturity": "forbidden",
            "status": "not_exposed",
            "exposure_policy": "REST_ADMIN_ONLY",
        }
        assert is_tool_mcp_exposed(fake_l4) is False

    def test_filter_excludes_forbidden_maturity(self):
        """maturity=forbidden 만으로도 거부된다 (다른 필드 모두 안전해도)."""
        from app.schemas.mcp import is_tool_mcp_exposed

        fake = {
            "name": "fake_legacy_tool",
            "risk_tier": "L0",
            "maturity": "forbidden",  # forbidden 단독으로 거부
            "status": "enabled",
            "exposure_policy": "MCP_ENABLED",
        }
        assert is_tool_mcp_exposed(fake) is False

    def test_filter_excludes_not_exposed_status(self):
        """status=not_exposed 만으로도 거부된다."""
        from app.schemas.mcp import is_tool_mcp_exposed

        fake = {
            "name": "fake_admin_only_tool",
            "risk_tier": "L1",
            "maturity": "stable",
            "status": "not_exposed",  # not_exposed 단독으로 거부
            "exposure_policy": "REST_ADMIN_ONLY",
        }
        assert is_tool_mcp_exposed(fake) is False

    def test_filter_accepts_l0_l1_enabled(self):
        """L0/L1 + enabled + MCP_ENABLED 도구는 통과한다."""
        from app.schemas.mcp import is_tool_mcp_exposed

        l0_tool = {"risk_tier": "L0", "maturity": "stable", "status": "enabled",
                   "exposure_policy": "MCP_ENABLED"}
        l1_tool = {"risk_tier": "L1", "maturity": "stable", "status": "enabled",
                   "exposure_policy": "MCP_ENABLED"}
        assert is_tool_mcp_exposed(l0_tool) is True
        assert is_tool_mcp_exposed(l1_tool) is True

    def test_mcp_exposed_tool_schemas_filter(self):
        """`mcp_exposed_tool_schemas()` 가 위 필터를 적용해 노출 도구만 반환한다."""
        from app.schemas.mcp import TOOL_SCHEMAS, mcp_exposed_tool_schemas

        exposed = mcp_exposed_tool_schemas()
        # 본 시점 (FG 4-0 §3.1) 의 5 도구는 모두 L0/L1 + stable/beta + enabled
        # → 모두 노출 가능
        assert len(exposed) == len(TOOL_SCHEMAS), (
            "manifest 필터가 모든 정상 도구를 통과시켜야 함"
        )

    def test_curated_tools_excludes_filtered(self):
        """`mcp_router._CURATED_TOOLS` 가 필터된 도구만 포함한다."""
        from app.api.v1.mcp_router import _CURATED_TOOLS
        from app.schemas.mcp import mcp_exposed_tool_schemas

        expected = {s["name"] for s in mcp_exposed_tool_schemas()}
        assert _CURATED_TOOLS == expected, (
            f"_CURATED_TOOLS 가 manifest 필터 결과와 일치해야 함. "
            f"기대: {expected}, 실제: {_CURATED_TOOLS}"
        )


# ===========================================================================
# 외부 응답 view (도구등급화_매핑.md §4 옵션 A — 보수)
# ===========================================================================


class TestPublicViewExposesNoOpsInfo:
    """외부 tools/list 응답에 운영 정보 (`risk_tier`, `status`, `exposure_policy`)
    가 노출되지 않는다.

    도구등급화_매핑.md §4 옵션 A 채택. 운영 정보 누출 0.
    """

    def test_public_view_excludes_risk_tier(self):
        from app.schemas.mcp import mcp_exposed_public_view

        tool = {
            "name": "search_documents",
            "description": "...",
            "risk_tier": "L0",
            "maturity": "stable",
            "status": "enabled",
            "exposure_policy": "MCP_ENABLED",
            "authentication": {},
            "inputSchema": {},
        }
        view = mcp_exposed_public_view(tool)
        assert "risk_tier" not in view
        assert "status" not in view
        assert "exposure_policy" not in view

    def test_public_view_includes_maturity(self):
        from app.schemas.mcp import mcp_exposed_public_view

        tool = {
            "name": "search_documents",
            "description": "...",
            "risk_tier": "L0",
            "maturity": "stable",
            "status": "enabled",
            "exposure_policy": "MCP_ENABLED",
        }
        view = mcp_exposed_public_view(tool)
        assert view.get("maturity") == "stable"

    def test_public_view_keeps_essentials(self):
        from app.schemas.mcp import mcp_exposed_public_view

        tool = {
            "name": "fetch_node",
            "description": "Fetch a node",
            "risk_tier": "L0",
            "maturity": "stable",
            "status": "enabled",
            "exposure_policy": "MCP_ENABLED",
            "authentication": {"method": "oauth2"},
            "inputSchema": {"type": "object"},
        }
        view = mcp_exposed_public_view(tool)
        assert view["name"] == "fetch_node"
        assert "description" in view
        assert "authentication" in view
        assert "inputSchema" in view
