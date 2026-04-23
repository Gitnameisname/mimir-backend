"""
IT-03 — 두 사용자가 동시에 같은 버전에 대해 approve 를 호출해도 상태 전이가 **1회만 성공**한다.

race condition 보호:
  - WorkflowService 가 row lock (SELECT ... FOR UPDATE) 또는 expected_current_status
    낙관적 동시성 검사를 수행하는지 검증.
  - 둘 다 동일 expected_current_status="in_review" 로 호출해도, 상태 전이 후 다시 호출하면
    409 (ApiConflictError) 또는 동등한 에러가 반환되어야 한다.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

pytestmark = pytest.mark.integration


def test_it03_concurrent_approve_exactly_one_succeeds(
    client,
    db_conn,
    auth_author_header,
    auth_approver_header,
    make_document,
    save_initial_draft,
    run_workflow_action,
):
    # 준비: submit-review 까지 전이시킨 버전.
    doc_id, _ = make_document(title="IT-03 동시 승인", headers=auth_author_header)
    ver_id = save_initial_draft(doc_id, headers=auth_author_header)
    run_workflow_action(doc_id, ver_id, "submit-review",
                       headers=auth_author_header, body={"comment": "리뷰"})

    # 두 번째 approver 헤더 — actor_id 만 달리 해서 동일 권한으로 동시 요청
    h1 = dict(auth_approver_header)
    h2 = dict(auth_approver_header)
    h2["X-Actor-Id"] = "it-approver-002"

    url = f"/api/v1/documents/{doc_id}/versions/{ver_id}/workflow/approve"
    body = {"comment": "동시 승인", "expected_current_status": "in_review"}

    def _approve(headers: dict) -> int:
        r = client.post(url, json=body, headers=headers)
        return r.status_code

    # TestClient 는 스레드 안전하다고 보장되지 않지만, FastAPI/Starlette 조합 상
    # 요청 단위로 별 객체이므로 실제 경합은 DB 레벨에서 발생한다.
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(_approve, h1), ex.submit(_approve, h2)]
        statuses = sorted(f.result() for f in as_completed(futures))

    # 가능한 조합: (200, 409) / (200, 4xx). 그러나 양쪽 다 200 은 금지 — race 방어 실패.
    assert statuses.count(200) == 1, (
        f"동시 approve 결과가 200 정확히 1건이 아님: {statuses} — race condition 방어 실패"
    )
    # 나머지 하나는 409/422/ 4xx 중 하나여야 한다 (구현체에 따라 차이 가능).
    other = [s for s in statuses if s != 200][0]
    assert 400 <= other < 500, f"나머지 응답이 4xx 가 아님: {other}"

    # DB 상 workflow_status 가 'approved' 로 정착
    with db_conn.cursor() as cur:
        cur.execute("SELECT workflow_status, status FROM versions WHERE id = %s", (ver_id,))
        row = cur.fetchone()
    final = row.get("workflow_status") or row["status"]
    assert final == "approved", f"최종 상태가 approved 가 아님: {final}"
