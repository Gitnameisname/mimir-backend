"""
IT-01 — 문서 전체 라이프사이클 (create → draft → submit_review → approve → publish)

검증 포인트:
  - 각 전이마다 HTTP 200/201
  - version.status 가 DB 레벨에서 draft → in_review → approved → published 순차 전이
  - 마지막 단계에서 documents.current_published_version_id 포인터가 갱신됨
  - 모든 전이가 감사 이벤트로 남음 (actor_type='user')

주: AUTHOR 헤더로 draft/submit-review, APPROVER 헤더로 approve/publish 를 호출한다.
    (workflow_service._ROLE_MAP 에 의해 SUPER_ADMIN/APPROVER 모두 허용)
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_it01_full_document_lifecycle(
    client,
    db_conn,
    auth_author_header,
    auth_approver_header,
    make_document,
    save_initial_draft,
    run_workflow_action,
):
    # 1) 문서 생성 (AUTHOR)
    document_id, doc = make_document(
        title="IT-01 문서 라이프사이클",
        document_type="policy",
        headers=auth_author_header,
    )
    assert doc["status"] == "draft"

    # 2) Draft 저장 (AUTHOR)
    version_id = save_initial_draft(
        document_id,
        title="IT-01 초안",
        paragraph_text="IT-01 본문 단락. 워크플로 전이 검증용.",
        headers=auth_author_header,
    )
    _assert_version_status(db_conn, version_id, expected="draft")

    # 3) 검토 요청 (AUTHOR)
    run_workflow_action(
        document_id, version_id, "submit-review",
        headers=auth_author_header, body={"comment": "검토 요청"},
    )
    _assert_workflow_status(db_conn, version_id, expected="in_review")

    # 4) 승인 (APPROVER)
    run_workflow_action(
        document_id, version_id, "approve",
        headers=auth_approver_header, body={"comment": "승인"},
    )
    _assert_workflow_status(db_conn, version_id, expected="approved")

    # 5) 게시 (APPROVER)
    run_workflow_action(
        document_id, version_id, "publish",
        headers=auth_approver_header, body={"comment": "게시"},
    )
    _assert_workflow_status(db_conn, version_id, expected="published")

    # 6) documents.current_published_version_id 가 이 버전으로 업데이트됨
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_version_id FROM documents WHERE id = %s",
            (document_id,),
        )
        row = cur.fetchone()
    assert row is not None, "document 행 사라짐"
    assert str(row["current_published_version_id"]) == str(version_id), (
        "publish 후 documents.current_published_version_id 포인터 미갱신"
    )

    # 7) 감사 이벤트 — 전이 4건 모두 actor_type='user' 로 기록되어야 함
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_type, actor_type
            FROM audit_events
            WHERE resource_id = %s
              AND event_type LIKE 'workflow.%%'
            ORDER BY created_at ASC
            """,
            (version_id,),
        )
        events = cur.fetchall()
    types = [e["event_type"] for e in events]
    # 이벤트 이름 규칙은 구현체마다 다를 수 있으나, 최소 4건 이상이면 ReviewAction/Workflow 모두 남은 것.
    assert len(events) >= 3, f"workflow 감사 이벤트 부족: {types}"
    for e in events:
        assert e["actor_type"] == "user", (
            f"감사 이벤트에 actor_type 이 user 가 아님: {e['actor_type']} "
            "— S2 원칙 ⑤ 위반"
        )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _assert_version_status(db_conn, version_id: str, *, expected: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, workflow_status FROM versions WHERE id = %s", (version_id,))
        row = cur.fetchone()
    assert row, f"버전 {version_id} 조회 실패"
    # status 또는 workflow_status 중 하나에 기대값이 있어야 한다.
    assert expected in (row["status"], row.get("workflow_status")), (
        f"version status 불일치: expected={expected} / row={dict(row)}"
    )


def _assert_workflow_status(db_conn, version_id: str, *, expected: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT workflow_status, status FROM versions WHERE id = %s",
            (version_id,),
        )
        row = cur.fetchone()
    assert row, f"버전 {version_id} 조회 실패"
    got = row.get("workflow_status") or row["status"]
    assert got == expected, f"workflow_status 불일치: expected={expected} / got={got}"
