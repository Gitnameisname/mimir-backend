"""S3 P4 FG 4-0 §2.1.6: scope_profiles.allowed_tools (MCP tool-level ACL)

Revision ID: s3_p4_scope_profile_allow_tools
Revises: s3_p2_tags
Create Date: 2026-04-28 12:00:00

Note: revision id 는 ``alembic_version.version_num`` ``VARCHAR(32)`` 제한으로
``"allow_tools"`` 로 단축 (31 char). 컬럼명·docstring 의미 표기는 그대로
``allowed_tools`` 유지 — 코드와 DB 측 식별자는 `allowed_tools` 가 정본.

배경
----

S3 Phase 3 task3-3.md §[129,223–225,318] 가 요구한 `ScopeProfile.allowed_tools` +
`AgentPrincipal.can_read("annotations")` tool-level 게이트가 read_annotations
단일이 아니라 **모든 MCP tool 표면**에서 미구현으로 확인됨 (2026-04-27 Codex
정적 리뷰 P1 / `docs/개발문서/S3/종결회고.md §4.1 #11`).

본 revision 은 Phase 4 FG 4-0 §2.1.6 의 흡수 종결을 수행한다.

스키마
------

`scope_profiles.allowed_tools` (`JSONB NOT NULL DEFAULT '[]'::jsonb`):
- 빈 배열 = default-deny (운영자 명시 등록 전까지 모든 에이전트 도구 호출 거부).
- 등록 가능 값: `app.schemas.mcp.known_tool_names()` 만 (manifest 정합).
- 기존 행은 빈 배열로 backfill — 운영자가 적용 직후 Admin UI 로 도구 등록 필요.

운영 절차
--------

1. `alembic upgrade head` 실행 (운영자) — 본 revision 적용
2. 운영자가 `AdminScopeProfilesPage.tsx` 또는 `migrate_scope_profile_allowed_tools.py`
   로 기존 ScopeProfile 의 `allowed_tools` 등록
3. 등록 전까지 모든 MCP tool 호출이 거부됨 — 운영 영향 의식적 트레이드오프

ACL
---

본 컬럼 자체는 `documents.scope_profile_id` 와 무관 — 별 차원 (per-(profile, tool)).
기존 ACL (`scope_definitions.acl_filter`, `scope_profiles.settings_json`) 와 중복
의미 없음.

실행
----

    cd backend
    ALEMBIC_POSTGRES_USER=mimir_admin alembic upgrade head
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic
revision = "s3_p4_scope_profile_allow_tools"  # 31 char (VARCHAR(32) 한도)
down_revision = "s3_p2_tags"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# UP
# ---------------------------------------------------------------------------

_ADD_COLUMN_SQL = """
ALTER TABLE scope_profiles
    ADD COLUMN IF NOT EXISTS allowed_tools JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 정합 보조 인덱스: allowed_tools 가 비지 않은 프로파일 빠르게 식별 (운영 진단용)
CREATE INDEX IF NOT EXISTS idx_scope_profiles_allowed_tools_nonempty
    ON scope_profiles ((jsonb_array_length(allowed_tools) > 0))
    WHERE jsonb_array_length(allowed_tools) > 0;
"""


# ---------------------------------------------------------------------------
# DOWN
# ---------------------------------------------------------------------------

_DOWN_SQL = """
DROP INDEX IF EXISTS idx_scope_profiles_allowed_tools_nonempty;
ALTER TABLE scope_profiles DROP COLUMN IF EXISTS allowed_tools;
"""


def upgrade() -> None:
    op.execute(_ADD_COLUMN_SQL)


def downgrade() -> None:
    op.execute(_DOWN_SQL)
