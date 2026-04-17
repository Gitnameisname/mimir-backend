"""
Phase 4 단위 테스트 — MCP 프로토콜, Scope Profile 필터, 도구 입출력.

FG4.1 검수 기준:
  - MCP 2025-11-25 스펙 호환성 (initialize 응답 구조)
  - 도구 3종 입출력 스키마 정확성
  - Resource URI 파싱

FG4.2 검수 기준:
  - FilterExpression 파서 정확성
  - $ctx 동적 변수 치환
  - SQL 빌더 정확성

FG4.3 검수 기준:
  - 응답 envelope 구조 일관성
  - 오류 코드 enum 완전성
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from app.api.auth.models import ActorContext, ActorType, AuthMethod
from app.mcp.errors import MCPError, MCPErrorCode
from app.mcp.resources import parse_resource_uri
from app.schemas.mcp import (
    MIMIR_EXTENSIONS,
    TOOL_SCHEMAS,
    MCPCapabilities,
    MCPInitializeResponse,
    MCPResponse,
    SearchDocumentsRequest,
    VerifyCitationRequest,
    FetchNodeRequest,
)
from app.services.filter_expression import (
    build_sql_filter,
    parse_filter_expression,
    substitute_ctx,
)
from app.models.scope_profile import FilterCondition, FilterExpression


# ---------------------------------------------------------------------------
# FG4.2: FilterExpression 파서 테스트
# ---------------------------------------------------------------------------

class TestFilterExpressionParser:
    def test_parse_empty(self):
        expr = parse_filter_expression({})
        assert expr.is_empty()

    def test_parse_and_eq(self):
        raw = {
            "and": [
                {"field": "organization_id", "op": "eq", "value": "org-123"},
            ]
        }
        expr = parse_filter_expression(raw)
        assert len(expr.and_) == 1
        assert expr.and_[0].field == "organization_id"
        assert expr.and_[0].op == "eq"
        assert expr.and_[0].value == "org-123"

    def test_parse_or(self):
        raw = {
            "or": [
                {"field": "visibility", "op": "eq", "value": "public"},
                {"field": "organization_id", "op": "eq", "value": "org-456"},
            ]
        }
        expr = parse_filter_expression(raw)
        assert len(expr.or_) == 2

    def test_parse_invalid_op(self):
        raw = {"and": [{"field": "organization_id", "op": "like", "value": "%test%"}]}
        with pytest.raises(ValueError, match="Unsupported op"):
            parse_filter_expression(raw)

    def test_parse_invalid_field(self):
        raw = {"and": [{"field": "password", "op": "eq", "value": "secret"}]}
        with pytest.raises(ValueError, match="Unsupported field"):
            parse_filter_expression(raw)

    def test_parse_in_operator(self):
        raw = {"and": [{"field": "organization_id", "op": "in", "value": ["org-1", "org-2"]}]}
        expr = parse_filter_expression(raw)
        assert expr.and_[0].op == "in"
        assert expr.and_[0].value == ["org-1", "org-2"]


# ---------------------------------------------------------------------------
# FG4.2: $ctx 변수 치환 테스트
# ---------------------------------------------------------------------------

class TestCtxSubstitution:
    def test_substitute_literal(self):
        expr = FilterExpression(
            and_=[FilterCondition(field="organization_id", op="eq", value="literal-val")]
        )
        result = substitute_ctx(expr, {"organization_id": "actual-org"})
        assert result.and_[0].value == "literal-val"  # 리터럴은 그대로

    def test_substitute_ctx_var(self):
        expr = FilterExpression(
            and_=[FilterCondition(field="organization_id", op="eq", value="$ctx.organization_id")]
        )
        result = substitute_ctx(expr, {"organization_id": "actual-org-456"})
        assert result.and_[0].value == "actual-org-456"

    def test_substitute_missing_ctx_key(self):
        expr = FilterExpression(
            and_=[FilterCondition(field="organization_id", op="eq", value="$ctx.organization_id")]
        )
        result = substitute_ctx(expr, {})
        assert result.and_[0].value is None  # 없으면 None

    def test_substitute_disallowed_ctx_key(self):
        expr = FilterExpression(
            and_=[FilterCondition(field="organization_id", op="eq", value="$ctx.password")]
        )
        with pytest.raises(ValueError, match="허용되지 않은 \\$ctx 변수"):
            substitute_ctx(expr, {"password": "secret"})

    def test_substitute_in_list(self):
        expr = FilterExpression(
            and_=[FilterCondition(field="organization_id", op="in",
                                  value=["$ctx.organization_id", "literal"])]
        )
        result = substitute_ctx(expr, {"organization_id": "org-789"})
        assert result.and_[0].value == ["org-789", "literal"]


# ---------------------------------------------------------------------------
# FG4.2: SQL 빌더 테스트
# ---------------------------------------------------------------------------

class TestSqlBuilder:
    def test_empty_expr(self):
        expr = FilterExpression()
        sql, params = build_sql_filter(expr)
        assert sql == ""
        assert params == []

    def test_eq_condition(self):
        expr = FilterExpression(
            and_=[FilterCondition(field="organization_id", op="eq", value="org-123")]
        )
        sql, params = build_sql_filter(expr)
        assert "AND (" in sql
        assert "d.organization_id = %s" in sql
        assert params == ["org-123"]

    def test_in_condition(self):
        expr = FilterExpression(
            and_=[FilterCondition(field="organization_id", op="in", value=["a", "b"])]
        )
        sql, params = build_sql_filter(expr)
        assert "IN (%s,%s)" in sql
        assert params == ["a", "b"]

    def test_or_conditions_wrapped(self):
        expr = FilterExpression(
            or_=[
                FilterCondition(field="organization_id", op="eq", value="org-1"),
                FilterCondition(field="organization_id", op="eq", value="org-2"),
            ]
        )
        sql, params = build_sql_filter(expr)
        assert "OR" in sql
        assert len(params) == 2


# ---------------------------------------------------------------------------
# FG4.1: Resource URI 파싱 테스트
# ---------------------------------------------------------------------------

class TestResourceUri:
    def test_valid_uri(self):
        uri = "mimir://documents/doc-123/versions/ver-456/nodes/node-789"
        resource = parse_resource_uri(uri)
        assert resource is not None
        assert resource.document_id == "doc-123"
        assert resource.version_id == "ver-456"
        assert resource.node_id == "node-789"
        assert resource.mime_type == "text/plain"

    def test_invalid_uri(self):
        assert parse_resource_uri("https://example.com") is None
        assert parse_resource_uri("mimir://docs/only") is None
        assert parse_resource_uri("") is None

    def test_uri_roundtrip(self):
        uri = "mimir://documents/d1/versions/v1/nodes/n1"
        resource = parse_resource_uri(uri)
        assert resource.uri == uri


# ---------------------------------------------------------------------------
# FG4.1: MCP 초기화 응답 스키마 테스트
# ---------------------------------------------------------------------------

class TestMCPInitializeResponse:
    def test_initialize_response_structure(self):
        resp = MCPInitializeResponse(
            server_id="mimir-s2",
            version="2.0.0",
            protocol_version="2025-11-25",
            capabilities=MCPCapabilities(
                tools=["search_documents", "fetch_node", "verify_citation"],
                resources=True,
                prompts=True,
                tasks=False,
            ),
        )
        d = resp.model_dump()
        assert d["server_id"] == "mimir-s2"
        assert d["protocol_version"] == "2025-11-25"
        assert "search_documents" in d["capabilities"]["tools"]
        assert d["capabilities"]["tasks"] is False

    def test_tool_schemas_completeness(self):
        """3종 도구 스키마가 모두 정의되어 있는지 확인."""
        names = {s["name"] for s in TOOL_SCHEMAS}
        assert "search_documents" in names
        assert "fetch_node" in names
        assert "verify_citation" in names

    def test_tool_schemas_have_required_fields(self):
        for schema in TOOL_SCHEMAS:
            assert "name" in schema
            assert "description" in schema
            assert "inputSchema" in schema
            assert "authentication" in schema

    def test_extensions_declared(self):
        assert len(MIMIR_EXTENSIONS) == 2
        ext_names = {e["name"] for e in MIMIR_EXTENSIONS}
        assert "mimir.citation-5tuple" in ext_names
        assert "mimir.span-backref" in ext_names


# ---------------------------------------------------------------------------
# FG4.3: 응답 envelope 테스트
# ---------------------------------------------------------------------------

class TestMCPResponseEnvelope:
    def test_success_envelope(self):
        resp = MCPResponse(success=True, data={"key": "value"})
        assert resp.success is True
        assert resp.error is None
        assert resp.data == {"key": "value"}

    def test_error_envelope(self):
        from app.schemas.mcp import MCPErrorDetail
        resp = MCPResponse(
            success=False,
            error=MCPErrorDetail(code="UNAUTHORIZED", message="Access denied"),
        )
        assert resp.success is False
        assert resp.error.code == "UNAUTHORIZED"
        assert resp.data is None

    def test_error_codes_enum_completeness(self):
        expected = {
            "UNAUTHORIZED", "NOT_FOUND", "INVALID_SCOPE",
            "INVALID_CITATION", "RATE_LIMIT", "INVALID_REQUEST",
            "AGENT_DISABLED", "INTERNAL_ERROR",
        }
        actual = {e.value for e in MCPErrorCode}
        assert expected == actual


# ---------------------------------------------------------------------------
# FG4.1: 도구 입력 스키마 유효성 테스트
# ---------------------------------------------------------------------------

class TestToolInputSchemas:
    def test_search_documents_request_required(self):
        req = SearchDocumentsRequest(query="테스트 쿼리")
        assert req.query == "테스트 쿼리"
        assert req.top_k == 5
        assert req.scope == "default"

    def test_search_documents_request_top_k_bounds(self):
        with pytest.raises(Exception):
            SearchDocumentsRequest(query="q", top_k=0)
        with pytest.raises(Exception):
            SearchDocumentsRequest(query="q", top_k=51)

    def test_verify_citation_request(self):
        req = VerifyCitationRequest(
            document_id="doc-1",
            version_id="ver-1",
            node_id="node-1",
            content_hash="abc123",
        )
        assert req.document_id == "doc-1"
        assert req.span_offset is None

    def test_fetch_node_request(self):
        req = FetchNodeRequest(document_id="doc-1", node_id="node-1")
        assert req.version_id is None  # 선택 필드


# ---------------------------------------------------------------------------
# FG4.2: ActorType.AGENT 테스트
# ---------------------------------------------------------------------------

class TestAgentActorType:
    def test_agent_actor_context(self):
        ctx = ActorContext(
            actor_type=ActorType.AGENT,
            actor_id="agent-uuid",
            is_authenticated=True,
            auth_method=AuthMethod.API_KEY,
            tenant_id="org-uuid",
            agent_id="agent-uuid",
            scope_profile_id="profile-uuid",
        )
        assert ctx.is_agent
        assert not ctx.is_anonymous
        assert not ctx.is_service
        assert ctx.audit_actor_type == "agent"
        assert ctx.scope_profile_id == "profile-uuid"

    def test_actor_type_values(self):
        assert ActorType.AGENT == "agent"
        assert ActorType.USER == "user"
        assert ActorType.SERVICE == "service"
        assert ActorType.ANONYMOUS == "anonymous"

    def test_acting_on_behalf_of(self):
        ctx = ActorContext(
            actor_type=ActorType.AGENT,
            actor_id="agent-1",
            is_authenticated=True,
            auth_method=AuthMethod.API_KEY,
            tenant_id=None,
            agent_id="agent-1",
            acting_on_behalf_of="user-999",
        )
        assert ctx.acting_on_behalf_of == "user-999"


# ---------------------------------------------------------------------------
# FG4.2: FilterCondition 유효성 테스트
# ---------------------------------------------------------------------------

class TestFilterConditionValidation:
    def test_valid_condition(self):
        cond = FilterCondition(field="organization_id", op="eq", value="test")
        cond.validate()  # 예외 없어야 함

    def test_invalid_op(self):
        cond = FilterCondition(field="organization_id", op="regexp", value=".*")
        with pytest.raises(ValueError, match="Unsupported op"):
            cond.validate()

    def test_invalid_field(self):
        cond = FilterCondition(field="secret_key", op="eq", value="val")
        with pytest.raises(ValueError, match="Unsupported field"):
            cond.validate()


# ---------------------------------------------------------------------------
# REC-4.1: Rate Limit 상수 및 라우터 엔드포인트 등록 확인
# ---------------------------------------------------------------------------

class TestRateLimitConstants:
    def test_mcp_rate_limit_values(self):
        from app.api.v1.mcp_router import (
            _MCP_INIT_LIMIT,
            _MCP_READ_LIMIT,
            _MCP_STREAM_LIMIT,
            _MCP_TOOL_LIMIT,
        )
        assert _MCP_INIT_LIMIT == "30/minute"
        assert _MCP_TOOL_LIMIT == "20/minute"
        assert _MCP_STREAM_LIMIT == "10/minute"
        assert _MCP_READ_LIMIT == "60/minute"

    def test_mcp_router_has_all_endpoints(self):
        from app.api.v1.mcp_router import router
        paths = {r.path for r in router.routes}
        assert "/initialize" in paths
        assert "/tools/call" in paths
        assert "/tools/call/stream" in paths
        assert "/resources" in paths
        assert "/resources/read" in paths
        assert "/prompts" in paths
        assert "/tools" in paths

    def test_tool_limit_stricter_than_read_limit(self):
        from app.api.v1.mcp_router import _MCP_READ_LIMIT, _MCP_TOOL_LIMIT
        tool_count = int(_MCP_TOOL_LIMIT.split("/")[0])
        read_count = int(_MCP_READ_LIMIT.split("/")[0])
        assert tool_count < read_count  # 도구 호출이 더 엄격해야 함

    def test_stream_limit_strictest(self):
        from app.api.v1.mcp_router import _MCP_STREAM_LIMIT, _MCP_TOOL_LIMIT
        stream_count = int(_MCP_STREAM_LIMIT.split("/")[0])
        tool_count = int(_MCP_TOOL_LIMIT.split("/")[0])
        assert stream_count < tool_count  # SSE가 가장 엄격해야 함


# ---------------------------------------------------------------------------
# REC-4.2: Scope Profile 변경 시 영향 에이전트 목록 쿼리
# ---------------------------------------------------------------------------

class TestAffectedAgentsQuery:
    def _make_conn(self, rows):
        """cursor() 컨텍스트 매니저를 모킹한 DB 연결을 반환한다."""
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = rows
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn.cursor.return_value.__exit__.return_value = False
        return mock_conn

    def test_returns_agent_list(self):
        from app.api.v1.scope_profiles import _get_affected_agents
        conn = self._make_conn([
            {"id": "agent-1", "name": "Agent One"},
            {"id": "agent-2", "name": "Agent Two"},
        ])
        result = _get_affected_agents(conn, "profile-uuid")
        assert len(result) == 2
        assert result[0] == {"id": "agent-1", "name": "Agent One"}
        assert result[1] == {"id": "agent-2", "name": "Agent Two"}

    def test_returns_empty_when_no_agents(self):
        from app.api.v1.scope_profiles import _get_affected_agents
        conn = self._make_conn([])
        result = _get_affected_agents(conn, "profile-uuid")
        assert result == []

    def test_id_converted_to_str(self):
        from app.api.v1.scope_profiles import _get_affected_agents
        import uuid
        agent_uuid = uuid.uuid4()
        conn = self._make_conn([{"id": agent_uuid, "name": "UUID Agent"}])
        result = _get_affected_agents(conn, "profile-uuid")
        assert isinstance(result[0]["id"], str)
        assert result[0]["id"] == str(agent_uuid)


# ---------------------------------------------------------------------------
# REC-4.3: Agent API Key expires_at 필수 강제
# ---------------------------------------------------------------------------

class TestAgentKeyExpiration:
    def _make_conn(self, agent_row):
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = agent_row
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn.cursor.return_value.__exit__.return_value = False
        return mock_conn

    def test_key_without_expires_at_rejected(self):
        from app.api.auth.dependencies import _extract_agent_context
        conn = self._make_conn(None)
        api_key_row = {"agent_id": "agent-uuid", "expires_at": None, "scope_profile_id": None}
        result = _extract_agent_context(conn, api_key_row)
        assert not result.is_authenticated
        assert result.actor_type == ActorType.ANONYMOUS

    def test_key_with_expires_at_authenticated(self):
        from datetime import datetime, timezone
        from app.api.auth.dependencies import _extract_agent_context
        conn = self._make_conn({
            "is_disabled": False,
            "organization_id": "org-1",
            "scope_profile_id": "profile-1",
        })
        api_key_row = {
            "agent_id": "agent-uuid",
            "expires_at": datetime(2027, 1, 1, tzinfo=timezone.utc),
            "scope_profile_id": None,
        }
        result = _extract_agent_context(conn, api_key_row)
        assert result.is_authenticated
        assert result.actor_type == ActorType.AGENT
        assert result.agent_id == "agent-uuid"

    def test_scope_profile_from_api_key_takes_precedence(self):
        from datetime import datetime, timezone
        from app.api.auth.dependencies import _extract_agent_context
        conn = self._make_conn({
            "is_disabled": False,
            "organization_id": "org-1",
            "scope_profile_id": "agent-profile",
        })
        api_key_row = {
            "agent_id": "agent-uuid",
            "expires_at": datetime(2027, 1, 1, tzinfo=timezone.utc),
            "scope_profile_id": "key-level-profile",
        }
        result = _extract_agent_context(conn, api_key_row)
        assert result.scope_profile_id == "key-level-profile"

    def test_kill_switched_agent_rejected_even_with_valid_expiry(self):
        from datetime import datetime, timezone
        from app.api.auth.dependencies import _extract_agent_context
        conn = self._make_conn({
            "is_disabled": True,
            "organization_id": "org-1",
            "scope_profile_id": None,
        })
        api_key_row = {
            "agent_id": "agent-uuid",
            "expires_at": datetime(2027, 1, 1, tzinfo=timezone.utc),
            "scope_profile_id": None,
        }
        result = _extract_agent_context(conn, api_key_row)
        assert not result.is_authenticated
        assert result.actor_type == ActorType.ANONYMOUS

    def test_nonexistent_agent_rejected(self):
        from datetime import datetime, timezone
        from app.api.auth.dependencies import _extract_agent_context
        conn = self._make_conn(None)  # DB에 agent 없음
        api_key_row = {
            "agent_id": "ghost-agent",
            "expires_at": datetime(2027, 1, 1, tzinfo=timezone.utc),
            "scope_profile_id": None,
        }
        result = _extract_agent_context(conn, api_key_row)
        assert not result.is_authenticated
