"""S3 P2 FG 2-5: saved_views (사용자 정의 필터+정렬+레이아웃)

Revision ID: s3_p2_saved_views
Revises: s3_p2_document_links
Create Date: 2026-05-10 14:00:00

배경
----

`docs/개발문서/S3/phase2/작업지시서/task2-5.md` §2.1 (1) 의 저장 구조.
사용자가 자주 쓰는 (필터 + 정렬 + 레이아웃) 조합을 저장하고 URL 로 공유한다.

Pre-flight 갱신: `docs/개발문서/S3/phase2/산출물/FG2-5_Pre-flight_갱신.md`.

테이블
------

``saved_views`` — 1 사용자당 정의 ≤ 50 개.

- `id`                — 행 PK
- `owner_id`          — FK users
- `name`              — 사용자가 입력한 이름 (1~200 chars)
- `filter`            — JSONB. SavedViewFilter Pydantic schema
- `sort`              — JSONB. SavedViewSort 배열 (multi-sort)
- `layout`            — VARCHAR(16). DocumentLayout Literal ("list"|"tree"|"cards"|"graph")
- `include_tag_nodes` — BOOL. 그래프 layout 의 메타노드 포함 (기본 false)
- `created_at` / `updated_at`

UNIQUE 제약: ``(owner_id, name)`` — 같은 owner 의 같은 이름 중복 방지 (task2-5.md §7 R-04).
사용자당 상한 50 은 서비스 계층에서 강제 (DB 제약 외).

ACL
---

owner 만 PATCH/DELETE. GET 단건은 인증된 모든 사용자에게 정의 노출 (단 owner_id 마스킹).
공유 URL 모델 — 결과는 viewer 의 ScopeProfile 로 documents API 가 재필터.

실행
----

    cd backend
    ALEMBIC_POSTGRES_USER=mimir_admin alembic upgrade head
"""
from __future__ import annotations

from alembic import op


revision = "s3_p2_saved_views"  # 18 chars (VARCHAR(32) 한도)
down_revision = "s3_p2_document_links"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# UP
# ---------------------------------------------------------------------------

_CREATE_SAVED_VIEWS_SQL = """
CREATE TABLE IF NOT EXISTS saved_views (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                VARCHAR(200) NOT NULL,
    filter              JSONB NOT NULL DEFAULT '{}'::jsonb,
    sort                JSONB NOT NULL DEFAULT '[]'::jsonb,
    layout              VARCHAR(16) NOT NULL,
    include_tag_nodes   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_saved_views_owner_name
        UNIQUE (owner_id, name),
    CONSTRAINT chk_saved_views_layout
        CHECK (layout IN ('list', 'tree', 'cards', 'graph'))
);

-- 사용자 본인 목록 조회용 인덱스
CREATE INDEX IF NOT EXISTS idx_saved_views_owner
    ON saved_views(owner_id, updated_at DESC);
"""


# ---------------------------------------------------------------------------
# DOWN
# ---------------------------------------------------------------------------

_DROP_SQL = """
DROP TABLE IF EXISTS saved_views;
"""


def upgrade() -> None:
    op.execute(_CREATE_SAVED_VIEWS_SQL)


def downgrade() -> None:
    op.execute(_DROP_SQL)
