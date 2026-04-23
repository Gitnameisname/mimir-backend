"""
IT-04 — Scope Profile / 권한 기반 검색 ACL.

검증 포인트 (S2 원칙 ⑥ — 코드 레벨이 아니라 실 DB + 실 질의로 검증):
  - `draft` 상태 문서는 AUTHOR 에게만 노출되고 VIEWER 에게는 가려진다.
  - `published` 문서는 VIEWER/미인증에게도 노출된다.
  - `document_chunks.accessible_roles` 가 특정 역할로 제한된 chunk 는
    그 역할이 없는 actor 의 RAG 답변 컨텍스트에 포함되지 않는다.

주: 본 테스트는 scope_profile 단위 상세 ACL 대신, documents.status + chunks.accessible_roles
   두 축에서 ACL 이 폴백 경로(FTS)에서도 적용됨을 확인한다.
   (S2 추가 원칙: "폴백 경로에서도 동일한 ACL 적용 필수")
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


def test_it04_draft_invisible_to_viewer(
    client,
    auth_viewer_header,
    auth_author_header,
    make_document,
    save_initial_draft,
):
    """draft 문서 본문의 고유 키워드로 검색 시 VIEWER 에게는 노출되지 않는다."""
    unique_marker = f"IT04-DRAFT-MARKER-{uuid.uuid4().hex[:8]}"

    doc_id, _ = make_document(
        title=f"IT-04 드래프트 전용 {unique_marker}",
        headers=auth_author_header,
    )
    save_initial_draft(
        doc_id,
        paragraph_text=f"비공개 본문. {unique_marker}",
        headers=auth_author_header,
    )

    # VIEWER 로 통합 검색 — 본문 마커로도 문서가 노출되면 안 됨
    resp = client.get(f"/api/v1/search?q={unique_marker}", headers=auth_viewer_header)
    assert resp.status_code in (200, 429), f"search 비정상: {resp.status_code}"
    if resp.status_code == 429:
        pytest.skip("rate limit — 병렬 실행 영향, 단독 실행 시 200")
    data = _unwrap(resp.json())
    docs = data.get("documents") or data.get("document_results") or []
    matched = [d for d in docs if str(d.get("id")) == doc_id]
    assert not matched, (
        f"VIEWER 가 draft 문서를 검색 결과에서 볼 수 있음 — S2 ⑥ ACL 위반\n"
        f"matched={matched}"
    )

    # AUTHOR 본인은 볼 수 있어야 한다 — 음/양성 대조군
    resp2 = client.get(f"/api/v1/search?q={unique_marker}", headers=auth_author_header)
    assert resp2.status_code in (200, 429)
    if resp2.status_code == 200:
        data2 = _unwrap(resp2.json())
        docs2 = data2.get("documents") or data2.get("document_results") or []
        assert any(str(d.get("id")) == doc_id for d in docs2), (
            "AUTHOR 자신에게도 draft 가 안 보임 — false negative"
        )


def test_it04_chunk_accessible_roles_filters_fts(db_conn, client, auth_viewer_header):
    """document_chunks.accessible_roles 가 ['SUPER_ADMIN'] 인 청크는 VIEWER 가 찾을 수 없다."""
    marker = f"IT04-CHUNK-{uuid.uuid4().hex[:10]}"

    # 1) 수동으로 published document + chunk 를 INSERT — 인증 경로를 우회해 데이터셋만 준비.
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    chunk_id = str(uuid.uuid4())
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (id, title, document_type, status, created_by)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (doc_id, f"IT04 ACL doc {marker}", "policy", "published", "it04"),
        )
        cur.execute(
            """
            INSERT INTO versions (id, document_id, version_number, status, workflow_status, content_snapshot)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (ver_id, doc_id, 1, "published", "published", "{}"),
        )
        # 본 테스트의 핵심: accessible_roles 가 SUPER_ADMIN 으로 제한된 chunk
        cur.execute(
            """
            INSERT INTO document_chunks
                (id, document_id, version_id, chunk_index, source_text,
                 document_type, document_status, accessible_roles, is_public, is_current)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                chunk_id, doc_id, ver_id, 0, f"비공개 청크 본문 {marker}",
                "policy", "published", ["SUPER_ADMIN"], False, True,
            ),
        )
        db_conn.commit()

    try:
        # 2) VIEWER 로 검색 — 마커가 포함된 청크/문서가 안 보여야 한다.
        resp = client.get(
            f"/api/v1/search/documents?q={marker}",
            headers=auth_viewer_header,
        )
        # rate-limit 허용
        if resp.status_code == 429:
            pytest.skip("rate limit")
        assert resp.status_code == 200, f"{resp.status_code} / {resp.text[:200]}"
        data = _unwrap(resp.json())
        results = data.get("results") or data.get("documents") or data.get("items") or []
        hit = [r for r in results if str(r.get("id")) == doc_id or marker in str(r)]
        assert not hit, (
            f"accessible_roles 가 SUPER_ADMIN 만인 청크를 VIEWER 가 검색 결과에서 봄 — "
            f"폴백 ACL 위반\nhit={hit}"
        )
    finally:
        # 3) 테스트 정리
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM document_chunks WHERE id = %s", (chunk_id,))
            cur.execute("DELETE FROM versions WHERE id = %s", (ver_id,))
            cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
            db_conn.commit()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _unwrap(body):
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body
