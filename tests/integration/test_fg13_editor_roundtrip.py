"""IT-FG13 — 에디터 왕복 + node_id 안정성 integration smoke.

Phase 1 FG 1-3 Step 7.

시나리오:
  1) 문서 생성 → PUT /draft (ProseMirror doc) 로 초기 저장
  2) save_draft 가 content_snapshot 저장 + rebuild_nodes_from_snapshot 호출 →
     nodes 테이블이 doc 과 정합인지 DB 쿼리로 확인
  3) Draft 재저장 (같은 node_id 유지 + content 수정) → nodes 테이블의 node_id
     집합이 동일 유지, content 는 변경 검출
  4) /api/v1/account/preferences 에 editor_view_mode 저장/조회

요구: Phase 0 FG 0-1 의 testcontainers 기반 CI (PostgreSQL + Valkey + pgvector).
      로컬은 `pytest -m integration` 로 마커 선택 실행.
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 헬퍼 — doc 빌더
# ---------------------------------------------------------------------------


def _pm_doc(paragraphs: list[tuple[str, str]]) -> dict:
    """[(node_id, text), ...] → ProseMirror doc."""
    return {
        "type": "doc",
        "schema_version": 1,
        "content": [
            {
                "type": "paragraph",
                "attrs": {"node_id": nid},
                "content": [{"type": "text", "text": txt}] if txt else [],
            }
            for nid, txt in paragraphs
        ],
    }


def _save_draft(client, headers, document_id: str, doc: dict, title: str = "FG1-3 에디터 테스트"):
    body = {
        "title": title,
        "summary": "fg1-3 통합 smoke",
        "change_summary": "fg1-3",
        "content_snapshot": doc,
    }
    resp = client.put(f"/api/v1/documents/{document_id}/draft", json=body, headers=headers)
    assert resp.status_code == 200, f"save_draft 실패: {resp.status_code} / {resp.text[:300]}"
    return resp.json()["data"]["id"]


def _fetch_nodes(db_conn, version_id: str) -> list[dict]:
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, parent_id, node_type, order_index, title, content
            FROM nodes
            WHERE version_id = %s
            ORDER BY order_index ASC, id ASC
            """,
            (version_id,),
        )
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Test 1 — content_snapshot 저장 후 nodes 파생 동기화 정합
# ---------------------------------------------------------------------------


def test_fg13_save_draft_syncs_nodes_from_snapshot(
    client,
    db_conn,
    auth_author_header,
    make_document,
):
    document_id, _ = make_document(
        title="FG1-3 에디터 왕복", document_type="policy", headers=auth_author_header,
    )

    node_id_1 = "aaaaaaaa-0000-4111-8111-111111111111"
    node_id_2 = "bbbbbbbb-0000-4111-8111-111111111111"
    doc = _pm_doc([
        (node_id_1, "첫 단락"),
        (node_id_2, "둘째 단락"),
    ])
    version_id = _save_draft(client, auth_author_header, document_id, doc)

    rows = _fetch_nodes(db_conn, version_id)
    assert len(rows) == 2
    node_ids = {str(r["id"]) for r in rows}
    assert node_ids == {node_id_1, node_id_2}
    contents = [r["content"] for r in rows]
    assert "첫 단락" in contents and "둘째 단락" in contents


# ---------------------------------------------------------------------------
# Test 2 — 재저장 시 node_id 유지 + content 변경 검출
# ---------------------------------------------------------------------------


def test_fg13_resave_preserves_node_ids_and_detects_edit(
    client,
    db_conn,
    auth_author_header,
    make_document,
):
    document_id, _ = make_document(
        title="FG1-3 재저장", document_type="policy", headers=auth_author_header,
    )

    nid = "cccccccc-0000-4111-8111-111111111111"
    doc_v1 = _pm_doc([(nid, "원본 내용")])
    version_id_v1 = _save_draft(client, auth_author_header, document_id, doc_v1)

    rows_v1 = _fetch_nodes(db_conn, version_id_v1)
    assert len(rows_v1) == 1
    assert rows_v1[0]["content"] == "원본 내용"

    # 동일 node_id 로 content 만 수정
    doc_v2 = _pm_doc([(nid, "편집된 내용")])
    version_id_v2 = _save_draft(client, auth_author_header, document_id, doc_v2)

    # 같은 Draft 을 수정했으므로 version_id 동일해야 함
    assert version_id_v1 == version_id_v2

    rows_v2 = _fetch_nodes(db_conn, version_id_v2)
    assert len(rows_v2) == 1
    assert str(rows_v2[0]["id"]) == nid  # id 유지
    assert rows_v2[0]["content"] == "편집된 내용"  # content 변경 반영


# ---------------------------------------------------------------------------
# Test 3 — /account/preferences GET/PATCH 왕복
# ---------------------------------------------------------------------------


def test_fg13_account_preferences_roundtrip(
    client,
    auth_author_header,
):
    # 1) GET — 초기 상태는 빈 dict (Alembic default)
    resp = client.get("/api/v1/account/preferences", headers=auth_author_header)
    assert resp.status_code == 200, resp.text[:300]
    data = resp.json()["data"]
    # editor_view_mode 미설정 시 None 또는 키 없음
    assert data.get("editor_view_mode") in (None, "block", "flow")

    # 2) PATCH — flow 로 설정
    resp = client.patch(
        "/api/v1/account/preferences",
        headers=auth_author_header,
        json={"editor_view_mode": "flow"},
    )
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["data"]["editor_view_mode"] == "flow"

    # 3) GET — 반영 확인
    resp = client.get("/api/v1/account/preferences", headers=auth_author_header)
    assert resp.status_code == 200
    assert resp.json()["data"]["editor_view_mode"] == "flow"

    # 4) PATCH — null 로 제거
    resp = client.patch(
        "/api/v1/account/preferences",
        headers=auth_author_header,
        json={"editor_view_mode": None},
    )
    assert resp.status_code == 200
    # null 제거 후 다시 조회
    resp = client.get("/api/v1/account/preferences", headers=auth_author_header)
    assert resp.json()["data"].get("editor_view_mode") in (None, False)
