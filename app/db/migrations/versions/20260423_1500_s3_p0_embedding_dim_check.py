"""S3 P0 FG 0-2: EMBEDDING_DIM ↔ document_chunks.embedding 차원 일치 검증 가드.

Revision ID: s3_p0_embedding_dim_check
Revises: p7_2_c_uppercase_doc_type
Create Date: 2026-04-23 15:00:00

목적:
  S2 종결 시점 탐지된 BUG-04 (EMBEDDING_DIM=768 vs document_chunks.embedding VECTOR(1536))
  의 재발을 migration 시점에 단호하게 차단한다.

정책 (유연한 검증 모드):
  - `document_chunks.embedding` 컬럼이 **없으면 pass** (현재 Milvus 중심 아키텍처와 정합).
  - 컬럼이 있고 차원이 config 와 **일치하면 pass**.
  - 컬럼이 있고 차원이 **다르면 RuntimeError → revision 실패**.

스키마 변경 없음:
  본 revision 은 **검증만** 수행한다. 차원 변경이 필요하면 별도 revision 에서
  column alter + 재벡터화 배치를 함께 다룬다 (FG 0-2 범위 밖).

downgrade:
  no-op. 검증 revision 이라 되돌릴 대상이 없다.

운영 실패 시 대응:
  `docs/개발문서/S3/phase0/산출물/FG0-2_운영가이드.md` §3 참고.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from alembic import op

# backend/ 를 sys.path 에 보장 (alembic env.py 가 이미 넣지만 이중 안전)
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.db.embedding_dim_check import (  # noqa: E402
    EmbeddingDimMismatchError,
    check_embedding_dim,
)

logger = logging.getLogger("alembic.runtime.migration.s3_p0_embedding_dim_check")


# revision identifiers, used by Alembic
revision = "s3_p0_embedding_dim_check"
down_revision = "p7_2_c_uppercase_doc_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """EMBEDDING_DIM ↔ DB 차원 일치를 검증한다. 불일치 시 revision 실패."""
    bind = op.get_bind()
    # SQLAlchemy Connection 에서 raw psycopg2 connection 을 꺼내 헬퍼에 전달.
    # (프로젝트 컨벤션: raw SQL 만 사용 — ORM 없음)
    raw_conn = bind.connection

    result = check_embedding_dim(raw_conn, check_milvus=False)

    if result.ok:
        if result.column_present:
            logger.info(
                "[FG0-2] EMBEDDING_DIM 검증 pass — config=%d / db=%s (type=%s)",
                result.config_dim, result.db_dim, result.column_type,
            )
        else:
            logger.info(
                "[FG0-2] EMBEDDING_DIM 검증 pass — document_chunks.embedding 컬럼 부재 "
                "(현재 Milvus 중심 아키텍처와 정합). config=%d.",
                result.config_dim,
            )
        return

    # 불일치 — 명확한 메시지로 revision 실패.
    msg = (
        f"[FG0-2] EMBEDDING_DIM 불일치 감지 — {result.reason}\n"
        "BUG-04 재발 방지를 위해 본 revision 은 중단된다.\n"
        "조치 절차: docs/개발문서/S3/phase0/산출물/FG0-2_운영가이드.md §3 참고."
    )
    logger.error(msg)
    raise EmbeddingDimMismatchError(msg)


def downgrade() -> None:
    """no-op — 검증 revision 이라 되돌릴 대상이 없다."""
    return None
