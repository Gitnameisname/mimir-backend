"""S3 P4 FG 4-6 §2.1.2: agent_proposals.idempotency_key (write 도구 idempotency)

Revision ID: s3_p4_agent_prop_idempotency
Revises: s3_p4_scope_profile_allow_tools
Create Date: 2026-04-28 14:00:00

Note: revision id 는 ``alembic_version.version_num`` ``VARCHAR(32)`` 제한 (FG 4-0
lesson) — 30자.

배경
----

S3 Phase 4 FG 4-6 의 4 사전 조건 중 첫째: **idempotency**. 같은
``(agent_id, idempotency_key)`` 입력 시 같은 proposal 반환 (네트워크 재시도 안전).

스키마
------

`agent_proposals.idempotency_key` (`VARCHAR(128)`):
- NULL 허용 — 기존 호출자 (idempotency_key 미입력) 호환.
- (agent_id, idempotency_key) UNIQUE INDEX — idempotent 보장 + 다른 agent 의 key 재사용 차단.

운영 절차
--------

1. `alembic upgrade head` 실행 (운영자) — 본 revision 적용
2. agent_proposal_service.propose_draft() 가 idempotency_key 인자 지원
3. tool_save_draft (FG 4-6) 가 본 컬럼 활용

실행
----

    cd backend
    ALEMBIC_POSTGRES_USER=mimir_admin alembic upgrade head
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic
revision = "s3_p4_agent_prop_idempotency"  # 30 char (VARCHAR(32) 한도)
down_revision = "s3_p4_scope_profile_allow_tools"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# UP
# ---------------------------------------------------------------------------

_ADD_COLUMN_SQL = """
ALTER TABLE agent_proposals
    ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128);

-- (agent_id, idempotency_key) UNIQUE — idempotent 호출 보장.
-- WHERE idempotency_key IS NOT NULL: 기존 행 (NULL) 은 제약 외.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_proposals_agent_idempotency
    ON agent_proposals(agent_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
"""


# ---------------------------------------------------------------------------
# DOWN
# ---------------------------------------------------------------------------

_DOWN_SQL = """
DROP INDEX IF EXISTS idx_agent_proposals_agent_idempotency;
ALTER TABLE agent_proposals DROP COLUMN IF EXISTS idempotency_key;
"""


def upgrade() -> None:
    op.execute(_ADD_COLUMN_SQL)


def downgrade() -> None:
    op.execute(_DOWN_SQL)
