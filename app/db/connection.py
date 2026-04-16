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
# Phase 10: pgvector 확장 및 document_chunks 테이블
# ---------------------------------------------------------------------------

_PGVECTOR_EXTENSION_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
"""

_DOCUMENT_CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS document_chunks (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id         UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_id          UUID NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    node_id             UUID REFERENCES nodes(id) ON DELETE SET NULL,
    chunk_index         INTEGER NOT NULL,
    source_text         TEXT NOT NULL,
    embedding           vector(1536),
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

# HNSW 인덱스: 테이블 생성 후 별도 DDL (embedding NULL인 행 제외)
_DOCUMENT_CHUNKS_VECTOR_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON document_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE embedding IS NOT NULL AND is_current = TRUE;
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


def init_db() -> None:
    """앱 시작 시 모든 테이블을 생성하고 마이그레이션을 적용한다 (idempotent)."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                # Phase 1~3 기본 테이블
                cur.execute(_DOCUMENTS_DDL)
                cur.execute(_VERSIONS_DDL)
                cur.execute(_NODES_DDL)
                cur.execute(_IDEMPOTENCY_DDL)
                cur.execute(_AUDIT_EVENTS_DDL)
                # Phase 4 마이그레이션 (ADD COLUMN IF NOT EXISTS — 멱등)
                cur.execute(_VERSIONS_MIGRATION_DDL)
                cur.execute(_DOCUMENTS_MIGRATION_DDL)
                # Phase 5 테이블 생성 (멱등)
                cur.execute(_REVIEW_ACTIONS_DDL)
                cur.execute(_WORKFLOW_HISTORY_DDL)
                cur.execute(_CHANGE_LOGS_DDL)
                # Phase 5 마이그레이션
                cur.execute(_VERSIONS_WORKFLOW_MIGRATION_DDL)
                cur.execute(_REVIEW_ACTIONS_MIGRATION_DDL)
                cur.execute(_WORKFLOW_HISTORY_MIGRATION_DDL)
                cur.execute(_CHANGE_LOGS_MIGRATION_DDL)
                # Phase 7 Admin 테이블 (멱등)
                cur.execute(_ORGANIZATIONS_DDL)
                cur.execute(_ROLES_DDL)
                cur.execute(_USERS_DDL)
                cur.execute(_USER_ORG_ROLES_DDL)
                cur.execute(_DOCUMENT_TYPES_DDL)
                cur.execute(_BACKGROUND_JOBS_DDL)
                cur.execute(_API_KEYS_DDL)
                # Phase 8 FTS 마이그레이션
                cur.execute(_FTS_MIGRATION_DDL)
                cur.execute(_SEARCH_INDEX_STATS_DDL)
                # Phase 10 pgvector 확장 및 문서 청크 테이블.
                # pgvector 가 서버에 설치되지 않은 개발 환경(로컬 등)에서는
                # SAVEPOINT 로 격리해 인증·관리 기능을 위한 나머지 DDL 만이라도
                # 진행되도록 한다. RAG / 벡터 검색 기능은 pgvector 설치 전까지
                # 비활성 상태가 된다.
                cur.execute("SAVEPOINT sp_pgvector")
                try:
                    cur.execute(_PGVECTOR_EXTENSION_DDL)
                    cur.execute(_DOCUMENT_CHUNKS_DDL)
                    cur.execute(_DOCUMENT_CHUNKS_VECTOR_INDEX_DDL)
                    cur.execute("RELEASE SAVEPOINT sp_pgvector")
                except psycopg2.Error as vec_err:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_pgvector")
                    cur.execute("RELEASE SAVEPOINT sp_pgvector")
                    logger.warning(
                        "pgvector DDL skipped (extension not available): %s. "
                        "벡터 검색/RAG 기능이 비활성됩니다. 사용하려면 Postgres 에 "
                        "pgvector 확장을 설치 후 앱을 재기동하세요.",
                        vec_err,
                    )
                cur.execute(_EMBEDDING_TOKEN_USAGE_DDL)
                # Phase 14 마이그레이션 (users 인증 컬럼 + refresh_tokens)
                cur.execute(_USERS_AUTH_MIGRATION_DDL)
                cur.execute(_REFRESH_TOKENS_DDL)
                # Phase 14-4: OAuth 계정 연동 테이블
                cur.execute(_OAUTH_ACCOUNTS_DDL)
                # Phase 14-11: 시스템 설정 테이블 + 시드
                cur.execute(_SYSTEM_SETTINGS_DDL)
                cur.execute(_SYSTEM_SETTINGS_SEED_DDL)
                # Phase 14-13: 알림 관리 테이블
                cur.execute(_ALERT_RULES_DDL)
                cur.execute(_ALERT_HISTORY_DDL)
                # Phase 14-14: 배치 작업 스케줄 + 시드
                cur.execute(_JOB_SCHEDULES_DDL)
                cur.execute(_JOB_SCHEDULES_SEED_DDL)
                # Phase 2 (S2): retrieval_config 컬럼 추가 (멱등)
                cur.execute(_DOCUMENT_TYPES_RETRIEVAL_CONFIG_MIGRATION_DDL)
                # Phase 3 (S2): Conversation 도메인 테이블 생성 (멱등)
                cur.execute(_CONVERSATIONS_DDL)
                cur.execute(_TURNS_DDL)
                cur.execute(_MESSAGES_DDL)
                cur.execute(_CONVERSATIONS_FTS_DDL)
                # Phase 3 (S2): audit_events actor_type 컬럼 추가 (멱등)
                cur.execute(_AUDIT_EVENTS_ACTOR_TYPE_MIGRATION_DDL)
                # Phase 3 (S2): retention_policies 테이블 생성 (멱등)
                cur.execute(_RETENTION_POLICIES_DDL)
        logger.info("DB schema initialized (Phase 3 S2 Conversation domain included)")
    except Exception as exc:
        logger.error("DB schema initialization failed: %s", exc)
        raise
