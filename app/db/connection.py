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
                # Phase 10 pgvector 확장 및 문서 청크 테이블
                cur.execute(_PGVECTOR_EXTENSION_DDL)
                cur.execute(_DOCUMENT_CHUNKS_DDL)
                cur.execute(_DOCUMENT_CHUNKS_VECTOR_INDEX_DDL)
                cur.execute(_EMBEDDING_TOKEN_USAGE_DDL)
        logger.info("DB schema initialized (Phase 10 pgvector included)")
    except Exception as exc:
        logger.error("DB schema initialization failed: %s", exc)
        raise
