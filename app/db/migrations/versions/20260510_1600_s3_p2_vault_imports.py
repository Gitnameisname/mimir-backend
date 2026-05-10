"""S3 P2 FG 2-6: vault_imports (옵시디언 vault 일회성 import)

Revision ID: s3_p2_vault_imports
Revises: s3_p2_saved_views
Create Date: 2026-05-10 16:00:00

배경
----

`docs/개발문서/S3/phase2/작업지시서/task2-6.md` §2.1 (1).

테이블
------

``vault_imports`` — 업로드 → 파싱 → 변환 진행 상태 추적 + report 보관.

- `id`                 — 행 PK
- `owner_id`           — FK users
- `uploaded_filename`  — 업로드 시 파일명 (안전 표시용)
- `bytes_original`     — 업로드 zip 바이트 수
- `bytes_extracted`    — 압축 해제 후 누적 바이트
- `file_count`         — markdown 파일 수
- `status`             — pending/running/succeeded/failed/cancelled
- `scope_profile_id`   — 변환된 documents 의 scope_profile_id
- `started_at` / `finished_at`
- `report`             — JSONB. 가져온 문서 수 / 폴더 수 / 태그 수 / 백링크 수 / PII 집계 / 실패 원인
- `created_at`         — 인덱스 정렬용

ACL
---

owner 본인 + admin 만 조회. 라우터 단에서 강제.

실행
----

    cd backend
    ALEMBIC_POSTGRES_USER=mimir_admin alembic upgrade head
"""
from __future__ import annotations

from alembic import op


revision = "s3_p2_vault_imports"  # 19 chars
down_revision = "s3_p2_saved_views"
branch_labels = None
depends_on = None


_CREATE_VAULT_IMPORTS_SQL = """
CREATE TABLE IF NOT EXISTS vault_imports (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    uploaded_filename   VARCHAR(500) NOT NULL,
    bytes_original      BIGINT NOT NULL DEFAULT 0,
    bytes_extracted     BIGINT NOT NULL DEFAULT 0,
    file_count          INTEGER NOT NULL DEFAULT 0,
    status              VARCHAR(16) NOT NULL DEFAULT 'pending',
    scope_profile_id    UUID NOT NULL REFERENCES scope_profiles(id) ON DELETE CASCADE,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    report              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_vault_imports_status
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled'))
);

-- owner 본인 목록 조회 인덱스
CREATE INDEX IF NOT EXISTS idx_vault_imports_owner_created
    ON vault_imports(owner_id, created_at DESC);
"""


_DROP_SQL = """
DROP TABLE IF EXISTS vault_imports;
"""


def upgrade() -> None:
    op.execute(_CREATE_VAULT_IMPORTS_SQL)


def downgrade() -> None:
    op.execute(_DROP_SQL)
