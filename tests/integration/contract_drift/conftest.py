"""S3 Phase 4 FG 4-4 — Contract Drift 테스트 공용 fixture.

mock 기반 dual-client + seed helpers. 실 DB testcontainers 와 분리 — 본 fixture 는
구조 / 의도 검증용. 실 DB 통합 검증은 운영자 testcontainers 설정 후 추가 (별 라운드).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "fg44-contract-drift-test")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal-secret")


# ---------------------------------------------------------------------------
# Dual client — REST / MCP 를 동일 actor 로 호출하는 thin wrapper
# ---------------------------------------------------------------------------


@pytest.fixture
def user_actor():
    """user actor — _check_tool_allowed 비대상."""
    from app.api.auth.models import ActorContext, ActorType, AuthMethod

    return ActorContext(
        actor_type=ActorType.USER,
        actor_id="u1",
        is_authenticated=True,
        auth_method=AuthMethod.SESSION,
        tenant_id=None,
        role="VIEWER",
    )


@pytest.fixture
def agent_actor_with_full_scope():
    """agent actor — allowed_tools 전체 통과 (mock).

    `_check_tool_allowed` 가 ScopeProfile lookup 을 시도하므로
    `can_call_tool` 을 monkeypatch 해야 함 (각 테스트가 patch).
    """
    from app.api.auth.models import ActorContext, ActorType, AuthMethod

    return ActorContext(
        actor_type=ActorType.AGENT,
        actor_id="agent-1",
        is_authenticated=True,
        auth_method=AuthMethod.API_KEY,
        tenant_id=None,
        role=None,
        agent_id="agent-1",
        scope_profile_id="sp-1",
    )


# ---------------------------------------------------------------------------
# Mock conn / chunk seed helpers
# ---------------------------------------------------------------------------


def make_chunk_row(
    *,
    document_id: str = "d1",
    version_id: str = "v1",
    node_id: str = "n1",
    source_text: str = "default chunk text",
):
    """`_fetch_accessible_chunk` 가 반환할 row 형식."""
    return {
        "document_id": document_id,
        "version_id": version_id,
        "node_id": node_id,
        "source_text": source_text,
    }


def make_version_row(
    *,
    version_id: str = "v1",
    status: str = "published",
):
    """versions 테이블 row 모방."""
    return {"id": version_id, "status": status}


@pytest.fixture
def chunk_text_fixture():
    """30 노드 시드 - (document_type × node_kind × scope) 다양 조합."""
    seeds = []
    for i in range(30):
        seeds.append(
            {
                "document_id": f"00000000-0000-0000-0000-{i:012d}",
                "version_id": "v1",
                "node_id": f"node-{i}",
                "source_text": f"Node {i} content body — sample text",
                "document_type": ["POLICY", "MANUAL", "RUNBOOK"][i % 3],
                "node_kind": ["paragraph", "heading", "list_item"][i % 3],
                "scope": ["default", "team", "org"][i % 3],
            }
        )
    return seeds
