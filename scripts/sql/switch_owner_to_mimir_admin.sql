-- ================================================================
-- Mimir — public 스키마 내 모든 DB 객체 OWNER 를 mimir_admin 으로 일괄 변경
-- 작성: 2026-04-24 (S3 Phase 1 FG 1-3 Alembic ALTER TABLE 권한 해결용)
-- ================================================================
--
-- 배경
-- ----
--   * Phase 1 FG 1-3 의 revision `s3_p1_users_preferences` 는 `ALTER TABLE users
--     ADD COLUMN preferences JSONB` 를 실행한다.
--   * PG 정책상 ALTER TABLE 은 테이블 OWNER 전용이며 `GRANT ALL` 로도 커버되지
--     않는다. 현재 DB 에는 각 테이블 OWNER 가 과거 초기화 유저로 고정되어 있어
--     일반 app 유저는 Alembic DDL 을 실행할 수 없다.
--   * 향후 모든 Alembic migration 을 동일 owner(`mimir_admin`) 로 일관 수행하기
--     위해 **public 스키마 전체 객체 소유권을 mimir_admin 으로 통일** 한다.
--
-- 실행 전제
-- ---------
--   1. pgAdmin 에서 대상 DB(예: `mimir`) 에 **슈퍼유저(postgres)** 로 연결
--   2. `mimir_admin` 역할이 이미 존재. 없다면 아래 §0 먼저 실행.
--   3. 이 스크립트는 **멱등** — 이미 mimir_admin 소유인 객체는 no-op.
--   4. 실 서비스 중단 없이 실행 가능 (OWNER 변경은 즉시 적용, 데이터 미손상).
--
-- 실행 방법 (pgAdmin)
-- -------------------
--   1. Query Tool 열기 (대상 DB 선택, 연결 유저=postgres)
--   2. 본 파일 전체 붙여넣기
--   3. F5 (Execute)
--   4. 맨 마지막 검증 쿼리의 tableowner 컬럼이 모두 `mimir_admin` 인지 확인
--
-- 실행 이후
-- --------
--   * Alembic 은 `mimir_admin` 자격으로 실행 (env.py 의 ALEMBIC_POSTGRES_USER
--     경로). 예:
--       ALEMBIC_POSTGRES_USER=mimir_admin \
--       ALEMBIC_POSTGRES_PASSWORD='<pw>' \
--       alembic upgrade head
--   * 기존 app 유저(`mimir_app` 등) 의 SELECT/INSERT/UPDATE/DELETE 권한은
--     owner 변경으로 영향받지 않음. 필요 시 §5 에서 재확인.
--
-- 롤백
-- ----
--   owner 되돌리기는 반복 실행 가능. `mimir_admin` → 이전 owner 로 동일
--   패턴 실행하면 복원. 단, 데이터 변경 없는 metadata-only 변경이라 롤백
--   자체가 데이터 복구와는 무관.
-- ================================================================


-- ------------------------------------------------------------
-- §0. (필요 시) mimir_admin 역할 생성
-- ------------------------------------------------------------
-- 이미 존재한다면 실행 불필요. CREATE ROLE IF NOT EXISTS 는 PG 표준에 없으므로
-- 존재 확인 DO 블록.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mimir_admin') THEN
    -- 비밀번호는 실제 값으로 바꿔 주세요.
    -- CREATE ROLE mimir_admin WITH LOGIN PASSWORD 'CHANGE_ME' NOSUPERUSER NOCREATEDB NOCREATEROLE;
    RAISE NOTICE 'mimir_admin 역할이 없습니다. 슈퍼유저로 먼저 생성하세요.';
  END IF;
END$$;


-- ------------------------------------------------------------
-- §1. 스키마 자체 소유권
-- ------------------------------------------------------------
ALTER SCHEMA public OWNER TO mimir_admin;


-- ------------------------------------------------------------
-- §2. 모든 테이블 OWNER → mimir_admin
-- ------------------------------------------------------------
DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT tablename
    FROM pg_tables
    WHERE schemaname = 'public'
  LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO mimir_admin', r.tablename);
  END LOOP;
