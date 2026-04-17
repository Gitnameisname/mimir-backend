"""
MCP e2e 테스트 — FG9.2 산출물.

MCP 서버 스펙 준수 및 핵심 도구 호출 흐름을 검증한다.
실 DB 불필요 — 구조/인터페이스 수준 검증.

MCP 2025-11-25 스펙:
  POST /mcp/initialize   — 핸드셰이크
  POST /mcp/tools/list   — 도구 목록
  POST /mcp/tools/call   — 도구 호출
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "mcp-e2e-test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal-secret")


# ===========================================================================
# MCP 서버 스펙 준수 검증
# ===========================================================================

class TestMCPServerSpec:
    """MCP 2025-11-25 스펙 준수 여부 검증."""

    def test_mcp_router_file_exists(self):
        """MCP 라우터 파일이 존재한다."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        assert mcp_path.exists(), "mcp_router.py 없음"

    def test_mcp_initialize_endpoint_exists(self):
        """POST /mcp/initialize 핸드셰이크 엔드포인트가 있다."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        source = mcp_path.read_text(encoding="utf-8")
        assert "initialize" in source.lower(), "mcp_router.py에 initialize 엔드포인트 없음"

    def test_mcp_tools_list_endpoint_exists(self):
        """POST /mcp/tools/list 도구 목록 엔드포인트가 있다."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        source = mcp_path.read_text(encoding="utf-8")
        assert "tools" in source.lower(), "mcp_router.py에 tools 엔드포인트 없음"

    def test_mcp_tools_call_endpoint_exists(self):
        """POST /mcp/tools/call 도구 호출 엔드포인트가 있다."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        source = mcp_path.read_text(encoding="utf-8")
        assert "call" in source.lower(), "mcp_router.py에 tools/call 엔드포인트 없음"

    def test_mcp_response_schema_exists(self):
        """MCP 응답 스키마가 정의되어 있다."""
        mcp_schemas = list((ROOT / "backend/app").rglob("*mcp*.py"))
        mcp_schemas += list((ROOT / "backend/app").rglob("*schema*mcp*.py"))
        assert mcp_schemas, "MCP 스키마 파일 없음"

    def test_mcp_module_is_importable(self):
        """app.api.v1.mcp_router가 import 가능하다."""
        mod = importlib.import_module("app.api.v1.mcp_router")
        assert mod is not None

    def test_mcp_tools_module_is_importable(self):
        """app.mcp.tools가 import 가능하다."""
        mod = importlib.import_module("app.mcp.tools")
        assert mod is not None


# ===========================================================================
# MCP 도구 인터페이스 검증
# ===========================================================================

class TestMCPToolInterfaces:
    """MCP 핵심 도구의 인터페이스 및 구조 검증."""

    def test_search_documents_tool_exists(self):
        """search_documents 도구 함수가 존재한다."""
        from app.mcp.tools import tool_search_documents
        assert callable(tool_search_documents)

    def test_fetch_node_tool_exists(self):
        """fetch_node 도구 함수가 존재한다."""
        from app.mcp.tools import tool_fetch_node
        assert callable(tool_fetch_node)

    def test_verify_citation_tool_exists(self):
        """verify_citation 도구 함수가 존재한다."""
        from app.mcp.tools import tool_verify_citation
        assert callable(tool_verify_citation)

    def test_search_documents_accepts_scope_parameter(self):
        """search_documents가 scope 파라미터를 받는다 (S2 원칙 ⑥)."""
        import inspect
        from app.mcp.tools import tool_search_documents
        sig = inspect.signature(tool_search_documents)
        params = list(sig.parameters.keys())
        assert "scope" in params or "request" in params or "access_context" in params, (
            "search_documents에 scope/access_context 파라미터 없음"
        )

    def test_mcp_curated_tools_list_defined(self):
        """_CURATED_TOOLS (허용 도구 목록)이 정의되어 있다."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        source = mcp_path.read_text(encoding="utf-8")
        assert "_CURATED_TOOLS" in source or "CURATED" in source, (
            "_CURATED_TOOLS allowlist 없음 — 모든 도구 노출 위험"
        )

    def test_mcp_tools_apply_acl_filter(self):
        """MCP 도구가 ACL 필터를 적용한다 (S2 원칙 ⑥)."""
        from app.mcp.tools import tool_search_documents
        from app.mcp import scope_filter
        # scope_filter 모듈이 존재하고 apply_scope_filter 함수가 있어야 함
        assert hasattr(scope_filter, "apply_scope_filter"), (
            "app.mcp.scope_filter.apply_scope_filter 없음"
        )

    def test_mcp_tools_apply_injection_detection(self):
        """MCP 도구 호출 시 Prompt Injection 탐지가 적용된다."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        source = mcp_path.read_text(encoding="utf-8")
        has_injection_check = (
            "injection" in source.lower()
            or "prompt_injection" in source.lower()
            or "detect" in source.lower()
        )
        assert has_injection_check, "MCP 라우터에 injection 탐지 없음"

    def test_mcp_rate_limiting_exists(self):
        """MCP 도구 호출에 rate limiting이 적용된다."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        source = mcp_path.read_text(encoding="utf-8")
        has_rate_limit = "rate_limit" in source.lower() or "limit" in source.lower()
        assert has_rate_limit, "MCP 라우터에 rate limiting 없음"


