"""
IT-06 — Draft 저장 중 일부 트랜잭션이 롤백될 때 `documents` 에 고아 레코드가 남지 않는다.
(BUG-01 재발 방지)

검증 포인트:
  - Draft 저장이 versions + nodes 다수 테이블에 분산되어 있을 때, 중간에 예외가 발생하면
    전체 트랜잭션이 롤백되어야 한다 (부분 커밋 금지).
  - 본 테스트는 서비스 레이어를 직접 호출해 강제로 예외를 일으키고, 이전 상태가 유지됨을
    확인한다.

주: HTTP 엔드포인트로는 부분 실패를 안정적으로 재현하기 어려우므로 서비스 레이어를 이용한다.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


def test_it06_failed_draft_save_leaves_no_orphan_version(
    client,
    db_conn,
    auth_author_header,
    make_document,
):
    """Draft 저장 경로에서 예외가 발생하면, versions 에 draft 레코드가 남지 않아야 한다."""
    doc_id, _ = make_document(title="IT-06 롤백 문서", headers=auth_author_header)

    # 초기 상태 확인 — 아직 draft 없음
    before_count = _count_drafts_for_doc(db_conn, doc_id)
    assert before_count == 0

    # draft_service.save_draft 내부의 어느 한 지점에서 예외가 발생하도록 patch.
    # 후보 1: nodes insert 직전에 raise.
    from app.services import draft_service as _draft

    sentinel = RuntimeError("simulated mid-transaction failure for IT-06")

    # 노드 저장 관련 함수를 모의 — 서비스 구조에 따라 존재하는 메서드를 패치.
    # draft_service 모듈의 save_draft_nodes 를 직접 공격하면 draft 생성 경로가 깨지므로
    # 더 내부의 trigger 지점을 찾는다. 여기선 nodes_repository 를 패치.
    # save_draft 흐름: versions_repository.create → documents_repository.update_version_pointers.
    # 두 번째 단계(update_version_pointers)에서 예외가 나면 같은 트랜잭션의 첫 단계도 롤백되어야 한다.
    with patch(
        "app.repositories.documents_repository.documents_repository.update_version_pointers",
        side_effect=sentinel,
    ):
        resp = client.put(
            f"/api/v1/documents/{doc_id}/draft",
            json={
                "title": "IT-06 실패 draft",
                "content_snapshot": {
                    "type": "document",
                    "children": [{"type": "paragraph", "content": "실패 예정"}],
                },
            },
            headers=auth_author_header,
        )

    # 5xx 가 나와야 한다 (예외 전파). 중요한 것은 그 다음 — DB 상태.
    assert resp.status_code >= 500 or resp.status_code == 422, (
        f"예외가 정상 전파되지 않음: {resp.status_code}"
    )

    after_count = _count_drafts_for_doc(db_conn, doc_id)
    # 실제 서비스 구현이 "draft row 먼저 커밋 후 nodes 저장" 이라면 count 가 1 이 될 수 있다 —
    # 그건 BUG-01 그 자체이므로 이 테스트가 '정확히 잡아야 할 부분'.
    assert after_count == before_count, (
        f"mid-transaction 실패에도 versions 에 draft 레코드가 남음 "
        f"(before={before_count} / after={after_count}) — BUG-01 재발"
    )


def test_it06_document_row_intact_after_rollback(
    db_conn,
    client,
    auth_author_header,
    make_document,
):
    """Draft 저장이 실패해도 부모 document 레코드는 손상되지 않는다."""
    doc_id, doc = make_document(title="IT-06 문서 보존", headers=auth_author_header)
    original_updated_at = _get_document_updated_at(db_conn, doc_id)

    from unittest.mock import patch as _patch

    with _patch(
        "app.repositories.documents_repository.documents_repository.update_version_pointers",
        side_effect=RuntimeError("boom"),
    ):
        client.put(
            f"/api/v1/documents/{doc_id}/draft",
            json={
                "title": "보존 테스트",
                "content_snapshot": {
                    "type": "document",
                    "children": [{"type": "paragraph", "content": "x"}],
                },
            },
            headers=auth_author_header,
        )

    with db_conn.cursor() as cur:
        cur.execute("SELECT id, status FROM documents WHERE id = %s", (doc_id,))
        row = cur.fetchone()
    assert row is not None, "document row 가 예외 중 삭제됨 — 심각한 무결성 위반"
    assert row["status"] in ("draft", "published"), (
        f"문서 상태가 손상됨: {row['status']}"
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _count_drafts_for_doc(db_conn, document_id: str) -> int:
    db_conn.rollback()  # 이전 에러 상태 정리
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM versions WHERE document_id = %s AND status = 'draft'",
            (document_id,),
        )
        return cur.fetchone()["c"]


def _get_document_updated_at(db_conn, document_id: str):
    with db_conn.cursor() as cur:
        cur.execute("SELECT updated_at FROM documents WHERE id = %s", (document_id,))
        return cur.fetchone()["updated_at"]
