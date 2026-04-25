"""
Idempotency persistence repository.

책임:
  - idempotency_records 테이블 CRUD
  - DB row → IdempotencyRecord 변환
  - 고유 키(idempotency_key + actor_id + resource_action) 조회

설계:
  - UNIQUE (idempotency_key, actor_id, resource_action)로 중복 방지
  - actor_id가 None인 경우 'anonymous'를 sentinel 값으로 사용
"""

import json
import logging
from typing import Any, Optional

import psycopg2.extensions

from app.models.idempotency_record import IdempotencyRecord
from app.utils.json_utils import dumps_ko

logger = logging.getLogger(__name__)

# actor_id가 None인 anonymous 요청의 sentinel 값
_ANONYMOUS_ACTOR = "anonymous"


def _actor_key(actor_id: Optional[str]) -> str:
    return actor_id if actor_id else _ANONYMOUS_ACTOR


def _row_to_record(row: dict[str, Any]) -> IdempotencyRecord:
    actor_id = row.get("actor_id")
    if actor_id == _ANONYMOUS_ACTOR:
        actor_id = None
    return IdempotencyRecord(
        id=str(row["id"]),
        idempotency_key=row["idempotency_key"],
        actor_id=actor_id,
        resource_action=row["resource_action"],
        request_fingerprint=row["request_fingerprint"],
        status=row["status"],
        response_status_code=row.get("response_status_code"),
        response_body=row.get("response_body"),
        resource_id=row.get("resource_id"),
        request_id=row.get("request_id"),
        trace_id=row.get("trace_id"),
        tenant_id=row.get("tenant_id"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row.get("expires_at"),
    )


class IdempotencyRepository:
    """Idempotency records 테이블 접근 repository."""

    def get(
        self,
        conn: psycopg2.extensions.connection,
        idempotency_key: str,
        actor_id: Optional[str],
        resource_action: str,
    ) -> Optional[IdempotencyRecord]:
        """(key, actor_id, action) 조합으로 record를 조회한다. 없으면 None."""
        sql = """
            SELECT id, idempotency_key, actor_id, resource_action, request_fingerprint,
                   status, response_status_code, response_body, resource_id,
                   request_id, trace_id, tenant_id, created_at, updated_at, expires_at
            FROM idempotency_records
            WHERE idempotency_key = %s
              AND actor_id = %s
              AND resource_action = %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (idempotency_key, _actor_key(actor_id), resource_action))
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_record(dict(row))

    def create_in_progress(
        self,
        conn: psycopg2.extensions.connection,
        idempotency_key: str,
        actor_id: Optional[str],
        resource_action: str,
        request_fingerprint: str,
        *,
        request_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> IdempotencyRecord:
        """새 in_progress record를 생성한다."""
        sql = """
            INSERT INTO idempotency_records
                (idempotency_key, actor_id, resource_action, request_fingerprint,
                 status, request_id, trace_id, tenant_id)
            VALUES (%s, %s, %s, %s, 'in_progress', %s, %s, %s)
            RETURNING
                id, idempotency_key, actor_id, resource_action, request_fingerprint,
                status, response_status_code, response_body, resource_id,
                request_id, trace_id, tenant_id, created_at, updated_at, expires_at
        """
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    idempotency_key,
                    _actor_key(actor_id),
                    resource_action,
                    request_fingerprint,
                    request_id,
                    trace_id,
                    tenant_id,
                ),
            )
            row = cur.fetchone()
        return _row_to_record(dict(row))

    def mark_completed(
        self,
        conn: psycopg2.extensions.connection,
        idempotency_key: str,
        actor_id: Optional[str],
        resource_action: str,
        *,
        response_status_code: int,
        response_body: dict[str, Any],
        resource_id: Optional[str] = None,
    ) -> None:
        """record를 completed 상태로 갱신하고 response snapshot을 저장한다."""
        sql = """
            UPDATE idempotency_records
            SET status = 'completed',
                response_status_code = %s,
                response_body = %s,
                resource_id = %s,
                updated_at = NOW()
            WHERE idempotency_key = %s
              AND actor_id = %s
              AND resource_action = %s
        """
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    response_status_code,
                    dumps_ko(response_body, default=str),
                    resource_id,
                    idempotency_key,
                    _actor_key(actor_id),
                    resource_action,
                ),
            )

    def mark_failed(
        self,
        conn: psycopg2.extensions.connection,
        idempotency_key: str,
        actor_id: Optional[str],
        resource_action: str,
    ) -> None:
        """record를 failed 상태로 갱신한다."""
        sql = """
            UPDATE idempotency_records
            SET status = 'failed',
                updated_at = NOW()
            WHERE idempotency_key = %s
              AND actor_id = %s
              AND resource_action = %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (idempotency_key, _actor_key(actor_id), resource_action))


# 모듈 수준 싱글턴
idempotency_repository = IdempotencyRepository()