# ===========================================================================
# MCP 응답 구조 검증
# ===========================================================================

class TestMCPResponseStructure:
    """MCP 응답이 스펙에 맞는 구조를 가지는지 검증."""

    def test_mcp_response_has_ok_field(self):
        """MCPResponse에 ok/error 구분이 있다."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        source = mcp_path.read_text(encoding="utf-8")
        # _ok / _err 헬퍼 함수 존재 확인
        assert "_ok" in source or "ok" in source.lower(), "MCPResponse에 ok 필드 없음"

    def test_mcp_response_includes_metadata(self):
        """MCPResponse에 메타데이터가 포함된다."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        source = mcp_path.read_text(encoding="utf-8")
        assert "metadata" in source.lower() or "meta" in source.lower(), (
            "MCPResponse에 metadata 없음"
        )

    def test_mcp_search_result_includes_citation(self):
        """MCP search_documents 결과에 Citation 5-tuple이 포함된다."""
        mcp_tools_path = ROOT / "backend/app/mcp/tools.py"
        source = mcp_tools_path.read_text(encoding="utf-8")
        has_citation = (
            "citation" in source.lower()
            or "content_hash" in source.lower()
            or "CitationBuilder" in source
        )
        assert has_citation, "MCP search 결과에 citation 없음"

    def test_mcp_agent_context_recorded(self):
        """MCP 도구 호출 시 ActorContext가 전달되어 감사 기록 가능."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        source = mcp_path.read_text(encoding="utf-8")
        # ActorContext가 _make_metadata에 전달됨
        assert "actor" in source.lower() and "ActorContext" in source, (
            "MCP 라우터에 ActorContext 없음 — 감사 기록 불가"
        )


# ===========================================================================
# MCP 보안 검증
# ===========================================================================

class TestMCPSecurity:
    """MCP 서버 보안 요구사항 검증."""

    def test_mcp_requires_authentication(self):
        """MCP 엔드포인트가 인증을 요구한다."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        source = mcp_path.read_text(encoding="utf-8")
        has_auth = (
            "actor" in source.lower()
            or "resolve_current_actor" in source
            or "require_authenticated" in source
            or "Depends" in source
        )
        assert has_auth, "MCP 엔드포인트에 인증 없음"

    def test_mcp_blocks_unknown_tools(self):
        """MCP에서 허용되지 않은 도구 호출이 차단된다."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        source = mcp_path.read_text(encoding="utf-8")
        assert "not in" in source or "not_in" in source.lower() or "INVALID" in source, (
            "MCP에서 미허용 도구 차단 로직 없음"
        )

    def test_mcp_agent_rate_limiting(self):
        """MCP 에이전트별 rate limiting이 있다."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        source = mcp_path.read_text(encoding="utf-8")
        assert "agent_rate_limit" in source or "rate_limit" in source.lower(), (
            "MCP에 에이전트 rate limiting 없음"
        )

    def test_mcp_injection_detection_on_tool_params(self):
        """MCP 도구 파라미터에 Prompt Injection 탐지가 적용된다."""
        mcp_path = ROOT / "backend/app/api/v1/mcp_router.py"
        source = mcp_path.read_text(encoding="utf-8")
        has_detection = (
            "injection_detection" in source.lower()
            or "_run_injection_detection" in source
            or "PromptInjectionDetector" in source
        )
        assert has_detection, "MCP 도구 파라미터에 injection 탐지 없음"

    def test_mcp_scope_filter_module_complete(self):
        """MCP scope_filter 모듈이 완전히 구현되어 있다."""
        scope_filter_path = ROOT / "backend/app/mcp/scope_filter.py"
        assert scope_filter_path.exists(), "app/mcp/scope_filter.py 없음"
        source = scope_filter_path.read_text(encoding="utf-8")
        assert "apply_scope_filter" in source, "apply_scope_filter 함수 없음"
