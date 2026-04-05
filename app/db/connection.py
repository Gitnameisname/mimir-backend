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
    """앱 시작 시 모든 테이블을 생성한다 (idempotent).

    main.py의 startup 이벤트에서 호출한다.
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(_DOCUMENTS_DDL)
                cur.execute(_VERSIONS_DDL)
                cur.execute(_NODES_DDL)
                cur.execute(_IDEMPOTENCY_DDL)
        logger.info("DB schema initialized (documents, versions, nodes, idempotency_records tables ready)")
    except Exception as exc:
        logger.error("DB schema initialization failed: %s", exc)
        raise
