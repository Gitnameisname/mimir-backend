"""P6-2: extraction_schema_versions.extra_metadata 의 rolled_back_from_version 소급 채움

Revision ID: p6_2_backfill_rollback
Revises: s2_5_users_scope
Create Date: 2026-04-22 06:54:00

배경:
  P4 (2026-04-22) 부터 rollback 으로 생성된 버전 이력은
  `extra_metadata = {"rolled_back_from_version": N}` 을 함께 기록한다.
  P5 프론트는 이 1급 필드를 1순위 감지 근거로 쓰고, default 한국어 요약
  (`"v{N} 로 되돌리기"`) 은 2순위 폴백 경로로만 사용한다.

  그러나 P4 배포 이전에 이미 존재하는 rollback 버전은 change_summary 만
  가지고 있고 extra_metadata 에는 해당 키가 없다. 이 상태로 남겨두면:

    1) P6-1 핑퐁 감지가 구 이력에서 "요약 폴백" 경로로만 동작 — custom
       요약을 쓴 사용자의 구 이력은 아예 감지 밖.
    2) S2 ⑤ (scope) / ⑥ (actor_type) 검증에는 영향이 없지만, diff 화면이
       "이 버전은 롤백입니다" 뱃지를 일관되게 표시하려면 1급 필드가 채워져
       있어야 한다.

  본 마이그레이션은 다음 조건을 모두 만족하는 행에 대해 소급 채움을 수행:
    - change_summary 가 `^v(\\d+)\\s*로\\s*되돌리기` 패턴과 일치
    - extra_metadata 에 `rolled_back_from_version` 키가 아직 없음
    - 추출된 N 이 1 이상의 정수

  실행 요건:
    backend 디렉터리에서 OWNER 권한 DB 유저로 `alembic upgrade head`.
    UPDATE 만 수행하므로 DDL 락은 필요 없다. 그러나 행 수가 많으면 단일
    UPDATE 로 묶여 트랜잭션이 길어질 수 있어, 실운영 DB 에선 off-peak 시간
    권장.

다운그레이드 정책:
  데이터 소급 채움은 개념상 "복구해도 잃는 것이 없는" 방향이라 되돌릴 필요가
  없다. 기존 rollback 이력의 정합성만 높아지는 작업이므로 downgrade 는
  no-op. 재실행이 필요하면 upgrade 를 다시 돌리면 되며, WHERE 조건이
  이중 적용을 막는다 (extra_metadata ? 'rolled_back_from_version' 필터).
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic
revision = "p6_2_backfill_rollback"
down_revision = "s2_5_users_scope"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------
#
# 구현 메모:
#   - `regexp_match(change_summary, '^v(\d+)\s*로\s*되돌리기')` 는 매칭 실패
#     시 NULL 을 반환하고, 첫 번째 캡처 그룹이 결과 배열의 [1] 인덱스.
#   - `jsonb_set(target, path, value, create_missing)` 에서 create_missing
#     기본값이 true 라 키가 없어도 생성된다. 그러나 target 이 NULL 이면 결과
#     도 NULL 이므로 `COALESCE(extra_metadata, '{}'::jsonb)` 로 방어.
#   - `NOT (extra_metadata ? 'rolled_back_from_version')` 은 NULL 에 대해
#     UNKNOWN 을 돌려 행이 제외될 수 있다 → COALESCE 로 안전화.
#   - 추출한 N 을 `to_jsonb(N::int)` 로 감싸 JSONB 숫자 리터럴로 저장
#     (P5 DTO 의 `Optional[int]` 와 호환). 문자열 "2" 로 저장하면 서버
#     strict validator(P5) 에서 탈락한다.

_BACKFILL_SQL = """
UPDATE extraction_schema_versions
SET extra_metadata = jsonb_set(
        COALESCE(extra_metadata, '{}'::jsonb),
        '{rolled_back_from_version}',
        to_jsonb(
            ((regexp_match(change_summary, '^v(\\d+)\\s*로\\s*되돌리기'))[1])::int
        ),
        true
    )
WHERE change_summary ~ '^v\\d+\\s*로\\s*되돌리기'
  AND NOT (COALESCE(extra_metadata, '{}'::jsonb) ? 'rolled_back_from_version')
  AND ((regexp_match(change_summary, '^v(\\d+)\\s*로\\s*되돌리기'))[1])::int >= 1;
"""


def upgrade() -> None:
    op.execute(_BACKFILL_SQL)


def downgrade() -> None:
    # 데이터 소급 채움은 되돌리지 않는다 — 개념상 손실 없는 방향.
    # 재실행이 필요하면 upgrade 를 다시 호출. WHERE 조건이 이중 적용을 막음.
    pass
