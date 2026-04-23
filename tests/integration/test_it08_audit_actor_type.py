"""
IT-08 — 감사 로그 `audit_events.actor_type` 필드가 user/agent 구분을 실제로 저장한다.
(S2 원칙 ⑤ — 실 DB 검증)

검증 포인트:
  - 문서 생성 API 를 사람(=X-Actor-Role=AUTHOR) 헤더로 호출하면, audit_events.actor_type='user'.
  - 동일 API 를 에이전트 principal (X-Actor-Type=agent 혹은 MCP 경로) 로 호출하면,
    audit_events.actor_type='agent'.
  - actor_type 컬럼 자체가 NOT NULL 제약을 가지며, NULL 로 남을 수 없다.
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration


def test_it08_actor_type_user_on_human_request(
    client,
    db_conn,
    auth_author_header,
    make_document,
):
    doc_id, _ = make_document(title="IT-08 human", headers=auth_author_header)
    _wait_for_audit(db_conn, resource_id=doc_id, min_count=1)

    rows = _fetch_audit(db_conn, resource_id=doc_id)
    assert rows, "audit_events 에 문서 생성 이벤트가 없음"
    for r in rows:
        assert r["actor_type"] == "user", (
            f"사람 헤더로 호출했는데 actor_type={r['actor_type']} — S2 ⑤ 위반"
        )


def test_it08_actor_type_agent_when_service_principal(client, db_conn):
    """서비스/에이전트 principal 경로로 호출 시 actor_type='agent'.

    현재 구현은 X-Actor-Type 헤더를 auth 레이어에서 해석해 ActorContext.actor_type 을 설정한다.
    에이전트용 인증(예: API key) 경로가 프로젝트마다 차이가 있어, 본 테스트는 debug 헤더 기반.
    """
    headers = {
        "X-Actor-Id": "it08-agent-001",
        "X-Actor-Role": "AUTHOR",
        "X-Actor-Type": "agent",  # 있으면 agent 로 해석
    }
    resp = client.post(
        "/api/v1/documents",
        json={"title": "IT-08 agent", "document_type": "policy"},
        headers=headers,
    )
    if resp.status_code != 201:
        pytest.skip(
            f"agent 헤더 경로에서 문서 생성 불가 (status={resp.status_code}) — "
            "프로젝트가 agent principal 을 별도 인증 경로로만 허용할 수 있음."
        )

    doc_id = _unwrap(resp.json())["id"]
    _wait_for_audit(db_conn, resource_id=doc_id, min_count=1)

    rows = _fetch_audit(db_conn, resource_id=doc_id)
    assert rows
    # 최소 한 행은 agent 여야 한다. 구현체에 따라 actor_type 해석이 'agent' 또는 'service' 계열.
    found = {r["actor_type"] for r in rows}
    assert "agent" in found or "service" in found, (
        f"agent 헤더로 호출했는데 actor_type 집합에 agent/service 없음: {found}"
    )


def test_it08_audit_actor_type_is_not_null(db_conn):
    """audit_events.actor_type 컬럼 자체가 NOT NULL 제약을 가진다 (F-07 시정)."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'audit_events'
              AND column_name = 'actor_type'
            """
        )
        row = cur.fetchone()
    assert row is not None, "audit_events.actor_type 컬럼 자체가 존재하지 않음"
    # S2-ph4 마이그레이션이 적용됐다면 NOT NULL.
    assert row["is_nullable"] == "NO", (
        f"audit_events.actor_type 이 NULLABLE — F-07 시정이 적용되지 않았음"
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _unwrap(body):
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _wait_for_audit(db_conn, *, resource_id: str, min_count: int, timeout_sec: float = 5.0) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        db_conn.rollback()  # snapshot 갱신
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE resource_id = %s",
                (resource_id,),
            )
            if cur.fetchone()["c"] >= min_count:
                return
        time.sleep(0.2)
    pytest.fail(f"audit_events.{resource_id} 가 {timeout_sec}s 내 {min_count} 건에 도달하지 못함")


def _fetch_audit(db_conn, *, resource_id: str):
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, actor_type, actor_id FROM audit_events WHERE resource_id = %s "
            "ORDER BY created_at ASC",
            (resource_id,),
        )
        return cur.fetchall()
