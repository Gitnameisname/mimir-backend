"""S3 P2 FG 2-3: document_links (백링크 [[문서명]])

Revision ID: s3_p2_document_links
Revises: s3_p4_agent_prop_idempotency
Create Date: 2026-05-10 12:00:00

배경
----

`docs/개발문서/S3/phase2/작업지시서/task2-3.md` §2.1 (1) 의 백링크 저장 구조.
본 revision 은 Phase 2 FG 2-3 가 Phase 3 / Phase 4 진입 시점에 미완료로 이월된 항목을
2026-05-10 시점에 재진입해 head 위에 쌓는 형태로 적용한다.

Pre-flight 갱신 메모: `docs/개발문서/S3/phase2/산출물/FG2-3_Pre-flight_갱신.md` §2.1.

테이블
------

``document_links`` — 본문 ``[[문서명]]`` 토큰의 양방향 그래프 에지.
각 행은 ``rebuild_wikilinks_for_document`` 동기화 시점에 ``replace`` 된다.

* ``id``                 — 행 PK
* ``from_document_id``   — 출발 문서 (FK documents, ON DELETE CASCADE)
* ``to_document_id``     — 도착 문서 (FK documents, NULL 허용 — missing/ambiguous 상태)
* ``node_id``            — 출발 문서의 block node_id (앵커)
* ``raw_text``           — 본문에 입력된 원문 (alias 제외, NFC 미적용)
* ``resolved_status``    — ``'resolved' | 'ambiguous' | 'missing'``
* ``created_at``

UNIQUE 제약: ``(from_document_id, node_id, raw_text)`` — 같은 block 안의 같은 원문은 한 번만.

ACL
---

본 테이블 자체는 ACL 결정에 영향 주지 않는다. 읽기 시점 (`/backlinks` / `/links`) 에
``documents`` JOIN 으로 viewer Scope 필터가 자동 적용된다.

실행
----

    cd backend
    ALEMBIC_POSTGRES_USER=mimir_admin alembic upgrade head
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic
revision = "s3_p2_document_links"  # 22 chars (VARCHAR(32) 한도)
down_revision = "s3_p4_agent_prop_idempotency"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# UP
# ---------------------------------------------------------------------------

_CREATE_DOCUMENT_LINKS_SQL = """
CREATE TABLE IF NOT EXISTS document_links (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_document_id    UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    to_document_id      UUID REFERENCES documents(id) ON DELETE SET NULL,
    node_id             UUID NOT NULL,
    raw_text            VARCHAR(500) NOT NULL,
    resolved_status     VARCHAR(16) NOT NULL DEFAULT 'missing',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_document_links_from_node_raw
        UNIQUE (from_document_id, node_id, raw_text),
    CONSTRAINT chk_document_links_status
        CHECK (resolved_status IN ('resolved', 'ambiguous', 'missing'))
);

-- 역방향 조회 (이 문서를 참조하는 문서 — /backlinks)
CREATE INDEX IF NOT EXISTS idx_document_links_to
    ON document_links(to_document_id)
    WHERE to_document_id IS NOT NULL;

-- 정방향 조회 (이 문서가 내보내는 링크 — /links, replace 경로)
CREATE INDEX IF NOT EXISTS idx_document_links_from
    ON document_links(from_document_id);
"""


# ---------------------------------------------------------------------------
# DOWN
# ---------------------------------------------------------------------------

_DROP_SQL = """
DROP TABLE IF EXISTS document_links;
"""


def upgrade() -> None:
    op.execute(_CREATE_DOCUMENT_LINKS_SQL)


def downgrade() -> None:
    op.execute(_DROP_SQL)
