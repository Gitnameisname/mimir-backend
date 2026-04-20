-- ============================================================================
-- [DEPRECATED 2026-04-20] 이 파일은 Alembic 마이그레이션으로 대체되었다.
-- ============================================================================
--
-- 정식 경로:
--     backend/app/db/migrations/versions/20260420_1330_s2_5_users_scope_profile_binding.py
--
-- 실행:
--     cd backend
--     alembic upgrade head   (users 테이블 OWNER 권한을 가진 DB 유저로 실행)
--
-- 이 .sql 파일은 Alembic 사용 불가한 환경(예: psql 만 쓸 수 있는 운영 접근 경로)
-- 에서의 비상 수동 실행용으로만 남겨둔다. 내용은 alembic upgrade 가 수행하는
-- SQL 과 동등하며 idempotent 하다.
-- ============================================================================

BEGIN;

ALTER TABLE users ADD COLUMN IF NOT EXISTS scope_profile_id UUID
    REFERENCES scope_profiles(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_users_scope_profile_id ON users(scope_profile_id);

INSERT INTO scope_profiles (id, name, description)
SELECT gen_random_uuid(),
       'Default Admin Scope',
       'S2-5 (2026-04-20) 자동 생성: 관리자 기본 Scope Profile'
WHERE NOT EXISTS (
    SELECT 1 FROM scope_profiles WHERE name = 'Default Admin Scope'
);

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

UPDATE users
SET scope_profile_id = (
        SELECT id FROM scope_profiles WHERE name = 'Default Admin Scope' LIMIT 1
    ),
    updated_at = NOW()
WHERE role_name IN ('SUPER_ADMIN', 'ORG_ADMIN')
  AND scope_profile_id IS NULL;

SELECT u.id,
       u.email,
       u.role_name,
       sp.name AS scope_profile
FROM users u
LEFT JOIN scope_profiles sp ON sp.id = u.scope_profile_id
WHERE u.role_name IN ('SUPER_ADMIN', 'ORG_ADMIN')
ORDER BY u.role_name, u.email;

COMMIT;
