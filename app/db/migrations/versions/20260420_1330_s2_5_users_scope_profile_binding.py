"""S2-5 users.scope_profile_id binding (+ default admin scope seed)

Revision ID: s2_5_users_scope
Revises:
Create Date: 2026-04-20 13:30:00

배경:
  S2 ⑥ 원칙상 scope_profile_id 가 없는 actor 는 골든셋/평가 Admin API 에서 403
  을 받는다. 2026-04-20 까지는 agents.scope_profile_id 만 관리돼 사람 관리자가
  admin UI 를 사용할 수 없었다. 본 마이그레이션은:

  1) users 테이블에 scope_profile_id 컬럼과 인덱스를 추가한다.
  2) 기본 Scope Profile("Default Admin Scope") + "all" scope 정의를 시드한다.
  3) 모든 SUPER_ADMIN / ORG_ADMIN 사용자의 scope_profile_id 가 NULL 이면
     기본 Profile 로 자동 바인딩한다.

  실행 요건: 이 마이그레이션은 users 테이블의 OWNER 권한을 가진 DB 유저로
  돌려야 한다 (일반 런타임 유저는 ALTER TABLE 권한이 없다).

  실행:
      cd backend
      alembic upgrade head
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic
revision = "s2_5_users_scope"
down_revision = None
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

_ADD_COLUMN_SQL = """
ALTER TABLE users ADD COLUMN IF NOT EXISTS scope_profile_id UUID
    REFERENCES scope_profiles(id) ON DELETE SET NULL;
"""

_ADD_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_users_scope_profile_id ON users(scope_profile_id);
"""

_SEED_PROFILE_SQL = """
INSERT INTO scope_profiles (id, name, description)
SELECT gen_random_uuid(),
       'Default Admin Scope',
       'S2-5 (2026-04-20) 자동 생성: 관리자 기본 Scope Profile'
WHERE NOT EXISTS (
    SELECT 1 FROM scope_profiles WHERE name = 'Default Admin Scope'
);
"""

_SEED_SCOPE_DEF_SQL = """
INSERT INTO scope_definitions (id, scope_profile_id, scope_name, description, acl_filter)
SELECT gen_random_uuid(),
       sp.id,
       'all',
       'S2-5 자동 생성: 제한 없음 (관리자 기본)',
       '{}'::jsonb
FROM scope_profiles sp
WHERE sp.name = 'Default Admin Scope'
  AND NOT EXISTS (
      SELECT 1
      FROM scope_definitions sd
      WHERE sd.scope_profile_id = sp.id AND sd.scope_name = 'all'
  );
"""

_BIND_ADMINS_SQL = """
UPDATE users
SET scope_profile_id = (
        SELECT id FROM scope_profiles WHERE name = 'Default Admin Scope' LIMIT 1
    ),
    updated_at = NOW()
WHERE role_name IN ('SUPER_ADMIN', 'ORG_ADMIN')
  AND scope_profile_id IS NULL;
"""

# 다운그레이드 — 컬럼/인덱스만 제거. 시드된 Scope Profile 은 다른 객체가 참조
# 중일 수 있으므로 자동 삭제하지 않는다 (운영자가 판단).
_DROP_INDEX_SQL = """
DROP INDEX IF EXISTS idx_users_scope_profile_id;
"""

_DROP_COLUMN_SQL = """
ALTER TABLE users DROP COLUMN IF EXISTS scope_profile_id;
"""


def upgrade() -> None:
    op.execute(_ADD_COLUMN_SQL)
    op.execute(_ADD_INDEX_SQL)
    op.execute(_SEED_PROFILE_SQL)
    op.execute(_SEED_SCOPE_DEF_SQL)
    op.execute(_BIND_ADMINS_SQL)


def downgrade() -> None:
    op.execute(_DROP_INDEX_SQL)
    op.execute(_DROP_COLUMN_SQL)
