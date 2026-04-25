"""S3 P1 FG 1-3: users.preferences JSONB 컬럼 추가.

Revision ID: s3_p1_users_preferences
Revises: s3_p1_content_snapshot_backfill
Create Date: 2026-04-24 14:00:00

목적:
  Phase 1 FG 1-3 (뷰 선호 저장/복원) 을 위해 ``users`` 테이블에 ``preferences``
  JSONB 컬럼을 추가한다. 초기 용도는 에디터 뷰 모드(`editor_view_mode`) 이며
  향후 테마/언어 등 사용자 선호가 확장될 수 있도록 open dict 로 둔다.

구조:
  - NOT NULL + DEFAULT '{}' (빈 객체) — 기존 레코드도 기본값 자동 주입
  - 허용 키 엄격화는 애플리케이션(`UserPreferences` pydantic schema) 에서 수행
    (DB 는 free-form JSONB, S1 ② generic + config 원칙)

downgrade:
  컬럼 drop. 선호 데이터는 소실됨 (의도된 롤백 전용 경로).

참조:
  - task1-3.md §2 FG 1-3 Step 3
  - S2-5 `s2_5_users_scope` 동일 패턴
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger("alembic.runtime.migration.s3_p1_users_preferences")


# revision identifiers, used by Alembic
revision = "s3_p1_users_preferences"
down_revision = "s3_p1_content_snapshot_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL JSONB + NOT NULL DEFAULT '{}' — 기존 row 에 자동으로 {} 주입.
    op.execute(
        sa.text(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS preferences JSONB NOT NULL DEFAULT '{}'::jsonb
            """
        )
    )
    logger.info("s3_p1_users_preferences: users.preferences JSONB 컬럼 추가 완료")


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE users DROP COLUMN IF EXISTS preferences"))
    logger.info("s3_p1_users_preferences: users.preferences 컬럼 삭제 (downgrade)")
