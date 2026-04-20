"""
Database connection management (psycopg2 기반).

psycopg2.pool.ThreadedConnectionPool을 사용해 동기 FastAPI endpoint에서
안전하게 DB 연결을 재사용한다.

사용법:
    from app.db import get_db

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ...")
"""

import logging
from contextlib import contextmanager
from typing import Generator

import psycopg2
import psycopg2.extras
import psycopg2.pool

from app.config import settings

logger = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None

# documents 테이블 DDL
_DOCUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title       VARCHAR(500) NOT NULL,
    document_type VARCHAR(100) NOT NULL,
    status      VARCHAR(50) NOT NULL DEFAULT 'draft',
    metadata    JSONB NOT NULL DEFAULT '{}',
    summary     TEXT,
    created_by  VARCHAR(255),
    updated_by  VARCHAR(255),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_documents_status
    ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_document_type
    ON documents(document_type);
CREATE INDEX IF NOT EXISTS idx_documents_created_at
    ON documents(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_updated_at
    ON documents(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_title
    ON documents(title);
"""

# Phase 4: documents 테이블 버전 포인터 컬럼 추가 마이그레이션
_DOCUMENTS_MIGRATION_DDL = """
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS current_draft_version_id UUID REFERENCES versions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS current_published_version_id UUID REFERENCES versions(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_documents_current_draft
    ON documents(current_draft_version_id)
    WHERE current_draft_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_documents_current_published
    ON documents(current_published_version_id)
    WHERE current_published_version_id IS NOT NULL;
"""


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        logger.info("DB connection pool initialized")
    return _pool


@contextmanager
def get_db() -> Generator[psycopg2.extensions.connection, None, None]:
    """DB 연결 컨텍스트 매니저.

    연결 풀에서 연결을 가져와 사용 후 반환한다.
    예외 발생 시 rollback, 정상 종료 시 commit.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# versions 테이블 DDL
_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_number  INTEGER NOT NULL,
    label           VARCHAR(200),
    status          VARCHAR(50) NOT NULL DEFAULT 'draft',
    change_summary  TEXT,
    source          VARCHAR(50) NOT NULL DEFAULT 'manual',
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_by      VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (document_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_versions_document_id
    ON versions(document_id);
CREATE INDEX IF NOT EXISTS idx_versions_created_at
    ON versions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_versions_version_number
    ON versions(document_id, version_number DESC);
"""

# Phase 4: versions 테이블 확장 컬럼 추가 마이그레이션
_VERSIONS_MIGRATION_DDL = """
ALTER TABLE versions
    ADD COLUMN IF NOT EXISTS parent_version_id UUID REFERENCES versions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS restored_from_version_id UUID REFERENCES versions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS title_snapshot VARCHAR(500),
    ADD COLUMN IF NOT EXISTS summary_snapshot TEXT,
    ADD COLUMN IF NOT EXISTS metadata_snapshot JSONB,
    ADD COLUMN IF NOT EXISTS content_snapshot JSONB,
    ADD COLUMN IF NOT EXISTS published_by VARCHAR(255),
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_versions_status
    ON versions(document_id, status);
CREATE INDEX IF NOT EXISTS idx_versions_restored_from
    ON versions(restored_from_version_id)
    WHERE restored_from_version_id IS NOT NULL;
"""

# nodes 테이블 DDL
_NODES_DDL = """
CREATE TABLE IF NOT EXISTS nodes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id  UUID NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    parent_id   UUID REFERENCES nodes(id) ON DELETE SET NULL,
    node_type   VARCHAR(100) NOT NULL DEFAULT 'paragraph',
    order_index INTEGER NOT NULL DEFAULT 0,
    title       VARCHAR(500),
    content     TEXT,
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_nodes_version_id
    ON nodes(version_id);
CREATE INDEX IF NOT EXISTS idx_nodes_parent_id
    ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_order
    ON nodes(version_id, order_index ASC);
"""

# audit_events 테이블 DDL (Phase 4)
_AUDIT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS audit_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type      VARCHAR(100) NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_user_id   VARCHAR(255),
    actor_role      VARCHAR(100),
    document_id     UUID,
    version_id      UUID,
    target_version_id UUID,
    previous_state  VARCHAR(100),
    new_state       VARCHAR(100),
    action_result   VARCHAR(50) NOT NULL DEFAULT 'success',
    reason          VARCHAR(500),
    request_id      VARCHAR(255)
);

CREATE INDEX IF NOT EXISTS idx_audit_events_document_id
    ON audit_events(document_id)
    WHERE document_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_events_actor
    ON audit_events(actor_user_id)
    WHERE actor_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_events_event_type
    ON audit_events(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_events_occurred_at
    ON audit_events(occurred_at DESC);
"""

# ---------------------------------------------------------------------------
# Phase 5: Workflow 관련 테이블 DDL
# ---------------------------------------------------------------------------

# versions 테이블 workflow_status 컬럼 추가 마이그레이션
_VERSIONS_WORKFLOW_MIGRATION_DDL = """
ALTER TABLE versions
    ADD COLUMN IF NOT EXISTS workflow_status VARCHAR(50);

-- 기존 rows: status 값을 workflow_status로 복사 (NULL인 경우에만)
UPDATE versions
SET workflow_status = status
WHERE workflow_status IS NULL;

CREATE INDEX IF NOT EXISTS idx_versions_workflow_status
    ON versions(document_id, workflow_status);
"""

# review_actions 테이블 DDL
_REVIEW_ACTIONS_DDL = """
CREATE TABLE IF NOT EXISTS review_actions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_id      UUID NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    action_type     VARCHAR(50) NOT NULL,
    from_status     VARCHAR(50) NOT NULL,
    to_status       VARCHAR(50) NOT NULL,
    actor_id        VARCHAR(255),
    actor_role      VARCHAR(100),
    comment         TEXT,
    reason          TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_review_actions_document_id
    ON review_actions(document_id);
CREATE INDEX IF NOT EXISTS idx_review_actions_version_id
    ON review_actions(version_id);
CREATE INDEX IF NOT EXISTS idx_review_actions_created_at
    ON review_actions(created_at DESC);
"""

# review_actions 테이블 Phase 5 마이그레이션 (actor_role, metadata 컬럼 추가)
_REVIEW_ACTIONS_MIGRATION_DDL = """
ALTER TABLE review_actions
    ADD COLUMN IF NOT EXISTS actor_role VARCHAR(100),
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}';
"""

# workflow_history 테이블 DDL
_WORKFLOW_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS workflow_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_id      UUID NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    from_status     VARCHAR(50) NOT NULL,
    to_status       VARCHAR(50) NOT NULL,
    action          VARCHAR(50) NOT NULL,
    actor_id        VARCHAR(255),
    actor_role      VARCHAR(100),
    comment         TEXT,
    reason          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_workflow_history_document_id
    ON workflow_history(document_id);
CREATE INDEX IF NOT EXISTS idx_workflow_history_version_id
    ON workflow_history(version_id);
CREATE INDEX IF NOT EXISTS idx_workflow_history_created_at
    ON workflow_history(created_at DESC);
"""

# workflow_history 테이블 Phase 5 마이그레이션 (actor_role 컬럼 추가)
_WORKFLOW_HISTORY_MIGRATION_DDL = """
ALTER TABLE workflow_history
    ADD COLUMN IF NOT EXISTS actor_role VARCHAR(100);
"""

# change_logs 테이블 DDL
_CHANGE_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS change_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_id      UUID REFERENCES versions(id) ON DELETE SET NULL,
    change_type     VARCHAR(100) NOT NULL,
    reason          TEXT,
    actor_id        VARCHAR(255),
    actor_role      VARCHAR(100),
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_change_logs_document_id
    ON change_logs(document_id);
CREATE INDEX IF NOT EXISTS idx_change_logs_created_at
    ON change_logs(created_at DESC);
"""

# change_logs 테이블 Phase 5 마이그레이션 (actor_role 컬럼 추가)
_CHANGE_LOGS_MIGRATION_DDL = """
ALTER TABLE change_logs
    ADD COLUMN IF NOT EXISTS actor_role VARCHAR(100);
"""

# ---------------------------------------------------------------------------
# Phase 7: Admin 관련 테이블 DDL
# ---------------------------------------------------------------------------

_ORGANIZATIONS_DDL = """
CREATE TABLE IF NOT EXISTS organizations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL,
    description TEXT,
    status      VARCHAR(50) NOT NULL DEFAULT 'ACTIVE',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_organizations_status ON organizations(status);
"""

_ROLES_DDL = """
CREATE TABLE IF NOT EXISTS roles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    is_system   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO roles (name, description, is_system) VALUES
    ('SUPER_ADMIN', '플랫폼 전체 관리 권한', TRUE),
    ('ORG_ADMIN', '조직 범위 관리 권한', TRUE),
    ('AUTHOR', '문서 작성 권한', TRUE),
    ('REVIEWER', '문서 검토 권한', TRUE),
    ('APPROVER', '문서 승인 권한', TRUE),
    ('VIEWER', '문서 조회 권한', TRUE)
ON CONFLICT (name) DO NOTHING;
"""

_USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           VARCHAR(255) NOT NULL UNIQUE,
    display_name    VARCHAR(255) NOT NULL,
    status          VARCHAR(50) NOT NULL DEFAULT 'ACTIVE',
    role_name       VARCHAR(100) NOT NULL DEFAULT 'VIEWER',
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC);
"""

_USER_ORG_ROLES_DDL = """
CREATE TABLE IF NOT EXISTS user_org_roles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    role_name       VARCHAR(100) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, org_id, role_name)
);
CREATE INDEX IF NOT EXISTS idx_user_org_roles_user ON user_org_roles(user_id);
CREATE INDEX IF NOT EXISTS idx_user_org_roles_org ON user_org_roles(org_id);
"""

_DOCUMENT_TYPES_DDL = """
CREATE TABLE IF NOT EXISTS document_types (
    type_code       VARCHAR(100) PRIMARY KEY,
    display_name    VARCHAR(255) NOT NULL,
    description     TEXT,
    status          VARCHAR(50) NOT NULL DEFAULT 'ACTIVE',
    schema_fields   JSONB NOT NULL DEFAULT '[]',
    plugin_config   JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO document_types (type_code, display_name, description, schema_fields, plugin_config) VALUES
    ('POLICY', '정책 문서', '조직의 규정 및 정책 문서',
     '[{"name":"title","type":"string","required":true},{"name":"author","type":"string","required":true},{"name":"effective_date","type":"date","required":true},{"name":"version","type":"string","required":false,"default":"1.0"},{"name":"department","type":"string","required":true},{"name":"tags","type":"array","required":false,"default":"[]"}]',
     '{"editor":"richtext-editor-v2","renderer":"policy-renderer-v1","default_workflow":"policy-approval-flow","chunking":"section-based"}'),
    ('MANUAL', '업무 매뉴얼', '업무 절차 및 운영 매뉴얼',
     '[{"name":"title","type":"string","required":true},{"name":"author","type":"string","required":true},{"name":"version","type":"string","required":false,"default":"1.0"},{"name":"category","type":"string","required":false}]',
     '{"editor":"richtext-editor-v2","renderer":"default-renderer-v1","default_workflow":"standard-approval-flow","chunking":"section-based"}'),
    ('REPORT', '보고서', '분석 및 현황 보고서',
     '[{"name":"title","type":"string","required":true},{"name":"author","type":"string","required":true},{"name":"period","type":"string","required":false}]',
     '{"editor":"richtext-editor-v2","renderer":"default-renderer-v1","default_workflow":"report-review-flow","chunking":"paragraph-based"}')
ON CONFLICT (type_code) DO NOTHING;
"""

_BACKGROUND_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS background_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type        VARCHAR(100) NOT NULL,
    resource_type   VARCHAR(100),
    resource_id     VARCHAR(255),
    resource_name   VARCHAR(500),
    status          VARCHAR(50) NOT NULL DEFAULT 'PENDING',
    progress        INTEGER DEFAULT 0,
    requester_id    VARCHAR(255),
    requester_name  VARCHAR(255),
    error_code      VARCHAR(100),
    error_message   TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}',
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON background_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_job_type ON background_jobs(job_type);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON background_jobs(created_at DESC);
"""

_API_KEYS_DDL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    description     TEXT,
    key_prefix      VARCHAR(20) NOT NULL,
    key_hash        VARCHAR(255) NOT NULL,
    scope           VARCHAR(100) NOT NULL DEFAULT 'READ_ONLY',
    status          VARCHAR(50) NOT NULL DEFAULT 'ACTIVE',
    issuer_id       VARCHAR(255),
    issuer_name     VARCHAR(255),
    last_used_at    TIMESTAMPTZ,
    last_used_ip    VARCHAR(50),
    use_count       INTEGER NOT NULL DEFAULT 0,
    expires_at      TIMESTAMPTZ,
    revoked_reason  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_api_keys_status ON api_keys(status);
"""

# ---------------------------------------------------------------------------
# Phase 10: document_chunks 테이블 (벡터는 Milvus로 분리, embedding 컬럼 없음)
# ---------------------------------------------------------------------------

_DOCUMENT_CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS document_chunks (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id         UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_id          UUID NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    node_id             UUID REFERENCES nodes(id) ON DELETE SET NULL,
    chunk_index         INTEGER NOT NULL,
    source_text         TEXT NOT NULL,
    embedding_model     VARCHAR(100),
    token_count         INTEGER,
    node_path           TEXT[] NOT NULL DEFAULT '{}',
    document_type       VARCHAR(100) NOT NULL,
    document_status     VARCHAR(50) NOT NULL,
    accessible_roles    TEXT[] NOT NULL DEFAULT '{}',
    accessible_user_ids TEXT[] NOT NULL DEFAULT '{}',
    accessible_org_ids  TEXT[] NOT NULL DEFAULT '{}',
    is_public           BOOLEAN NOT NULL DEFAULT FALSE,
    is_current          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id
    ON document_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_version_id
    ON document_chunks(version_id);
CREATE INDEX IF NOT EXISTS idx_chunks_is_current
    ON document_chunks(is_current)
    WHERE is_current = TRUE;
CREATE INDEX IF NOT EXISTS idx_chunks_document_type
    ON document_chunks(document_type);
"""

# embedding_token_usage 테이블: 임베딩 API 비용 추적
_EMBEDDING_TOKEN_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS embedding_token_usage (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID REFERENCES background_jobs(id) ON DELETE SET NULL,
    document_id     UUID REFERENCES documents(id) ON DELETE SET NULL,
    model           VARCHAR(100) NOT NULL,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_token_usage_created_at
    ON embedding_token_usage(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_token_usage_document_id
    ON embedding_token_usage(document_id)
    WHERE document_id IS NOT NULL;
"""

# ---------------------------------------------------------------------------
# Phase 8: Full-Text Search 마이그레이션
# ---------------------------------------------------------------------------

_FTS_MIGRATION_DDL = """
-- documents tsvector 컬럼 추가
ALTER TABLE documents ADD COLUMN IF NOT EXISTS search_vector tsvector;

-- documents 기존 데이터 인덱싱
UPDATE documents
SET search_vector =
    setweight(to_tsvector('simple', COALESCE(title, '')), 'A') ||
    setweight(to_tsvector('simple', COALESCE(summary, '')), 'C')
WHERE search_vector IS NULL;

-- documents GIN 인덱스
CREATE INDEX IF NOT EXISTS idx_documents_search_vector
    ON documents USING GIN(search_vector);

-- documents 자동 갱신 트리거 함수
CREATE OR REPLACE FUNCTION update_document_search_vector()
RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('simple', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('simple', COALESCE(NEW.summary, '')), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_document_search_vector ON documents;
CREATE TRIGGER trg_document_search_vector
    BEFORE INSERT OR UPDATE OF title, summary ON documents
    FOR EACH ROW EXECUTE FUNCTION update_document_search_vector();

-- versions tsvector 컬럼 추가
ALTER TABLE versions ADD COLUMN IF NOT EXISTS search_vector tsvector;

UPDATE versions
SET search_vector =
    setweight(to_tsvector('simple', COALESCE(title_snapshot, '')), 'A') ||
    setweight(to_tsvector('simple', COALESCE(summary_snapshot, '')), 'B') ||
    setweight(to_tsvector('simple', COALESCE(change_summary, '')), 'C')
WHERE search_vector IS NULL;

CREATE INDEX IF NOT EXISTS idx_versions_search_vector
    ON versions USING GIN(search_vector);

CREATE OR REPLACE FUNCTION update_version_search_vector()
RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('simple', COALESCE(NEW.title_snapshot, '')), 'A') ||
        setweight(to_tsvector('simple', COALESCE(NEW.summary_snapshot, '')), 'B') ||
        setweight(to_tsvector('simple', COALESCE(NEW.change_summary, '')), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_version_search_vector ON versions;
CREATE TRIGGER trg_version_search_vector
    BEFORE INSERT OR UPDATE OF title_snapshot, summary_snapshot, change_summary ON versions
    FOR EACH ROW EXECUTE FUNCTION update_version_search_vector();

-- nodes tsvector 컬럼 추가
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS search_vector tsvector;

UPDATE nodes
SET search_vector =
    setweight(to_tsvector('simple', COALESCE(title, '')), 'A') ||
    setweight(to_tsvector('simple', COALESCE(content, '')), 'B')
WHERE search_vector IS NULL;

CREATE INDEX IF NOT EXISTS idx_nodes_search_vector
    ON nodes USING GIN(search_vector);

CREATE OR REPLACE FUNCTION update_node_search_vector()
RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('simple', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('simple', COALESCE(NEW.content, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_node_search_vector ON nodes;
CREATE TRIGGER trg_node_search_vector
    BEFORE INSERT OR UPDATE OF title, content ON nodes
    FOR EACH ROW EXECUTE FUNCTION update_node_search_vector();
"""

# search_index_stats 뷰 — Admin 인덱싱 현황 조회용
_SEARCH_INDEX_STATS_DDL = """
CREATE OR REPLACE VIEW search_index_stats AS
SELECT
    'documents' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(*) FILTER (WHERE search_vector IS NOT NULL) AS indexed_rows,
    COUNT(*) FILTER (WHERE search_vector IS NULL) AS unindexed_rows
FROM documents
UNION ALL
SELECT
    'versions',
    COUNT(*),
    COUNT(*) FILTER (WHERE search_vector IS NOT NULL),
    COUNT(*) FILTER (WHERE search_vector IS NULL)
FROM versions
UNION ALL
SELECT
    'nodes',
    COUNT(*),
    COUNT(*) FILTER (WHERE search_vector IS NOT NULL),
    COUNT(*) FILTER (WHERE search_vector IS NULL)
FROM nodes;
"""

# idempotency_records 테이블 DDL
_IDEMPOTENCY_DDL = """
CREATE TABLE IF NOT EXISTS idempotency_records (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key      VARCHAR(512) NOT NULL,
    actor_id             VARCHAR(255),
    resource_action      VARCHAR(200) NOT NULL,
    request_fingerprint  VARCHAR(128) NOT NULL,
    status               VARCHAR(50) NOT NULL DEFAULT 'in_progress',
    response_status_code INTEGER,
    response_body        JSONB,
    resource_id          VARCHAR(255),
    request_id           VARCHAR(255),
    trace_id             VARCHAR(255),
    tenant_id            VARCHAR(255),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at           TIMESTAMPTZ,
    UNIQUE (idempotency_key, actor_id, resource_action)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_key_actor_action
    ON idempotency_records(idempotency_key, actor_id, resource_action);
CREATE INDEX IF NOT EXISTS idx_idempotency_created_at
    ON idempotency_records(created_at DESC);
"""


# ---------------------------------------------------------------------------
# Phase 14: users 테이블 인증 컬럼 마이그레이션
# ---------------------------------------------------------------------------
_USERS_AUTH_MIGRATION_DDL = """
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash      VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider       VARCHAR(50) DEFAULT 'local';
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified      BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at   TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_count  INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until        TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url          VARCHAR(500);
-- Phase 14-17: 아이디(username) 기반 로그인 지원
ALTER TABLE users ADD COLUMN IF NOT EXISTS username            VARCHAR(50);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_unique
    ON users(LOWER(username)) WHERE username IS NOT NULL;
"""


# ---------------------------------------------------------------------------
# S2-5 (2026-04-20): users.scope_profile_id 스키마 변경은 Alembic 으로 이관.
# ---------------------------------------------------------------------------
# 런타임 DB 유저는 users 테이블 OWNER 가 아니어서 init_db() 안에서 ALTER TABLE
# 이 권한 부족으로 skip 된다. 따라서 본 스키마 변경과 관련 시드는 Alembic 으로
# 옮겼다. 실행 경로:
#     backend/app/db/migrations/versions/20260420_1330_s2_5_users_scope_profile_binding.py
#     cd backend && alembic upgrade head   (OWNER 권한 DB 유저)
# 이곳에 DDL 상수를 두지 않는 이유는 "한 곳에서만 스키마 변경을 선언" 하기 위함
# (S1 ② generic+config 원칙과 동일 정신의 single source of truth).


# ---------------------------------------------------------------------------
# Phase 14: refresh_tokens 테이블
# ---------------------------------------------------------------------------
_REFRESH_TOKENS_DDL = """
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  VARCHAR(255) NOT NULL UNIQUE,
    family_id   UUID NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked     BOOLEAN DEFAULT FALSE,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT now(),
    ip_address  VARCHAR(50),
    user_agent  TEXT
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user   ON refresh_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_family ON refresh_tokens(family_id);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_hash   ON refresh_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_expires ON refresh_tokens(expires_at);
"""


# ---------------------------------------------------------------------------
# Phase 14-4: oauth_accounts 테이블
# ---------------------------------------------------------------------------
_OAUTH_ACCOUNTS_DDL = """
CREATE TABLE IF NOT EXISTS oauth_accounts (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider         VARCHAR(50) NOT NULL,
    provider_uid     VARCHAR(255) NOT NULL,
    provider_email   VARCHAR(255),
    provider_name    VARCHAR(255),
    avatar_url       VARCHAR(500),
    access_token     TEXT,
    refresh_token    TEXT,
    token_expires_at TIMESTAMPTZ,
    raw_profile      JSONB DEFAULT '{}',
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE (provider, provider_uid)
);

CREATE INDEX IF NOT EXISTS idx_oauth_accounts_user ON oauth_accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_oauth_accounts_provider ON oauth_accounts(provider, provider_uid);
CREATE INDEX IF NOT EXISTS idx_oauth_accounts_email ON oauth_accounts(provider, provider_email);
"""


# ---------------------------------------------------------------------------
# Phase 14-11: system_settings 테이블
# ---------------------------------------------------------------------------
# Phase 14-13: 알림 관리 — alert_rules + alert_history
_ALERT_RULES_DDL = """
CREATE TABLE IF NOT EXISTS alert_rules (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(255) NOT NULL,
    description  TEXT,
    metric_name  VARCHAR(255) NOT NULL,
    condition    JSONB NOT NULL,
    severity     VARCHAR(50) NOT NULL,
    channels     JSONB NOT NULL DEFAULT '[]'::jsonb,
    channel_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_by   UUID REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_alert_rules_enabled_metric
    ON alert_rules(enabled, metric_name);
"""

_ALERT_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS alert_history (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id           UUID NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    triggered_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ,
    acknowledged_at   TIMESTAMPTZ,
    acknowledged_by   UUID REFERENCES users(id),
    status            VARCHAR(50) NOT NULL,
    metric_value      NUMERIC,
    message           TEXT,
    notified_channels JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_alert_history_rule ON alert_history(rule_id);
CREATE INDEX IF NOT EXISTS idx_alert_history_status
    ON alert_history(status, triggered_at DESC);

-- 동일 규칙의 활성(firing) 이력은 최대 1건 — 중복 발생 방지
CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_history_one_firing_per_rule
    ON alert_history(rule_id) WHERE status = 'firing';
"""

# ---------------------------------------------------------------------------
# Phase 14-14: 배치 작업 스케줄 — job_schedules (실행 기록은 background_jobs 재사용)
_JOB_SCHEDULES_DDL = """
CREATE TABLE IF NOT EXISTS job_schedules (
    id                   VARCHAR(100) PRIMARY KEY,
    name                 VARCHAR(255) NOT NULL,
    description          TEXT,
    schedule             VARCHAR(100),
    enabled              BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at          TIMESTAMPTZ,
    last_run_duration_ms INTEGER,
    last_run_result      VARCHAR(50),
    last_run_id          UUID REFERENCES background_jobs(id) ON DELETE SET NULL,
    next_run_at          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_job_schedules_enabled ON job_schedules(enabled);
"""

# 표준 4 작업 시드 (멱등)
_JOB_SCHEDULES_SEED_DDL = """
INSERT INTO job_schedules (id, name, description, schedule) VALUES
  ('reindex_all',    '전체 검색 인덱스 재구축',  'FTS + 벡터 인덱스를 전량 재구축합니다.', '0 2 * * *'),
  ('vector_sync',    '벡터 동기화',              '누락된 청크에 대해 임베딩을 생성합니다.', '0 * * * *'),
  ('audit_cleanup',  '감사 로그 정리',            '보관 기간 경과 감사 로그를 정리합니다.',   '0 4 * * 1'),
  ('token_cleanup',  '만료 토큰 정리',            '만료된 refresh_tokens 를 제거합니다.',     '0 3 * * *')
ON CONFLICT (id) DO NOTHING;
"""


_SYSTEM_SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS system_settings (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category    VARCHAR(100) NOT NULL,
    key         VARCHAR(255) NOT NULL,
    value       JSONB NOT NULL,
    description TEXT,
    updated_by  UUID REFERENCES users(id),
    updated_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (category, key)
);

CREATE INDEX IF NOT EXISTS idx_system_settings_category ON system_settings(category);
"""

# 초기 시드 — INSERT ON CONFLICT DO NOTHING 으로 멱등 보장
_SYSTEM_SETTINGS_SEED_DDL = """
INSERT INTO system_settings (category, key, value, description) VALUES
  ('auth', 'session_timeout_minutes', '120'::jsonb, '세션 타임아웃 (분)'),
  ('auth', 'max_login_attempts', '5'::jsonb, '최대 로그인 시도 횟수'),
  ('auth', 'lockout_duration_minutes', '15'::jsonb, '로그인 잠금 시간 (분)'),
  ('auth', 'password_min_length', '8'::jsonb, '최소 비밀번호 길이'),
  ('auth', 'auto_create_gitlab_users', 'true'::jsonb, 'GitLab 최초 로그인 시 자동 계정 생성'),
  ('system', 'platform_name', '"Mimir"'::jsonb, '플랫폼 표시 이름'),
  ('system', 'default_user_role', '"VIEWER"'::jsonb, '신규 사용자 기본 역할'),
  ('system', 'maintenance_mode', 'false'::jsonb, '유지보수 모드 활성화'),
  ('notification', 'email_enabled', 'true'::jsonb, '이메일 알림 활성화'),
  ('notification', 'webhook_enabled', 'false'::jsonb, '웹훅 알림 활성화'),
  ('auth', 'email_verification_required', 'true'::jsonb, '이메일 인증 필수 여부 (false이면 가입 후 관리자 승인 필요)'),
  ('security', 'api_rate_limit_per_minute', '100'::jsonb, 'API 분당 요청 제한'),
  ('security', 'require_email_verification', 'false'::jsonb, '이메일 인증 필수 여부')
ON CONFLICT (category, key) DO NOTHING;
"""


_DOCUMENT_TYPES_RETRIEVAL_CONFIG_MIGRATION_DDL = """
ALTER TABLE document_types
    ADD COLUMN IF NOT EXISTS retrieval_config JSONB NOT NULL DEFAULT
        '{"default_retriever":"fts","retriever_params":{},"default_reranker":null,"reranker_params":{}}'::jsonb;
"""


# ---------------------------------------------------------------------------
# Phase 3 (S2): Conversation 도메인 — conversations / turns / messages
# ---------------------------------------------------------------------------

_CONVERSATIONS_DDL = """
CREATE TABLE IF NOT EXISTS conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        UUID NOT NULL,
    organization_id UUID NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    title           VARCHAR(256) NOT NULL,
    status          VARCHAR(32) NOT NULL DEFAULT 'active',
    metadata        JSONB NOT NULL DEFAULT '{}',
    retention_days  INTEGER NOT NULL DEFAULT 90,
    expires_at      TIMESTAMPTZ,
    deleted_at      TIMESTAMPTZ,
    access_level    VARCHAR(32) NOT NULL DEFAULT 'private',
    CONSTRAINT check_conversation_status
        CHECK (status IN ('active', 'archived', 'expired', 'deleted')),
    CONSTRAINT check_conversation_access_level
        CHECK (access_level IN ('private', 'organization', 'public'))
);

CREATE INDEX IF NOT EXISTS ix_conversations_owner_created
    ON conversations(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_conversations_organization_status
    ON conversations(organization_id, status);
CREATE INDEX IF NOT EXISTS ix_conversations_expires_at
    ON conversations(expires_at)
    WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_conversations_deleted_at
    ON conversations(deleted_at)
    WHERE deleted_at IS NULL;
"""

_TURNS_DDL = """
CREATE TABLE IF NOT EXISTS turns (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    turn_number         INTEGER NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_message        TEXT NOT NULL,
    assistant_response  TEXT NOT NULL,
    retrieval_metadata  JSONB NOT NULL DEFAULT '{}',
    CONSTRAINT uq_turns_conversation_turn_number
        UNIQUE (conversation_id, turn_number)
);

CREATE INDEX IF NOT EXISTS ix_turns_conversation_id
    ON turns(conversation_id);
CREATE INDEX IF NOT EXISTS ix_turns_created_at
    ON turns(created_at DESC);
"""

_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id     UUID NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    role        VARCHAR(32) NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata    JSONB NOT NULL DEFAULT '{}',
    CONSTRAINT check_message_role
        CHECK (role IN ('user', 'assistant', 'system'))
);

CREATE INDEX IF NOT EXISTS ix_messages_turn_id
    ON messages(turn_id);
"""

# FTS 트리거 — conversations.title 을 search_vector 로 색인
_CONVERSATIONS_FTS_DDL = """
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS search_vector tsvector;

UPDATE conversations
SET search_vector = to_tsvector('simple', COALESCE(title, ''))
WHERE search_vector IS NULL;

CREATE INDEX IF NOT EXISTS ix_conversations_search_vector
    ON conversations USING GIN(search_vector);

CREATE OR REPLACE FUNCTION update_conversation_search_vector()
RETURNS trigger AS $$
BEGIN
    NEW.search_vector := to_tsvector('simple', COALESCE(NEW.title, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_conversation_search_vector ON conversations;
CREATE TRIGGER trg_conversation_search_vector
    BEFORE INSERT OR UPDATE OF title ON conversations
    FOR EACH ROW EXECUTE FUNCTION update_conversation_search_vector();
"""

# PH3-CARRY-002: title_tsv — 제목 전용 FTS 컬럼 (task7-10)
# search_vector 는 레거시 유지, title_tsv 는 ts_rank 기반 정밀 검색용
_CONVERSATIONS_TITLE_TSV_DDL = """
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS title_tsv tsvector;

UPDATE conversations
SET title_tsv = to_tsvector('simple', COALESCE(title, ''))
WHERE title_tsv IS NULL;

CREATE INDEX IF NOT EXISTS idx_conv_title_fts
    ON conversations USING GIN(title_tsv);

CREATE OR REPLACE FUNCTION update_conv_title_tsv()
RETURNS trigger AS $$
BEGIN
    NEW.title_tsv := to_tsvector('simple', COALESCE(NEW.title, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_conv_title_tsv ON conversations;
CREATE TRIGGER trg_conv_title_tsv
    BEFORE INSERT OR UPDATE OF title ON conversations
    FOR EACH ROW EXECUTE FUNCTION update_conv_title_tsv();
"""

# ---------------------------------------------------------------------------
# Phase 3 (S2): audit_events 에 actor_type 컬럼 추가
#   S2 원칙 ⑥: 감사 로그에 actor_type (user|agent) 필드 필수
# ---------------------------------------------------------------------------

_AUDIT_EVENTS_ACTOR_TYPE_MIGRATION_DDL = """
ALTER TABLE audit_events
    ADD COLUMN IF NOT EXISTS actor_type VARCHAR(32);
"""

# ---------------------------------------------------------------------------
# Phase 3 (S2): retention_policies — 조직별 보존 정책
# ---------------------------------------------------------------------------

_RETENTION_POLICIES_DDL = """
CREATE TABLE IF NOT EXISTS retention_policies (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id         UUID NOT NULL UNIQUE,
    default_retention_days  INTEGER NOT NULL DEFAULT 90,
    max_retention_days      INTEGER NOT NULL DEFAULT 365,
    auto_expire_enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    batch_schedule          VARCHAR(32) NOT NULL DEFAULT '0 0 * * *',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT check_retention_days_positive
        CHECK (default_retention_days >= 1 AND max_retention_days >= 1),
    CONSTRAINT check_retention_days_order
        CHECK (default_retention_days <= max_retention_days)
);

CREATE INDEX IF NOT EXISTS ix_retention_policies_organization_id
    ON retention_policies(organization_id);
"""

# ---------------------------------------------------------------------------
# Phase 4 (S2): scope_profiles / scope_definitions / agents 테이블
# ---------------------------------------------------------------------------

_SCOPE_PROFILES_DDL = """
CREATE TABLE IF NOT EXISTS scope_profiles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    description     TEXT,
    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scope_profiles_organization_id
    ON scope_profiles(organization_id);
CREATE INDEX IF NOT EXISTS idx_scope_profiles_name
    ON scope_profiles(name);
"""

_SCOPE_DEFINITIONS_DDL = """
CREATE TABLE IF NOT EXISTS scope_definitions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_profile_id UUID NOT NULL REFERENCES scope_profiles(id) ON DELETE CASCADE,
    scope_name       VARCHAR(100) NOT NULL,
    description      TEXT,
    acl_filter       JSONB NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (scope_profile_id, scope_name)
);

CREATE INDEX IF NOT EXISTS idx_scope_definitions_profile_id
    ON scope_definitions(scope_profile_id);
"""

_AGENTS_DDL = """
CREATE TABLE IF NOT EXISTS agents (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             VARCHAR(255) NOT NULL,
    description      TEXT,
    organization_id  UUID REFERENCES organizations(id) ON DELETE SET NULL,
    scope_profile_id UUID REFERENCES scope_profiles(id) ON DELETE SET NULL,
    is_disabled      BOOLEAN NOT NULL DEFAULT FALSE,
    disabled_at      TIMESTAMPTZ,
    disabled_reason  TEXT,
    metadata         JSONB NOT NULL DEFAULT '{}',
    created_by       UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agents_organization_id
    ON agents(organization_id);
CREATE INDEX IF NOT EXISTS idx_agents_scope_profile_id
    ON agents(scope_profile_id);
CREATE INDEX IF NOT EXISTS idx_agents_is_disabled
    ON agents(is_disabled);
"""

_API_KEYS_AGENT_MIGRATION_DDL = """
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS scope_profile_id UUID REFERENCES scope_profiles(id) ON DELETE SET NULL;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS principal_type VARCHAR(50) NOT NULL DEFAULT 'user';
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS agent_id UUID REFERENCES agents(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_api_keys_agent_id
    ON api_keys(agent_id) WHERE agent_id IS NOT NULL;
"""

# ---------------------------------------------------------------------------
# S2 Phase 5 (FG5.1): 에이전트 제안 — 상태 확장 + 신규 테이블
# ---------------------------------------------------------------------------

# audit_events 에 acting_on_behalf_of 컬럼 추가 (S2 원칙 ⑥)
_AUDIT_EVENTS_AGENT_MIGRATION_DDL = """
ALTER TABLE audit_events
    ADD COLUMN IF NOT EXISTS acting_on_behalf_of VARCHAR(255);
"""

# transition_proposals 테이블 — 에이전트의 워크플로 전이 제안
_TRANSITION_PROPOSALS_DDL = """
CREATE TABLE IF NOT EXISTS transition_proposals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_id      UUID NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    current_state   VARCHAR(50) NOT NULL,
    proposed_state  VARCHAR(50) NOT NULL,
    status          VARCHAR(50) NOT NULL DEFAULT 'pending_approval',
    reason          TEXT,
    approver_notes  TEXT,
    reviewed_by     VARCHAR(255),
    review_notes    TEXT,
    review_timestamp TIMESTAMPTZ,
    mcp_task_id     VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transition_proposals_agent_id
    ON transition_proposals(agent_id);
CREATE INDEX IF NOT EXISTS idx_transition_proposals_document_id
    ON transition_proposals(document_id);
CREATE INDEX IF NOT EXISTS idx_transition_proposals_status
    ON transition_proposals(status);
CREATE INDEX IF NOT EXISTS idx_transition_proposals_created_at
    ON transition_proposals(created_at DESC);
"""

# agent_proposals 테이블 — 에이전트 제안 통합 큐 (FG5.2)
# Draft 제안(proposal_type=draft)과 전이 제안(proposal_type=transition)을 단일 큐로 관리
_AGENT_PROPOSALS_DDL = """
CREATE TABLE IF NOT EXISTS agent_proposals (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id         UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    proposal_type    VARCHAR(50) NOT NULL,
    reference_id     UUID NOT NULL,
    status           VARCHAR(50) NOT NULL DEFAULT 'pending',
    reviewed_by      VARCHAR(255),
    review_notes     TEXT,
    review_timestamp TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT check_proposal_type
        CHECK (proposal_type IN ('draft', 'transition')),
    CONSTRAINT check_proposal_status
        CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn'))
);

CREATE INDEX IF NOT EXISTS idx_agent_proposals_agent_id
    ON agent_proposals(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_proposals_status
    ON agent_proposals(status);
CREATE INDEX IF NOT EXISTS idx_agent_proposals_proposal_type
    ON agent_proposals(proposal_type);
CREATE INDEX IF NOT EXISTS idx_agent_proposals_created_at
    ON agent_proposals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_proposals_reference_id
    ON agent_proposals(reference_id);
"""

# llm_providers 테이블 — S2 Phase 6 (FG6.1) 관리자 지정 LLM/Embedding 프로바이더
_LLM_PROVIDERS_DDL = """
CREATE TABLE IF NOT EXISTS llm_providers (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             VARCHAR(255) NOT NULL,
    type             VARCHAR(32)  NOT NULL CHECK (type IN ('llm', 'embedding')),
    model_name       VARCHAR(255) NOT NULL,
    api_base_url     VARCHAR(1024),
    embed_endpoint   VARCHAR(512),
    api_key          TEXT,
    description      VARCHAR(500),
    is_default       BOOLEAN NOT NULL DEFAULT FALSE,
    status           VARCHAR(32)  NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'inactive', 'error')),
    last_tested_at   TIMESTAMPTZ,
    last_test_result VARCHAR(32),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_providers_type ON llm_providers(type);
CREATE INDEX IF NOT EXISTS idx_llm_providers_is_default ON llm_providers(is_default);
"""

# prompts / prompt_versions 테이블 — S2 Phase 6 (FG6.1) 관리자 프롬프트 버전 관리
_PROMPTS_DDL = """
CREATE TABLE IF NOT EXISTS prompts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name              VARCHAR(255) NOT NULL UNIQUE,
    description       VARCHAR(500),
    active_version_id UUID,
    ab_test_config    JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_id      UUID NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    version_number INT  NOT NULL,
    content        TEXT NOT NULL,
    created_by     VARCHAR(255),
    is_active      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (prompt_id, version_number)
);

ALTER TABLE prompts
    ADD COLUMN IF NOT EXISTS active_version_id UUID REFERENCES prompt_versions(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_prompt_versions_prompt_id ON prompt_versions(prompt_id);
"""

_PROMPTS_SEED_DDL = """
DO $$
DECLARE
    p_id  UUID;
    pv_id UUID;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM prompts WHERE name = 'rag_system_prompt') THEN
        INSERT INTO prompts (name, description)
        VALUES (
            'rag_system_prompt',
            'RAG 질의응답 시스템 기본 프롬프트. 검색된 문서 컨텍스트를 바탕으로 정확하고 출처 명시된 답변을 생성합니다.'
        )
        RETURNING id INTO p_id;

        INSERT INTO prompt_versions (prompt_id, version_number, content, created_by, is_active)
        VALUES (
            p_id,
            1,
            $PROMPT$당신은 Mimir 지식 관리 시스템의 AI 어시스턴트입니다.
아래에 제공된 [참고 문서]를 바탕으로 사용자의 질문에 답변하세요.

## 지침
- 반드시 제공된 참고 문서의 내용만을 근거로 답변하세요.
- 답변에 사용한 정보의 출처(문서 제목 또는 섹션)를 명시하세요.
- 참고 문서에서 답을 찾을 수 없는 경우 "제공된 문서에서 해당 정보를 찾을 수 없습니다"라고 솔직하게 말하세요.
- 추측이나 외부 지식으로 답변을 보완하지 마세요.
- 한국어로 질문하면 한국어로, 영어로 질문하면 영어로 답변하세요.

## 참고 문서
{context}

## 사용자 질문
{question}$PROMPT$,
            'system',
            TRUE
        )
        RETURNING id INTO pv_id;

        UPDATE prompts SET active_version_id = pv_id WHERE id = p_id;
    END IF;
END;
$$;
"""

# mcp_tasks 테이블 — MCP Tasks 비동기 승인 플로우 (experimental)
# MCP Tasks 스펙이 unstable하므로 자체 DB 기반 fallback 구현
_MCP_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS mcp_tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           VARCHAR(500) NOT NULL,
    description     TEXT,
    task_type       VARCHAR(100) NOT NULL DEFAULT 'agent_proposal_review',
    state           VARCHAR(50) NOT NULL DEFAULT 'input_required',
    progress        INTEGER NOT NULL DEFAULT 0,
    reference_type  VARCHAR(50),
    reference_id    UUID,
    agent_id        UUID REFERENCES agents(id) ON DELETE SET NULL,
    assignee_id     VARCHAR(255),
    result_payload  JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mcp_tasks_state
    ON mcp_tasks(state);
CREATE INDEX IF NOT EXISTS idx_mcp_tasks_reference
    ON mcp_tasks(reference_type, reference_id)
    WHERE reference_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mcp_tasks_agent_id
    ON mcp_tasks(agent_id)
    WHERE agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mcp_tasks_created_at
    ON mcp_tasks(created_at DESC);
"""

# ---------------------------------------------------------------------------
# Phase 7 (S2): Golden Set 도메인 테이블 — FG7.1
# ---------------------------------------------------------------------------

_GOLDEN_SETS_DDL = """
CREATE TABLE IF NOT EXISTS golden_sets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id        UUID NOT NULL,
    name            VARCHAR(200) NOT NULL,
    description     TEXT,
    domain          VARCHAR(50)  NOT NULL DEFAULT 'custom',
    status          VARCHAR(20)  NOT NULL DEFAULT 'draft',
    version         INTEGER      NOT NULL DEFAULT 1,
    extra_metadata  JSONB        NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_by      VARCHAR(255) NOT NULL,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_by      VARCHAR(255),
    deleted_at      TIMESTAMPTZ,
    is_deleted      BOOLEAN      NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_golden_sets_scope_domain
    ON golden_sets(scope_id, domain);
CREATE INDEX IF NOT EXISTS idx_golden_sets_scope_status
    ON golden_sets(scope_id, status);
CREATE INDEX IF NOT EXISTS idx_golden_sets_created_by
    ON golden_sets(created_by);
CREATE INDEX IF NOT EXISTS idx_golden_sets_is_deleted
    ON golden_sets(is_deleted);

CREATE TABLE IF NOT EXISTS golden_items (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    golden_set_id        UUID NOT NULL REFERENCES golden_sets(id) ON DELETE CASCADE,
    version              INTEGER NOT NULL DEFAULT 1,
    question             VARCHAR(2000) NOT NULL,
    expected_answer      TEXT NOT NULL,
    expected_source_docs JSONB NOT NULL DEFAULT '[]',
    expected_citations   JSONB NOT NULL DEFAULT '[]',
    notes                TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by           VARCHAR(255) NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by           VARCHAR(255),
    deleted_at           TIMESTAMPTZ,
    is_deleted           BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_golden_items_golden_set
    ON golden_items(golden_set_id);
CREATE INDEX IF NOT EXISTS idx_golden_items_is_deleted
    ON golden_items(is_deleted);
CREATE INDEX IF NOT EXISTS idx_golden_items_created_by
    ON golden_items(created_by);

CREATE TABLE IF NOT EXISTS golden_set_versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    golden_set_id   UUID NOT NULL REFERENCES golden_sets(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    name            VARCHAR(200) NOT NULL,
    description     TEXT,
    domain          VARCHAR(50) NOT NULL,
    status          VARCHAR(20) NOT NULL,
    extra_metadata  JSONB NOT NULL DEFAULT '{}',
    items_snapshot  JSONB NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      VARCHAR(255) NOT NULL,
    UNIQUE (golden_set_id, version)
);

CREATE INDEX IF NOT EXISTS idx_gsv_golden_set
    ON golden_set_versions(golden_set_id);
CREATE INDEX IF NOT EXISTS idx_gsv_version
    ON golden_set_versions(golden_set_id, version);
"""

_EVALUATION_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS evaluation_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id        VARCHAR(200) NOT NULL,
    status          VARCHAR(20)  NOT NULL DEFAULT 'queued',
    golden_set_id   UUID,
    scope_id        UUID NOT NULL,
    total_items     INTEGER      NOT NULL DEFAULT 0,
    successful_items INTEGER     NOT NULL DEFAULT 0,
    failed_items    INTEGER      NOT NULL DEFAULT 0,
    overall_score   FLOAT,
    total_tokens    INTEGER      NOT NULL DEFAULT 0,
    total_latency_ms FLOAT       NOT NULL DEFAULT 0.0,
    total_cost      FLOAT        NOT NULL DEFAULT 0.0,
    duration_seconds FLOAT,
    actor_id        VARCHAR(255) NOT NULL,
    actor_type      VARCHAR(20)  NOT NULL DEFAULT 'user',
    metadata_json   JSONB        NOT NULL DEFAULT '{}',
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS evaluation_result_records (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id               UUID NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
    item_id              VARCHAR(200) NOT NULL,
    question             TEXT NOT NULL,
    answer               TEXT NOT NULL,
    contexts             JSONB NOT NULL DEFAULT '[]',
    expected_answer      TEXT,
    expected_sources     JSONB,
    faithfulness         FLOAT,
    answer_relevance     FLOAT,
    context_precision    FLOAT,
    context_recall       FLOAT,
    citation_present_rate FLOAT,
    hallucination_rate   FLOAT,
    overall_score        FLOAT,
    retrieval_ms         FLOAT,
    generation_ms        FLOAT,
    total_latency_ms     FLOAT,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    total_tokens         INTEGER,
    estimated_cost       FLOAT,
    evaluator_version    VARCHAR(20) NOT NULL DEFAULT '1.0',
    notes                TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_scope_id
    ON evaluation_runs(scope_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_eval_runs_status
    ON evaluation_runs(status, scope_id);
CREATE INDEX IF NOT EXISTS idx_eval_runs_batch_id
    ON evaluation_runs(batch_id);
CREATE INDEX IF NOT EXISTS idx_eval_result_run_id
    ON evaluation_result_records(run_id);
CREATE INDEX IF NOT EXISTS idx_eval_result_created_at
    ON evaluation_result_records(created_at DESC);
"""


# ---------------------------------------------------------------------------
# S2 Phase 8 (FG8.1): 추출 스키마 도메인 테이블 DDL
# ---------------------------------------------------------------------------

_EXTRACTION_SCHEMAS_DDL = """
CREATE TABLE IF NOT EXISTS extraction_schemas (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_type_code   VARCHAR(100) NOT NULL REFERENCES document_types(type_code) ON DELETE RESTRICT,
    version         INTEGER NOT NULL DEFAULT 1,
    fields_json     JSONB NOT NULL DEFAULT '{}',
    extra_metadata  JSONB NOT NULL DEFAULT '{}',
    is_deprecated   BOOLEAN NOT NULL DEFAULT FALSE,
    deprecation_reason TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      VARCHAR(255) NOT NULL,
    updated_by      VARCHAR(255) NOT NULL,
    scope_profile_id UUID REFERENCES scope_profiles(id) ON DELETE SET NULL,
    is_soft_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at      TIMESTAMPTZ,
    deleted_by      VARCHAR(255),
    UNIQUE (doc_type_code, version)
);
CREATE INDEX IF NOT EXISTS idx_extraction_schemas_doc_type_code
    ON extraction_schemas(doc_type_code);
CREATE INDEX IF NOT EXISTS idx_extraction_schemas_scope_profile_id
    ON extraction_schemas(scope_profile_id);
CREATE INDEX IF NOT EXISTS idx_extraction_schemas_is_deprecated
    ON extraction_schemas(is_deprecated);
CREATE INDEX IF NOT EXISTS idx_extraction_schemas_is_soft_deleted
    ON extraction_schemas(is_soft_deleted);
"""

_EXTRACTION_SCHEMA_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS extraction_schema_versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schema_id       UUID NOT NULL REFERENCES extraction_schemas(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    fields_json     JSONB NOT NULL DEFAULT '{}',
    extra_metadata  JSONB NOT NULL DEFAULT '{}',
    is_deprecated   BOOLEAN NOT NULL DEFAULT FALSE,
    deprecation_reason TEXT,
    change_summary  TEXT,
    changed_fields  JSONB NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      VARCHAR(255) NOT NULL,
    UNIQUE (schema_id, version)
);
CREATE INDEX IF NOT EXISTS idx_extraction_schema_versions_schema_id
    ON extraction_schema_versions(schema_id);
CREATE INDEX IF NOT EXISTS idx_extraction_schema_versions_created_at
    ON extraction_schema_versions(created_at DESC);
"""


# ---------------------------------------------------------------------------
# S2 Phase 8 (FG8.2): 추출 캔디데이트 테이블 DDL
# ---------------------------------------------------------------------------

_EXTRACTION_CANDIDATES_DDL = """
CREATE TABLE IF NOT EXISTS extraction_candidates (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id             UUID NOT NULL,
    document_version        INTEGER NOT NULL DEFAULT 1,
    extraction_schema_id    VARCHAR(100) NOT NULL,
    extraction_schema_version INTEGER NOT NULL DEFAULT 1,
    extracted_fields        JSONB NOT NULL DEFAULT '{}',
    confidence_scores       JSONB NOT NULL DEFAULT '[]',
    extraction_model        VARCHAR(255) NOT NULL,
    extraction_mode         VARCHAR(32) NOT NULL DEFAULT 'deterministic',
    extraction_latency_ms   INTEGER NOT NULL DEFAULT 0,
    extraction_tokens       JSONB,
    extraction_cost_estimate FLOAT,
    extraction_prompt_version VARCHAR(64),
    document_content_hash   VARCHAR(64),
    status                  VARCHAR(32) NOT NULL DEFAULT 'pending',
    reviewed_by             VARCHAR(255),
    reviewed_at             TIMESTAMPTZ,
    human_feedback          TEXT,
    human_edits             JSONB NOT NULL DEFAULT '[]',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_type              VARCHAR(32) NOT NULL DEFAULT 'agent',
    scope_profile_id        UUID REFERENCES scope_profiles(id) ON DELETE SET NULL,
    is_soft_deleted         BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at              TIMESTAMPTZ,
    deleted_by              VARCHAR(255)
);
CREATE INDEX IF NOT EXISTS idx_extraction_candidates_doc_ver
    ON extraction_candidates(document_id, document_version);
CREATE INDEX IF NOT EXISTS idx_extraction_candidates_status
    ON extraction_candidates(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_extraction_candidates_schema_id
    ON extraction_candidates(extraction_schema_id);
CREATE INDEX IF NOT EXISTS idx_extraction_candidates_scope
    ON extraction_candidates(scope_profile_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_extraction_candidates_soft_deleted
    ON extraction_candidates(is_soft_deleted);
"""

_APPROVED_EXTRACTIONS_DDL = """
CREATE TABLE IF NOT EXISTS approved_extractions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id            UUID REFERENCES extraction_candidates(id) ON DELETE SET NULL,
    document_id             UUID NOT NULL,
    document_version        INTEGER NOT NULL,
    extraction_schema_id    VARCHAR(100) NOT NULL,
    extraction_schema_version INTEGER NOT NULL,
    extraction_model        VARCHAR(255) NOT NULL,
    extraction_latency_ms   INTEGER NOT NULL DEFAULT 0,
    extraction_tokens       JSONB,
    extraction_cost_estimate FLOAT,
    extraction_prompt_version VARCHAR(64),
    approved_fields         JSONB NOT NULL DEFAULT '{}',
    human_edits             JSONB NOT NULL DEFAULT '[]',
    approved_by             VARCHAR(255) NOT NULL,
    approved_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approval_comment        TEXT,
    actor_type              VARCHAR(32) NOT NULL DEFAULT 'user',
    scope_profile_id        UUID REFERENCES scope_profiles(id) ON DELETE SET NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_soft_deleted         BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at              TIMESTAMPTZ,
    deleted_by              VARCHAR(255)
);
CREATE INDEX IF NOT EXISTS idx_approved_extractions_doc_ver
    ON approved_extractions(document_id, document_version);
CREATE INDEX IF NOT EXISTS idx_approved_extractions_scope_time
    ON approved_extractions(scope_profile_id, approved_at DESC);
CREATE INDEX IF NOT EXISTS idx_approved_extractions_candidate
    ON approved_extractions(candidate_id);
CREATE INDEX IF NOT EXISTS idx_approved_extractions_soft_deleted
    ON approved_extractions(is_soft_deleted);
"""


# ---------------------------------------------------------------------------
# S2 Phase 8 (Task 8-7): 배치 재추출 작업 테이블 DDL
# ---------------------------------------------------------------------------

_BATCH_EXTRACTION_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS batch_extraction_jobs (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    extraction_schema_id        VARCHAR(100) NOT NULL,
    extraction_schema_version   INTEGER NOT NULL DEFAULT 1,
    scope_profile_id            UUID REFERENCES scope_profiles(id) ON DELETE SET NULL,
    status                      VARCHAR(32) NOT NULL DEFAULT 'pending',
    total_count                 INTEGER NOT NULL DEFAULT 0,
    completed_count             INTEGER NOT NULL DEFAULT 0,
    failed_count                INTEGER NOT NULL DEFAULT 0,
    skipped_count               INTEGER NOT NULL DEFAULT 0,
    progress_percentage         FLOAT NOT NULL DEFAULT 0.0,
    date_from                   TIMESTAMPTZ,
    date_to                     TIMESTAMPTZ,
    sample_count                INTEGER,
    sample_mode                 BOOLEAN NOT NULL DEFAULT FALSE,
    comparison_mode             BOOLEAN NOT NULL DEFAULT FALSE,
    comparison_report_path      TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at                  TIMESTAMPTZ,
    completed_at                TIMESTAMPTZ,
    estimated_completion_at     TIMESTAMPTZ,
    current_processing          INTEGER,
    error_summary               TEXT,
    failed_document_ids         JSONB NOT NULL DEFAULT '[]',
    created_by                  VARCHAR(255) NOT NULL,
    is_cancellation_requested   BOOLEAN NOT NULL DEFAULT FALSE,
    actor_type                  VARCHAR(32) NOT NULL DEFAULT 'user'
);
CREATE INDEX IF NOT EXISTS idx_batch_extraction_jobs_scope_status
    ON batch_extraction_jobs(scope_profile_id, status);
CREATE INDEX IF NOT EXISTS idx_batch_extraction_jobs_created_at_status
    ON batch_extraction_jobs(created_at DESC, status);
CREATE INDEX IF NOT EXISTS idx_batch_extraction_jobs_schema_id
    ON batch_extraction_jobs(extraction_schema_id, status);
"""

_EXTRACTION_RETRY_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS extraction_retry_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID NOT NULL REFERENCES batch_extraction_jobs(id) ON DELETE CASCADE,
    document_id     UUID NOT NULL,
    attempt_number  INTEGER NOT NULL,
    status          VARCHAR(32) NOT NULL,
    error_reason    TEXT,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_extraction_retry_logs_job_doc
    ON extraction_retry_logs(job_id, document_id);
CREATE INDEX IF NOT EXISTS idx_extraction_retry_logs_created_at
    ON extraction_retry_logs(created_at DESC);
"""

# ---------------------------------------------------------------------------
# S2 Phase 8 FG8.3 (task8-8): extraction_spans 테이블
# ---------------------------------------------------------------------------

_EXTRACTION_SPANS_DDL = """
CREATE TABLE IF NOT EXISTS extraction_spans (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    extraction_candidate_id UUID NOT NULL REFERENCES extraction_candidates(id) ON DELETE CASCADE,
    field_name              VARCHAR(255) NOT NULL,
    document_id             UUID NOT NULL,
    version_id              UUID REFERENCES versions(id) ON DELETE SET NULL,
    node_id                 UUID REFERENCES nodes(id) ON DELETE SET NULL,
    span_start              INTEGER NOT NULL,
    span_end                INTEGER NOT NULL,
    source_text             TEXT NOT NULL,
    content_hash            VARCHAR(64),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (span_start >= 0 AND span_start < span_end)
);
CREATE INDEX IF NOT EXISTS idx_extraction_spans_candidate
    ON extraction_spans(extraction_candidate_id, field_name);
CREATE INDEX IF NOT EXISTS idx_extraction_spans_document
    ON extraction_spans(document_id, extraction_candidate_id);
"""

# ---------------------------------------------------------------------------
# S2 Phase 8 FG8.3 (task8-9): extraction_records + verification_results
# ---------------------------------------------------------------------------

_EXTRACTION_RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS extraction_records (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    extraction_candidate_id     UUID NOT NULL REFERENCES extraction_candidates(id) ON DELETE CASCADE,
    document_id                 UUID NOT NULL,
    document_version            INTEGER NOT NULL DEFAULT 1,
    document_content_hash       VARCHAR(64),
    extraction_schema_id        VARCHAR(100) NOT NULL,
    extraction_schema_version   INTEGER NOT NULL DEFAULT 1,
    extraction_model            VARCHAR(100) NOT NULL,
    model_version               VARCHAR(100),
    extraction_prompt_version   VARCHAR(100),
    extraction_mode             VARCHAR(32) NOT NULL DEFAULT 'deterministic',
    temperature                 FLOAT NOT NULL DEFAULT 0.0,
    seed                        INTEGER,
    extracted_result            JSONB NOT NULL DEFAULT '{}',
    extracted_timestamp         TIMESTAMPTZ,
    scope_profile_id            UUID REFERENCES scope_profiles(id) ON DELETE SET NULL,
    actor_type                  VARCHAR(32) NOT NULL DEFAULT 'agent',
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_extraction_records_candidate
    ON extraction_records(extraction_candidate_id);
CREATE INDEX IF NOT EXISTS idx_extraction_records_document
    ON extraction_records(document_id, extraction_schema_id);
CREATE INDEX IF NOT EXISTS idx_extraction_records_created_at
    ON extraction_records(created_at DESC);
"""

_VERIFICATION_RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS verification_results (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    extraction_candidate_id UUID NOT NULL REFERENCES extraction_candidates(id) ON DELETE CASCADE,
    verified_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    match_status            VARCHAR(32) NOT NULL,
    field_match_count       INTEGER NOT NULL DEFAULT 0,
    field_total_count       INTEGER NOT NULL DEFAULT 0,
    field_accuracy          FLOAT NOT NULL DEFAULT 0.0,
    diff_details            JSONB NOT NULL DEFAULT '[]',
    error_message           TEXT,
    verified_by             VARCHAR(255) NOT NULL DEFAULT 'system',
    actor_type              VARCHAR(32) NOT NULL DEFAULT 'user'
);
CREATE INDEX IF NOT EXISTS idx_verification_results_candidate
    ON verification_results(extraction_candidate_id, verified_at DESC);
"""

# ---------------------------------------------------------------------------
# S2 Phase 8 FG8.3 (task8-10): golden_extraction_sets + golden_extraction_items
#   + extraction_evaluations
# ---------------------------------------------------------------------------

_GOLDEN_EXTRACTION_SETS_DDL = """
CREATE TABLE IF NOT EXISTS golden_extraction_sets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    description     TEXT,
    document_type   VARCHAR(100) NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    created_by      VARCHAR(255) NOT NULL,
    scope_profile_id UUID REFERENCES scope_profiles(id) ON DELETE SET NULL,
    actor_type      VARCHAR(32) NOT NULL DEFAULT 'user',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_golden_extraction_sets_doc_type
    ON golden_extraction_sets(document_type);
CREATE INDEX IF NOT EXISTS idx_golden_extraction_sets_scope
    ON golden_extraction_sets(scope_profile_id);
"""

_GOLDEN_EXTRACTION_ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS golden_extraction_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    golden_set_id   UUID NOT NULL REFERENCES golden_extraction_sets(id) ON DELETE CASCADE,
    document_id     UUID NOT NULL,
    document_version INTEGER NOT NULL DEFAULT 1,
    document_type   VARCHAR(100) NOT NULL,
    expected_fields JSONB NOT NULL DEFAULT '[]',
    expected_spans  JSONB NOT NULL DEFAULT '[]',
    created_by      VARCHAR(255) NOT NULL DEFAULT 'system',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_golden_extraction_items_set
    ON golden_extraction_items(golden_set_id);
CREATE INDEX IF NOT EXISTS idx_golden_extraction_items_document
    ON golden_extraction_items(document_id);
"""

_EXTRACTION_EVALUATIONS_DDL = """
CREATE TABLE IF NOT EXISTS extraction_evaluations (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    golden_set_id           UUID REFERENCES golden_extraction_sets(id) ON DELETE SET NULL,
    golden_item_id          UUID REFERENCES golden_extraction_items(id) ON DELETE SET NULL,
    extraction_candidate_id UUID REFERENCES extraction_candidates(id) ON DELETE SET NULL,
    metrics                 JSONB NOT NULL DEFAULT '{}',
    field_details           JSONB NOT NULL DEFAULT '[]',
    evaluated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    evaluated_by            VARCHAR(255) NOT NULL DEFAULT 'system',
    actor_type              VARCHAR(32) NOT NULL DEFAULT 'user',
    scope_profile_id        UUID REFERENCES scope_profiles(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_extraction_evaluations_golden_set
    ON extraction_evaluations(golden_set_id, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_extraction_evaluations_candidate
    ON extraction_evaluations(extraction_candidate_id);
"""


def init_db() -> None:
    """앱 시작 시 모든 테이블을 생성하고 마이그레이션을 적용한다 (idempotent).

    각 DDL을 개별 savepoint로 감싸므로, 테이블 소유권이 없어 ALTER TABLE이 실패해도
    나머지 DDL은 계속 실행된다. 이미 스키마가 구성된 환경에서 재시작할 때 안전하다.
    """
    _ddl_steps = [
        # Phase 1~3 기본 테이블
        ("DOCUMENTS_DDL", _DOCUMENTS_DDL),
        ("VERSIONS_DDL", _VERSIONS_DDL),
        ("NODES_DDL", _NODES_DDL),
        ("IDEMPOTENCY_DDL", _IDEMPOTENCY_DDL),
        ("AUDIT_EVENTS_DDL", _AUDIT_EVENTS_DDL),
        # Phase 4 마이그레이션
        ("VERSIONS_MIGRATION", _VERSIONS_MIGRATION_DDL),
        ("DOCUMENTS_MIGRATION", _DOCUMENTS_MIGRATION_DDL),
        # Phase 5
        ("REVIEW_ACTIONS_DDL", _REVIEW_ACTIONS_DDL),
        ("WORKFLOW_HISTORY_DDL", _WORKFLOW_HISTORY_DDL),
        ("CHANGE_LOGS_DDL", _CHANGE_LOGS_DDL),
        ("VERSIONS_WORKFLOW_MIGRATION", _VERSIONS_WORKFLOW_MIGRATION_DDL),
        ("REVIEW_ACTIONS_MIGRATION", _REVIEW_ACTIONS_MIGRATION_DDL),
        ("WORKFLOW_HISTORY_MIGRATION", _WORKFLOW_HISTORY_MIGRATION_DDL),
        ("CHANGE_LOGS_MIGRATION", _CHANGE_LOGS_MIGRATION_DDL),
        # Phase 7 Admin 테이블
        ("ORGANIZATIONS_DDL", _ORGANIZATIONS_DDL),
        ("ROLES_DDL", _ROLES_DDL),
        ("USERS_DDL", _USERS_DDL),
        ("USER_ORG_ROLES_DDL", _USER_ORG_ROLES_DDL),
        ("DOCUMENT_TYPES_DDL", _DOCUMENT_TYPES_DDL),
        ("BACKGROUND_JOBS_DDL", _BACKGROUND_JOBS_DDL),
        ("API_KEYS_DDL", _API_KEYS_DDL),
        # Phase 8 FTS
        ("FTS_MIGRATION", _FTS_MIGRATION_DDL),
        ("SEARCH_INDEX_STATS_DDL", _SEARCH_INDEX_STATS_DDL),
        # Phase 10
        ("DOCUMENT_CHUNKS_DDL", _DOCUMENT_CHUNKS_DDL),
        ("EMBEDDING_TOKEN_USAGE_DDL", _EMBEDDING_TOKEN_USAGE_DDL),
        # Phase 14
        ("USERS_AUTH_MIGRATION", _USERS_AUTH_MIGRATION_DDL),
        ("REFRESH_TOKENS_DDL", _REFRESH_TOKENS_DDL),
        ("OAUTH_ACCOUNTS_DDL", _OAUTH_ACCOUNTS_DDL),
        ("SYSTEM_SETTINGS_DDL", _SYSTEM_SETTINGS_DDL),
        ("SYSTEM_SETTINGS_SEED", _SYSTEM_SETTINGS_SEED_DDL),
        ("ALERT_RULES_DDL", _ALERT_RULES_DDL),
        ("ALERT_HISTORY_DDL", _ALERT_HISTORY_DDL),
        ("JOB_SCHEDULES_DDL", _JOB_SCHEDULES_DDL),
        ("JOB_SCHEDULES_SEED", _JOB_SCHEDULES_SEED_DDL),
        # S2 Phase 2
        ("DOCUMENT_TYPES_RETRIEVAL_CONFIG_MIGRATION", _DOCUMENT_TYPES_RETRIEVAL_CONFIG_MIGRATION_DDL),
        # S2 Phase 3
        ("CONVERSATIONS_DDL", _CONVERSATIONS_DDL),
        ("TURNS_DDL", _TURNS_DDL),
        ("MESSAGES_DDL", _MESSAGES_DDL),
        ("CONVERSATIONS_FTS", _CONVERSATIONS_FTS_DDL),
        ("CONVERSATIONS_TITLE_TSV", _CONVERSATIONS_TITLE_TSV_DDL),
        ("AUDIT_EVENTS_ACTOR_TYPE_MIGRATION", _AUDIT_EVENTS_ACTOR_TYPE_MIGRATION_DDL),
        ("RETENTION_POLICIES_DDL", _RETENTION_POLICIES_DDL),
        # S2 Phase 4
        ("SCOPE_PROFILES_DDL", _SCOPE_PROFILES_DDL),
        ("SCOPE_DEFINITIONS_DDL", _SCOPE_DEFINITIONS_DDL),
        ("AGENTS_DDL", _AGENTS_DDL),
        ("API_KEYS_AGENT_MIGRATION", _API_KEYS_AGENT_MIGRATION_DDL),
        # S2-5 (2026-04-20): users.scope_profile_id + 기본 Scope Profile 시드는
        # Alembic 으로 이관. backend/app/db/migrations/versions/20260420_1330_*.py 참고.
        # S2 Phase 5
        ("AUDIT_EVENTS_AGENT_MIGRATION", _AUDIT_EVENTS_AGENT_MIGRATION_DDL),
        ("TRANSITION_PROPOSALS_DDL", _TRANSITION_PROPOSALS_DDL),
        ("MCP_TASKS_DDL", _MCP_TASKS_DDL),
        ("AGENT_PROPOSALS_DDL", _AGENT_PROPOSALS_DDL),
        # S2 Phase 6
        ("LLM_PROVIDERS_DDL", _LLM_PROVIDERS_DDL),
        ("PROMPTS_DDL", _PROMPTS_DDL),
        ("PROMPTS_SEED", _PROMPTS_SEED_DDL),
        # S2 Phase 7
        ("GOLDEN_SETS_DDL", _GOLDEN_SETS_DDL),
        ("EVALUATION_RUNS_DDL", _EVALUATION_RUNS_DDL),
        # S2 Phase 8
        ("EXTRACTION_SCHEMAS_DDL", _EXTRACTION_SCHEMAS_DDL),
        ("EXTRACTION_SCHEMA_VERSIONS_DDL", _EXTRACTION_SCHEMA_VERSIONS_DDL),
        ("EXTRACTION_CANDIDATES_DDL", _EXTRACTION_CANDIDATES_DDL),
        ("APPROVED_EXTRACTIONS_DDL", _APPROVED_EXTRACTIONS_DDL),
        ("BATCH_EXTRACTION_JOBS_DDL", _BATCH_EXTRACTION_JOBS_DDL),
        ("EXTRACTION_RETRY_LOGS_DDL", _EXTRACTION_RETRY_LOGS_DDL),
        ("EXTRACTION_SPANS_DDL", _EXTRACTION_SPANS_DDL),
        ("EXTRACTION_RECORDS_DDL", _EXTRACTION_RECORDS_DDL),
        ("VERIFICATION_RESULTS_DDL", _VERIFICATION_RESULTS_DDL),
        ("GOLDEN_EXTRACTION_SETS_DDL", _GOLDEN_EXTRACTION_SETS_DDL),
        ("GOLDEN_EXTRACTION_ITEMS_DDL", _GOLDEN_EXTRACTION_ITEMS_DDL),
        ("EXTRACTION_EVALUATIONS_DDL", _EXTRACTION_EVALUATIONS_DDL),
    ]

    skipped: list[str] = []
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                for label, ddl in _ddl_steps:
                    try:
                        cur.execute("SAVEPOINT ddl_step")
                        cur.execute(ddl)
                        cur.execute("RELEASE SAVEPOINT ddl_step")
                    except Exception as exc:
                        cur.execute("ROLLBACK TO SAVEPOINT ddl_step")
                        skipped.append(label)
                        logger.debug("DDL skipped [%s]: %s", label, str(exc).strip())
        if skipped:
            logger.warning("DB init: %d DDL step(s) skipped (already applied or insufficient privilege): %s",
                           len(skipped), ", ".join(skipped))
        logger.info("DB schema initialized")
    except Exception as exc:
        logger.error("DB schema initialization failed: %s", exc)
        raise
