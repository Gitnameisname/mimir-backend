"""
IT-07 — `EMBEDDING_DIM` 설정값과 `document_chunks.embedding` 컬럼의 vector 차원이 일치해야 한다.
(BUG-04 재발 방지)

**상태: SKIP until FG 0-2**
작업지시서 task0-1 §3 에 따라 FG 0-2 (embedding_dim 검증 Alembic revision) 완료 시점에
활성화된다. 그때까지는 명시적 skip 마커로 수동 확인 유도.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skip(reason="pending FG 0-2 (embedding_dim config↔DB auto-validate revision)")
def test_it07_embedding_dim_matches_document_chunks_vector_dim(db_conn):
    """설정 EMBEDDING_DIM 과 document_chunks.embedding 컬럼 차원이 동일해야 한다."""
    from app.config import settings

    configured_dim = int(
        os.environ.get("EMBEDDING_DIM") or settings.embedding_dim or 0
    )
    assert configured_dim > 0, "EMBEDDING_DIM 설정이 비어 있음"

    with db_conn.cursor() as cur:
        # information_schema.columns 는 vector 타입 차원을 직접 노출하지 않는다.
        # pg_attribute + pg_type 을 조합해 atttypmod 에서 유추한다.
        cur.execute(
            """
            SELECT a.attname,
                   format_type(a.atttypid, a.atttypmod) AS formatted_type,
                   a.atttypmod
            FROM pg_attribute a
            JOIN pg_class c ON a.attrelid = c.oid
            WHERE c.relname = 'document_chunks'
              AND a.attname = 'embedding'
              AND a.attnum > 0
            """
        )
        row = cur.fetchone()

    if row is None:
        pytest.skip(
            "document_chunks.embedding 컬럼이 없음 — pgvector 미탑재 환경이거나 "
            "FG 0-2 완료 후 Alembic 이 컬럼을 추가해야 함."
        )

    # format_type 은 'vector(768)' 형식으로 출력된다.
    formatted = row["formatted_type"]  # e.g. 'vector(768)'
    assert "vector" in formatted, f"embedding 컬럼 타입이 vector 가 아님: {formatted}"

    # 차원 파싱
    import re as _re
    m = _re.search(r"vector\((\d+)\)", formatted)
    assert m, f"vector 차원 파싱 실패: {formatted}"
    db_dim = int(m.group(1))

    assert db_dim == configured_dim, (
        f"EMBEDDING_DIM({configured_dim}) != document_chunks.embedding 차원({db_dim}) — BUG-04"
    )
