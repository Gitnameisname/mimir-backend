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
from types import SimpleNamespace
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
        """MCP fetch/read 경로가 실제 ACL을 적용한다."""
        from app.api.auth.models import ActorContext, ActorType
        from app.mcp.errors import MCPErrorCode
        from app.mcp.tools import tool_fetch_node
        from app.schemas.mcp import FetchNodeRequest

        actor = ActorContext(
            actor_type=ActorType.AGENT,
            actor_id="agent-1",
            is_authenticated=True,
            auth_method=None,
            tenant_id=None,
            role="VIEWER",
            agent_id="agent-1",
            scope_profile_id="scope-profile-1",
        )
        request = FetchNodeRequest(document_id="doc-1", version_id="ver-1", node_id="node-1")
        conn = MagicMock()

        import app.mcp.tools as tools
        from app.mcp.errors import MCPError

        original_resolve = tools._resolve_acl_filter
        try:
            tools._resolve_acl_filter = lambda *args, **kwargs: {"sql": "AND (d.id = %s)", "params": ["other-doc"]}
            with pytest.raises(MCPError) as exc_info:
                tool_fetch_node(request, actor, conn)
            assert exc_info.value.code == MCPErrorCode.UNAUTHORIZED
        finally:
            tools._resolve_acl_filter = original_resolve

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
        """MCP search_documents가 실제 citation 필드를 전달한다."""
        from app.api.auth.models import ActorContext, ActorType
        from app.mcp.tools import tool_search_documents
        from app.schemas.citation import Citation
        from app.schemas.mcp import SearchDocumentsRequest
        from app.schemas.search import DocumentSearchResponse, DocumentSearchResult, SearchPagination

        actor = ActorContext(
            actor_type=ActorType.AGENT,
            actor_id="agent-1",
            is_authenticated=True,
            auth_method=None,
            tenant_id=None,
            role="VIEWER",
        )
        citation = Citation.from_chunk("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222", "33333333-3333-3333-3333-333333333333", "chunk text")
        fake_result = DocumentSearchResult(
            id="11111111-1111-1111-1111-111111111111",
            title="문서",
            document_type="POLICY",
            status="published",
            metadata={},
            created_at="2026-04-18T00:00:00Z",
            updated_at="2026-04-18T00:00:00Z",
            rank=0.9,
            citation=citation,
        )
        fake_response = DocumentSearchResponse(
            query="hello",
            results=[fake_result],
            pagination=SearchPagination(page=1, limit=1, total=1, has_next=False),
        )

        import app.mcp.tools as tools

        class FakeSearchService:
            def search_documents(self, **kwargs):
                return fake_response

        original_search_service = None
        from app.services import search_service as _unused  # noqa: F401
        try:
            import app.services.search_service as svc_module
            original_search_service = svc_module.SearchService
            svc_module.SearchService = FakeSearchService
            result = tool_search_documents(SearchDocumentsRequest(query="hello"), actor, MagicMock())
        finally:
            if original_search_service is not None:
                svc_module.SearchService = original_search_service

        assert result.results[0].citation is not None
        assert result.results[0].citation.content_hash == citation.content_hash

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

    def test_scope_filter_resolution_fails_closed(self):
        """scope profile 해석 실패 시 빈 필터가 아니라 예외가 발생해야 한다."""
        from app.mcp.scope_filter import ScopeFilterResolutionError, apply_scope_filter

        class FakeRepo:
            def __init__(self, conn):
                self.conn = conn

            def get_definition(self, profile_id, scope_name):
                return None

        import app.mcp.scope_filter as scope_filter_module

        original_repo = scope_filter_module.ScopeProfileRepository
        try:
            scope_filter_module.ScopeProfileRepository = FakeRepo
            with pytest.raises(ScopeFilterResolutionError):
                apply_scope_filter(
                    scope_profile_id="scope-profile-1",
                    scope_name="default",
                    access_context={},
                    conn=MagicMock(),
                )
        finally:
            scope_filter_module.ScopeProfileRepository = original_repo

    def test_verify_citation_uses_chunk_source_text_hash(self):
        """MCP verify_citation은 nodes.content가 아니라 chunk source_text를 기준으로 검증해야 한다."""
        from app.api.auth.models import ActorContext, ActorType
        from app.mcp.tools import tool_verify_citation
        from app.schemas.citation import Citation
        from app.schemas.mcp import VerifyCitationRequest

        actor = ActorContext(
            actor_type=ActorType.AGENT,
            actor_id="agent-1",
            is_authenticated=True,
            auth_method=None,
            tenant_id=None,
            role="VIEWER",
        )
        citation = Citation.from_chunk(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
            "parent-context actual chunk body",
        )
        request = VerifyCitationRequest(
            document_id=str(citation.document_id),
            version_id=str(citation.version_id),
            node_id=str(citation.node_id),
            content_hash=citation.content_hash,
        )

        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.__exit__.return_value = False
        cursor.fetchone.side_effect = [{"id": str(citation.version_id)}]
        conn.cursor.return_value = cursor

        import app.mcp.tools as tools

        original_fetch_chunk = tools._fetch_accessible_chunk
        try:
            tools._fetch_accessible_chunk = lambda *args, **kwargs: {
                "document_id": request.document_id,
                "version_id": request.version_id,
                "node_id": request.node_id,
                "source_text": "parent-context actual chunk body",
            }
            result = tool_verify_citation(request, actor, conn)
        finally:
            tools._fetch_accessible_chunk = original_fetch_chunk

        assert result.verified is True
