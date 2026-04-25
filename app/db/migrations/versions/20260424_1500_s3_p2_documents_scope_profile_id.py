"""S3 P2 FG 2-0: documents.scope_profile_id 컬럼 + backfill

Revision ID: s3_p2_documents_scope_profile
Revises: s3_p1_users_preferences
Create Date: 2026-04-24 15:00:00

배경
----

Phase 2 의 절대 규칙은 **"컬렉션·폴더·태그·Saved View 는 순수 뷰 레이어이며
ACL 은 Scope Profile 단독이 결정"** 이다. 그런데 현재 `documents` 테이블에는
`scope_profile_id` 컬럼이 없어서 ACL 필터링이 documents 조회 경로에
적용되어 있지 않다 (Phase 2 Pre-flight 실측 §2, §6).

이 상태에서 컬렉션/폴더/태그/백링크가 documents 를 참조하면 **"뷰 ≠ 권한"
규칙을 검증할 수 없다**. 본 마이그레이션이 Phase 2 작업의 **필수 선행 (FG 2-0)**
이다.

작업
----

1. ``documents.scope_profile_id UUID NULL`` 컬럼 + FK + 인덱스 추가
2. Backfill: 기존 레코드를 ``created_by`` 의 ``users.scope_profile_id`` 로 채움
3. Fallback: created_by 가 NULL 이거나 user 매핑 실패 시 "Default Admin Scope"
   (S2-5 시드 프로파일) 로 채움 — 최소한 제한 없음(all) scope 가 붙어서
   기존 동작과 호환 유지
4. ``SCHEMA_MIGRATION_DRY_RUN=1`` 이면 UPDATE 실행을 건너뛰고 로그만 남김

실행 요건
---------

``documents`` 테이블 OWNER 권한을 가진 DB 유저 (mimir_admin) 로 실행.

    cd backend
    ALEMBIC_POSTGRES_USER=mimir_admin \\
    ALEMBIC_POSTGRES_PASSWORD='<pw>' \\
    alembic upgrade head

참조
----

- docs/개발문서/S3/phase2/작업지시서/task2-1.md §0
- docs/개발문서/S3/phase2/산출물/Pre-flight_실측.md §6
- S2-5 동일 패턴: 20260420_1330_s2_5_users_scope_profile_binding.py
"""
from __future__ import annotations

import logging
import os

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger("alembic.runtime.migration.s3_p2_documents_scope_profile")


# revision identifiers, used by Alembic
revision = "s3_p2_documents_scope_profile"
down_revision = "s3_p1_users_preferences"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

_ADD_COLUMN_SQL = """
ALTER TABLE documents ADD COLUMN IF NOT EXISTS scope_profile_id UUID
    REFERENCES scope_profiles(id) ON DELETE SET NULL;
"""

_ADD_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_documents_scope_profile_id
    ON documents(scope_profile_id)
    WHERE scope_profile_id IS NOT NULL;
"""

# Backfill: created_by (users.id VARCHAR 로 저장돼 있을 수 있음 — users 테이블의
# id 컬럼 타입과 맞춰 cast 한다. psql 에서 UUID → TEXT 비교는 자동 캐스팅 되므로
# 안전하다.
_BACKFILL_FROM_USER_SQL = """
UPDATE documents d
SET scope_profile_id = u.scope_profile_id,
    updated_at = NOW()
FROM users u
WHERE d.scope_profile_id IS NULL
  AND d.created_by IS NOT NULL
  AND (u.id::text = d.created_by OR u.email = d.created_by)
  AND u.scope_profile_id IS NOT NULL;
"""

# Fallback: 남은 NULL 은 Default Admin Scope 로 묶는다. 이는 "기본 제한 없음"
# 이므로 기존 동작과 동등하게 동작한다 (all scope). 운영자가 필요 시 세분화
# 프로파일로 재할당할 수 있다.
_BACKFILL_FALLBACK_SQL = """
UPDATE documents
SET scope_profile_id = (
        SELECT id FROM scope_profiles WHERE name = 'Default Admin Scope' LIMIT 1
    ),
    updated_at = NOW()
WHERE scope_profile_id IS NULL;
"""

_COUNT_REMAINING_SQL = """
SELECT COUNT(*) AS remaining FROM documents WHERE scope_profile_id IS NULL;
"""

_COUNT_TOTAL_SQL = "SELECT COUNT(*) AS total FROM documents;"


_DROP_INDEX_SQL = "DROP INDEX IF EXISTS idx_documents_scope_profile_id;"

_DROP_COLUMN_SQL = "ALTER TABLE documents DROP COLUMN IF EXISTS scope_profile_id;"


def _is_dry_run() -> bool:
    """환경변수 SCHEMA_MIGRATION_DRY_RUN=1 일 때 UPDATE 실행 건너뜀."""
    return os.environ.get("SCHEMA_MIGRATION_DRY_RUN", "").strip() in {"1", "true", "TRUE", "yes"}


def upgrade() -> None:
    # 1) 컬럼 + 인덱스 — 항상 실행 (ADD COLUMN IF NOT EXISTS 는 멱등)
    op.execute(_ADD_COLUMN_SQL)
    op.execute(_ADD_INDEX_SQL)

    bind = op.get_bind()
    total = bind.execute(sa.text(_COUNT_TOTAL_SQL)).scalar() or 0
    if total == 0:
        logger.info("documents 테이블이 비어 있음 — backfill 건너뜀")
        return

    if _is_dry_run():
        logger.warning(
            "SCHEMA_MIGRATION_DRY_RUN=1 — documents %d rows backfill 건너뜀. "
            "실제 실행 시 변수 없이 재실행하라.",
            total,
        )
        return

    # 2) Backfill from users
    bind.execute(sa.text(_BACKFILL_FROM_USER_SQL))
    mid_remaining = bind.execute(sa.text(_COUNT_REMAINING_SQL)).scalar() or 0
    logger.info(
        "documents.scope_profile_id backfill-from-users 완료 — "
        "total=%d, remaining_null=%d", total, mid_remaining,
    )

    # 3) Fallback
    if mid_remaining:
        bind.execute(sa.text(_BACKFILL_FALLBACK_SQL))
        final_remaining = bind.execute(sa.text(_COUNT_REMAINING_SQL)).scalar() or 0
        logger.info(
            "documents.scope_profile_id fallback(Default Admin Scope) 완료 — "
            "remaining_null=%d", final_remaining,
        )
        if final_remaining:
            logger.warning(
                "documents.scope_profile_id 가 여전히 NULL 인 row %d 건 남음. "
                "Default Admin Scope 프로파일이 없을 가능성 — 수동 확인 필요.",
                final_remaining,
            )


def downgrade() -> None:
    # 다운그레이드: 컬럼/인덱스만 제거. 기존 데이터는 소실된다 (의도된 롤백 전용).
    op.execute(_DROP_INDEX_SQL)
    op.execute(_DROP_COLUMN_SQL)
