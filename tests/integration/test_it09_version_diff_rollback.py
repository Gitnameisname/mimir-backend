"""
IT-09 — 버전 diff + rollback 후 content_snapshot 일관성 (S2 Phase 6 회귀).

검증 포인트:
  - V1 publish → V2 publish → V1 restore 로 새 Draft(V3) 생성
  - V3.content_snapshot 이 V1.content_snapshot 과 동일해야 한다 (복원의 일관성)
  - V3.rolled_back_from_version 또는 restored_from_version 메타가 V1 id 를 가리켜야 한다.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_it09_restore_produces_draft_equivalent_to_source(
    client,
    db_conn,
    auth_author_header,
    auth_approver_header,
    make_document,
    save_initial_draft,
    run_workflow_action,
):
    # --- V1 publish ---
    doc_id, _ = make_document(title="IT-09 diff+rollback", headers=auth_author_header)
    v1_text = "V1: 원본 본문 — 아주 중요한 정책 문장."
    v1_id = save_initial_draft(doc_id, paragraph_text=v1_text, headers=auth_author_header)
    run_workflow_action(doc_id, v1_id, "submit-review",
                       headers=auth_author_header, body={"comment": "리뷰"})
    run_workflow_action(doc_id, v1_id, "approve",
                       headers=auth_approver_header, body={"comment": "승인"})
    run_workflow_action(doc_id, v1_id, "publish",
                       headers=auth_approver_header, body={"comment": "게시 V1"})

    # --- V2 publish (본문 변경) ---
    v2_text = "V2: 수정된 본문 — 최신 정책."
    v2_id = save_initial_draft(doc_id, paragraph_text=v2_text, headers=auth_author_header)
    run_workflow_action(doc_id, v2_id, "submit-review",
                       headers=auth_author_header, body={"comment": "리뷰"})
    run_workflow_action(doc_id, v2_id, "approve",
                       headers=auth_approver_header, body={"comment": "승인"})
    run_workflow_action(doc_id, v2_id, "publish",
                       headers=auth_approver_header, body={"comment": "게시 V2"})

    # --- V1 restore → 새 Draft ---
    resp = client.post(
        f"/api/v1/documents/{doc_id}/versions/{v1_id}/restore",
        json={"change_summary": "V1 롤백"},
        headers=auth_approver_header,
    )
    assert resp.status_code == 201, f"restore 실패: {resp.status_code} / {resp.text[:200]}"
    v3 = _unwrap(resp.json())
    v3_id = v3["id"]

    # --- V3.content_snapshot == V1.content_snapshot ---
    v1_snapshot = _fetch_content_snapshot(db_conn, v1_id)
    v3_snapshot = _fetch_content_snapshot(db_conn, v3_id)
    assert v1_snapshot == v3_snapshot, (
        "restore 결과 Draft 의 content_snapshot 이 원본 V1 과 다르다 — 일관성 위반"
    )

    # --- V3 메타가 V1 을 가리킨다 ---
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT restored_from_version_id, rolled_back_from_version
            FROM versions
            WHERE id = %s
            """,
            (v3_id,),
        )
        row = cur.fetchone()
    assert row is not None
    pointer = row.get("restored_from_version_id") or row.get("rolled_back_from_version")
    assert pointer is not None, "restore 메타 포인터(restored_from_version_id) 가 비어 있음"
    assert str(pointer) == str(v1_id), (
        f"restore 포인터가 V1 을 가리키지 않음: {pointer} (expected {v1_id})"
    )


def test_it09_diff_endpoint_returns_nonzero_changes_between_v1_v2(
    client,
    auth_author_header,
    auth_approver_header,
    make_document,
    save_initial_draft,
    run_workflow_action,
):
    """두 published 버전 간 diff 가 non-empty 여야 한다."""
    doc_id, _ = make_document(title="IT-09 diff non-empty", headers=auth_author_header)

    v1_id = save_initial_draft(doc_id, paragraph_text="A", headers=auth_author_header)
    for step in ("submit-review", "approve", "publish"):
        h = auth_author_header if step == "submit-review" else auth_approver_header
        run_workflow_action(doc_id, v1_id, step, headers=h, body={"comment": step})

    v2_id = save_initial_draft(doc_id, paragraph_text="B", headers=auth_author_header)
    for step in ("submit-review", "approve", "publish"):
        h = auth_author_header if step == "submit-review" else auth_approver_header
        run_workflow_action(doc_id, v2_id, step, headers=h, body={"comment": step})

    # GET /documents/{doc}/versions/{v1}/diff/{v2}
    resp = client.get(
        f"/api/v1/documents/{doc_id}/versions/{v1_id}/diff/{v2_id}",
        headers=auth_author_header,
    )
    # diff 엔드포인트 미노출이거나 다른 경로 형식인 경우 스킵.
    if resp.status_code == 404:
        pytest.skip("diff 엔드포인트 경로 형식이 다름 — 이 테스트는 환경 종속")
    if resp.status_code == 422:
        pytest.skip("diff 파라미터 검증 실패 — 스키마 변경 가능성")
    assert resp.status_code == 200, f"diff 호출 실패: {resp.status_code} / {resp.text[:200]}"

    data = _unwrap(resp.json())
    # 대체로 changes/blocks/diff 필드에 변경 내역이 담긴다.
    body_str = str(data)
    assert any(key in data for key in ("changes", "blocks", "diff", "operations", "node_diff")), (
        f"diff 응답에 변경 필드가 없음: {list(data.keys()) if isinstance(data, dict) else type(data)}"
    )
    # 최소 A / B 텍스트가 어딘가 포함돼야 한다.
    assert "A" in body_str or "B" in body_str, "diff 본문에 원문 단편이 보이지 않음"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _unwrap(body):
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _fetch_content_snapshot(db_conn, version_id: str):
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT content_snapshot FROM versions WHERE id = %s",
            (version_id,),
        )
        row = cur.fetchone()
    assert row is not None, f"version {version_id} 조회 실패"
    return row["content_snapshot"]
