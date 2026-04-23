"""
IT-07 — `EMBEDDING_DIM` 설정값과 `document_chunks.embedding` 차원 일관성 + FG0-2 revision 가드.
(BUG-04 재발 방지)

FG 0-2 (2026-04-23) 완료로 활성화. 검증 정책:
  - `document_chunks.embedding` 컬럼 부재 → pass (현재 Milvus 중심 아키텍처 기대값)
  - 컬럼 존재 + 차원 일치 → pass
  - 컬럼 존재 + 차원 불일치 → fail (BUG-04 재발)

추가로, FG 0-2 Alembic revision (`s3_p0_embedding_dim_check`) 이 head 에 포함됨을 확인한다.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_it07_embedding_dim_consistency(db_conn):
    """pgvector 컬럼 있으면 EMBEDDING_DIM 과 차원 일치, 없으면 pass."""
    from app.db.embedding_dim_check import check_embedding_dim

    result = check_embedding_dim(db_conn, check_milvus=False)
    assert result.ok, (
        f"EMBEDDING_DIM 정합성 위반 (BUG-04 재발): {result.reason} "
        f"(config={result.config_dim}, db={result.db_dim}, column_type={result.column_type})"
    )


def test_it07_fg02_revision_in_head(db_conn):
    """FG 0-2 revision 이 alembic_version 헤드에 포함되어 있어야 한다."""
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        rows = cur.fetchall()
    heads = {r["version_num"] for r in rows}
    # 멀티헤드일 가능성은 낮지만 방어적으로 set 비교.
    assert "s3_p0_embedding_dim_check" in heads, (
        f"FG 0-2 revision 이 migration head 에 없음: {heads}. "
        "`cd backend && alembic upgrade head` 를 먼저 수행하세요."
    )


def test_it07_healthcheck_includes_embedding_dim_block(client):
    """/system/health 응답에 embedding_dim 블록이 포함된다 (FG 0-2 §4.2)."""
    resp = client.get("/api/v1/system/health")
    assert resp.status_code == 200
    body = resp.json()
    data = body.get("data", body)
    assert "embedding_dim" in data, (
        f"health 응답에 embedding_dim 서브체크 누락: keys={list(data.keys())}"
    )
    block = data["embedding_dim"]
    # 최소 필드 계약
    for key in ("config", "match", "column_present"):
        assert key in block, f"embedding_dim 에 {key} 누락: {block}"