END$$;


-- ------------------------------------------------------------
-- §3. 모든 시퀀스 OWNER → mimir_admin
--   (SERIAL / IDENTITY 뒤에 자동 생성된 시퀀스도 포함)
-- ------------------------------------------------------------
DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT sequence_name
    FROM information_schema.sequences
    WHERE sequence_schema = 'public'
  LOOP
    EXECUTE format('ALTER SEQUENCE public.%I OWNER TO mimir_admin', r.sequence_name);
  END LOOP;
END$$;


-- ------------------------------------------------------------
-- §4. 모든 뷰 OWNER → mimir_admin
-- ------------------------------------------------------------
DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT table_name
    FROM information_schema.views
    WHERE table_schema = 'public'
  LOOP
    EXECUTE format('ALTER VIEW public.%I OWNER TO mimir_admin', r.table_name);
  END LOOP;
END$$;


-- ------------------------------------------------------------
-- §5. 모든 함수/프로시저 OWNER → mimir_admin
--   user-defined function 만 대상 (PG 내장 제외)
-- ------------------------------------------------------------
DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT
      n.nspname || '.' || p.proname || '(' ||
      pg_catalog.pg_get_function_identity_arguments(p.oid) || ')' AS sig
    FROM pg_proc p
    JOIN pg_namespace n ON p.pronamespace = n.oid
    WHERE n.nspname = 'public'
  LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO mimir_admin', r.sig);
  END LOOP;
END$$;


-- ------------------------------------------------------------
-- §6. 타입 (domain / composite) OWNER — 선택
-- ------------------------------------------------------------
DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT t.typname
    FROM pg_type t
    JOIN pg_namespace n ON t.typnamespace = n.oid
    WHERE n.nspname = 'public'
      AND t.typtype IN ('d', 'c', 'e')   -- domain / composite / enum
      -- SERIAL / table-implicit type 등 auto 생성 제외
      AND t.typrelid = 0
  LOOP
    EXECUTE format('ALTER TYPE public.%I OWNER TO mimir_admin', r.typname);
  END LOOP;
END$$;


-- ------------------------------------------------------------
-- §7. (선택) 향후 신규 객체도 자동으로 app 유저에게 기본 권한 부여
--   주의: 아래 블록은 실제 app 유저 이름을 알아야 하므로 기본 주석 처리.
--        필요 시 `mimir_app` 을 실제 유저로 바꿔 주석 해제.
-- ------------------------------------------------------------
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public
--   GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mimir_app;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public
--   GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO mimir_app;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public
--   GRANT EXECUTE ON FUNCTIONS TO mimir_app;


-- ================================================================
-- 검증 쿼리 — 실행 직후 결과 확인 (모두 'mimir_admin' 이어야 정상)
-- ================================================================

-- 7-1. 스키마 owner
SELECT nspname AS schema, pg_get_userbyid(nspowner) AS owner
FROM pg_namespace
WHERE nspname = 'public';

-- 7-2. 테이블 owner (모두 mimir_admin 이어야 함)
SELECT tablename, tableowner
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY tablename;

-- 7-3. 시퀀스 owner
SELECT sequence_name, sequence_schema
FROM information_schema.sequences
WHERE sequence_schema = 'public'
ORDER BY sequence_name;

-- 7-4. 혹시 mimir_admin 이 아닌 객체가 남아있는지 한 번 더 감시
SELECT n.nspname AS schema, c.relname AS object, c.relkind AS kind,
       pg_get_userbyid(c.relowner) AS owner
FROM pg_class c
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname = 'public'
  AND c.relkind IN ('r','v','S','m','p')   -- table / view / sequence / mat view / partitioned table
  AND pg_get_userbyid(c.relowner) <> 'mimir_admin'
ORDER BY c.relname;
-- ↑ 이 쿼리 결과가 **빈 결과(0 rows)** 여야 정상. 결과가 있으면 해당 객체에 대해
--   개별 ALTER 가 필요 (권한 문제일 가능성).
