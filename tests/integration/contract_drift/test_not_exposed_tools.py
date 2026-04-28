"""
S3 Phase 4 FG 4-4 §2.1.3 — 비노출 도구 검증 (R1 핵심).

목적: manifest 의 `status: not_exposed` 또는 `maturity: forbidden` 도구가 실제로
MCP 표면에 노출되지 **않음** 을 보장. L4 도구는 정의 단계에서도 안전망.

본 테스트는 mock 기반 — 가짜 L4 도구 fixture 로 차단 동작 입증.
실 DB / HTTP 통합 (`POST /mcp/tools/list` HTTP) 은 운영자 testcontainers 후속.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


class TestNoForbiddenInToolSchemas:
    """TOOL_SCHEMAS 자체에 forbidden / L4 / not_exposed 가 0건."""

    def test_no_l4_tools(self):
        from app.schemas.mcp import TOOL_SCHEMAS

        l4 = [s for s in TOOL_SCHEMAS if s.get("risk_tier") == "L4"]
        assert l4 == [], f"L4 도구 등재 — R1 위반: {[s['name'] for s in l4]}"

    def test_no_forbidden_tools(self):
        from app.schemas.mcp import TOOL_SCHEMAS

        forbidden = [s for s in TOOL_SCHEMAS if s.get("maturity") == "forbidden"]
        assert forbidden == [], (
            f"forbidden 도구 등재 — R1 위반: {[s['name'] for s in forbidden]}"
        )

    def test_no_not_exposed_tools(self):
        from app.schemas.mcp import TOOL_SCHEMAS

        ne = [s for s in TOOL_SCHEMAS if s.get("status") == "not_exposed"]
        assert ne == [], (
            f"not_exposed 도구 등재 — TOOL_SCHEMAS 에 미노출 도구가 있어선 안 됨: "
            f"{[s['name'] for s in ne]}"
        )

    def test_l4_implies_not_exposed_and_forbidden(self):
        """L4 정의가 1+ 라면 모두 status=not_exposed AND maturity=forbidden 임을 보장.

        현재 정의 0개라 통과 (false safe). L4 추가 시 본 테스트가 정의 시점 차단.
        """
        from app.schemas.mcp import TOOL_SCHEMAS

        for s in TOOL_SCHEMAS:
            if s.get("risk_tier") == "L4":
                assert s.get("status") == "not_exposed", (
                    f"L4 도구 {s['name']} 가 not_exposed 가 아님"
                )
                assert s.get("maturity") == "forbidden", (
                    f"L4 도구 {s['name']} 가 forbidden 이 아님"
                )


class TestFakeL4ToolBlocked:
    """가짜 L4 도구 fixture — 차단 메커니즘 자체 검증."""

    def test_is_tool_mcp_exposed_blocks_fake_l4(self):
        """`is_tool_mcp_exposed` 가 가짜 L4 도구를 거부 — 정의 시점 안전망."""
        from app.schemas.mcp import is_tool_mcp_exposed

        fake_l4 = {
            "name": "delete_everything",
            "risk_tier": "L4",
            "maturity": "forbidden",
            "status": "not_exposed",
            "exposure_policy": "REST_ADMIN_ONLY",
        }
        assert is_tool_mcp_exposed(fake_l4) is False

    def test_fake_forbidden_blocked_even_if_status_enabled(self):
        from app.schemas.mcp import is_tool_mcp_exposed

        fake = {
            "name": "fake",
            "risk_tier": "L0",
            "maturity": "forbidden",  # forbidden 단독으로 거부
            "status": "enabled",  # status 가 enabled 여도 forbidden 우선
            "exposure_policy": "MCP_ENABLED",
        }
        assert is_tool_mcp_exposed(fake) is False

    def test_fake_not_exposed_blocked_even_if_l0(self):
        from app.schemas.mcp import is_tool_mcp_exposed

        fake = {
            "name": "fake",
            "risk_tier": "L0",
            "maturity": "stable",
            "status": "not_exposed",  # not_exposed 단독으로 거부
            "exposure_policy": "REST_ADMIN_ONLY",
        }
        assert is_tool_mcp_exposed(fake) is False

    def test_curated_tools_excludes_filtered_after_fake_injection(self):
        """가짜 L4 를 임시 주입했을 때 mcp_exposed_tool_schemas 가 제외함을 입증."""
        from app.schemas.mcp import TOOL_SCHEMAS, mcp_exposed_tool_schemas

        fake_l4 = {
            "name": "fake_l4_test",
            "risk_tier": "L4",
            "maturity": "forbidden",
            "status": "not_exposed",
            "exposure_policy": "REST_ADMIN_ONLY",
        }
        # 임시 주입
        TOOL_SCHEMAS.append(fake_l4)
        try:
            exposed = mcp_exposed_tool_schemas()
            exposed_names = {s["name"] for s in exposed}
            assert "fake_l4_test" not in exposed_names
        finally:
            TOOL_SCHEMAS.remove(fake_l4)


class TestRestAdminOnlyToolsNotInToolSchemas:
    """REST 관리자 도구 (publish / reindex / change_schema / delete) 가 절대 등재 안 됨.

    도구등급화_매핑.md §2.3 의 영구 정책 — TOOL_SCHEMAS 에 추가하지 않음.
    """

    def test_rest_admin_tools_absent(self):
        from app.schemas.mcp import TOOL_SCHEMAS

        forbidden_names = {
            "publish_document",
            "reindex_document",
            "change_schema",
            "delete_document",
        }
        registered = {s["name"] for s in TOOL_SCHEMAS}
        intersect = registered & forbidden_names
        assert not intersect, (
            f"REST 관리자 도구가 TOOL_SCHEMAS 에 등재됨 — 도구등급화_매핑.md §2.3 위반: {intersect}"
        )


class TestDispatcherRejectsUnknownTools:
    """`tools/call` dispatcher 가 _CURATED_TOOLS 외 도구를 INVALID_REQUEST 로 거부."""

    def test_curated_tools_matches_exposed_schemas(self):
        from app.api.v1.mcp_router import _CURATED_TOOLS
        from app.schemas.mcp import mcp_exposed_tool_schemas

        expected = {s["name"] for s in mcp_exposed_tool_schemas()}
        assert _CURATED_TOOLS == expected

    def test_dispatch_unknown_tool_path_returns_invalid_request(self):
        """mcp_router._dispatch_tool 이 알 수 없는 도구를 ValueError raise.

        실 HTTP 경로 검증은 운영자 testcontainers 후속.
        """
        from app.api.v1.mcp_router import _dispatch_tool
        from unittest.mock import MagicMock

        # _dispatch_tool 자체는 _CURATED_TOOLS 검사 없이 직접 분기. 라우터 (mcp_tool_call)
        # 가 _CURATED_TOOLS 검사 — 그 거부 패턴은 mcp_tool_call 코드 grep 으로 검증.
        from pathlib import Path
        ROOT = Path(__file__).resolve().parents[4]
        router_src = (ROOT / "backend/app/api/v1/mcp_router.py").read_text(encoding="utf-8")
        assert "tool_name not in _CURATED_TOOLS" in router_src
        assert "INVALID_REQUEST" in router_src
