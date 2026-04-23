"""P7-2-c: document_types.type_code 및 extraction_schemas.doc_type_code 를 UPPER 로 일괄 정규화

Revision ID: p7_2_c_uppercase_doc_type
Revises: p6_2_backfill_rollback
Create Date: 2026-04-22 12:00:00

배경:
  P7-1 이후로 서버/프론트 모두 신규 입력을 UPPER 로 저장한다
  (서버 `CreateExtractionSchemaRequest`, `CreateDocumentTypeBody` 의 field_validator
   및 프론트 `normalizeDocTypeCode`). 그러나 P7-1 이전에 raw SQL 또는 API 를
  거치지 않고 삽입된 소문자 / 혼합 케이스 레코드는 그대로 남아 있을 수 있고,
  이 경우 신규 생성 요청(대문자) 이 "같은 개념의 다른 코드" 로 보여 FK 위반
  혹은 일관성 문제를 일으킨다.

  본 마이그레이션은 그 잔존 레코드를 일괄 대문자화한다. P7-1 검수보고서 §7 의
  "소문자 기 생성분 호환" 항목을 해결한다.

정책:
  - `document_types.type_code` 와 `extraction_schemas.doc_type_code` 는
    `extraction_schemas_doc_type_code_fkey` (ON DELETE RESTRICT) 로 연결된다.
    부모 UPDATE 시 FK 가 자동 전파되지 않으므로, 아래 순서로 일관성 확보:
      (1) 대문자 변환 시 이미 대문자 레코드와 충돌하는지 감지 → 충돌 시 abort.
      (2) FK 제약 DROP.
      (3) 부모 UPDATE (document_types.type_code).
      (4) 자식 UPDATE (extraction_schemas.doc_type_code).
      (5) FK 재생성 (동일 옵션: ON DELETE RESTRICT).
  - 모든 UPDATE 는 `WHERE type_code <> UPPER(...)` 로 idempotent.

충돌 감지:
  예) `contract` 와 `CONTRACT` 가 모두 존재 → UPPER 변환 시 PK 중복.
  이 경우 RuntimeError 를 던져 마이그레이션을 중단하고, 운영팀이 수동으로
  둘 중 하나를 삭제/병합한 뒤 재실행하도록 한다.

다운그레이드:
  대문자 → 소문자/혼합으로 되돌릴 수 없다 (정보 손실). `downgrade()` 는
  no-op. 데이터 롤백이 필요하면 백업 스냅샷에서 복구.

실행 요건:
  backend 디렉터리에서 OWNER 권한 DB 유저로 `alembic upgrade head`.
  DDL 이 포함되므로 짧은 테이블 락이 걸린다 (FK drop/create).
  운영 DB 는 off-peak 시간 권장. 데이터 볼륨은 통상 적어 < 1초 내 완료.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic
revision = "p7_2_c_uppercase_doc_type"
down_revision = "p6_2_backfill_rollback"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

# (1) 충돌 감지: UPPER 결과 기준으로 그룹핑해 2개 이상인 키가 있으면 PK 중복.
_CONFLICT_CHECK_SQL = """
SELECT UPPER(type_code) AS upper_code,
       ARRAY_AGG(type_code ORDER BY type_code) AS original_codes,
       COUNT(*) AS n
FROM document_types
GROUP BY UPPER(type_code)
HAVING COUNT(*) > 1
"""

# (2) FK drop.
#   IF EXISTS 를 붙여 마이그레이션 재실행 시에도 실패하지 않게 한다 (upgrade
#   가 중간에 멈췄다 재개되는 운영 시나리오 대비).
_DROP_FK_SQL = """
ALTER TABLE extraction_schemas
    DROP CONSTRAINT IF EXISTS extraction_schemas_doc_type_code_fkey
"""

# (3) 부모 UPDATE.
_UPDATE_PARENT_SQL = """
UPDATE document_types
   SET type_code = UPPER(type_code)
 WHERE type_code <> UPPER(type_code)
"""

# (4) 자식 UPDATE.
_UPDATE_CHILD_SQL = """
UPDATE extraction_schemas
   SET doc_type_code = UPPER(doc_type_code)
 WHERE doc_type_code <> UPPER(doc_type_code)
"""

# (5) FK 재생성. 원래 DDL 과 동일: ON DELETE RESTRICT.
#   IF NOT EXISTS 는 ADD CONSTRAINT 에 지원되지 않아 DROP IF EXISTS 뒤에 무조건
#   CREATE 하는 패턴으로 재실행 안전성을 확보한다.
_ADD_FK_SQL = """
ALTER TABLE extraction_schemas
    ADD CONSTRAINT extraction_schemas_doc_type_code_fkey
    FOREIGN KEY (doc_type_code)
    REFERENCES document_types(type_code)
    ON DELETE RESTRICT
"""


def upgrade() -> None:
    bind = op.get_bind()

    # (1) 충돌 감지. 운영 데이터가 이미 정돈돼 있으면 결과 0 건.
    result = bind.execute(text(_CONFLICT_CHECK_SQL))
    conflicts = result.fetchall()
    if conflicts:
        # 에러 메시지에 충돌 샘플을 그대로 동봉해 운영팀이 바로 조치할 수 있게 함.
        # 실제 운영 로그에는 이 메시지가 그대로 남는다.
        msg_lines = [
            "document_types.type_code 를 대문자로 정규화하려 했으나 "
            "UPPER() 결과가 같은 행이 복수 존재합니다. 수동 병합 후 재실행하세요."
        ]
        for row in conflicts:
            upper_code = row[0]
            originals = row[1]
            msg_lines.append(f"  - UPPER='{upper_code}' 원본={originals}")
        raise RuntimeError("\n".join(msg_lines))

    # (2) FK 임시 해제.
    op.execute(_DROP_FK_SQL)

    try:
        # (3) 부모 먼저 UPDATE.
        op.execute(_UPDATE_PARENT_SQL)
        # (4) 자식 UPDATE.
        op.execute(_UPDATE_CHILD_SQL)
    finally:
        # (5) 실패 여부와 관계없이 FK 를 재생성해 참조 무결성 보장.
        #     내부 UPDATE 가 실패해 롤백되는 경우에도 FK 재생성은 DROP 이전
        #     상태로 복원되도록 별도 트랜잭션에서 수행해야 하지만, Alembic 의
        #     단일 트랜잭션 정책에 맞추어 finally 에서 수행하고 예외는 그대로
        #     상위로 전파한다 (psycopg2 는 실패 시 롤백 + FK 도 원상 복귀).
        op.execute(_ADD_FK_SQL)


def downgrade() -> None:
    # 대문자 → 소문자 역변환은 정보 손실 (어떤 케이스가 원본이었는지 모름).
    # 데이터 롤백이 필요한 경우 백업 스냅샷에서 복구. 이 함수는 no-op.
    pass
