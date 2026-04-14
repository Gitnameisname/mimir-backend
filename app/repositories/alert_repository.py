"""Alert rules & history persistence repository (Phase 14-13).

책임:
  - alert_rules CRUD
  - alert_history 조회/INSERT/상태 전이 (firing → resolved/acknowledged)
  - 동일 규칙 중복 firing 방지 (DB unique index + 명시적 존재 확인)

모든 SQL 은 `%s` 파라미터 바인딩을 사용한다 (f-string 미사용).
JSONB 필드 (condition, channels, channel_config, notified_channels) 는
`json.dumps(...) ::jsonb` 캐스트로 저장한다.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import psycopg2.extensions

logger = logging.getLogger(__name__)


def _row_to_rule(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row.get("description"),
        "metric_name": row["metric_name"],
        "condition": row["condition"],
        "severity": row["severity"],
        "channels": row.get("channels") or [],
        "channel_config": row.get("channel_config") or {},
        "enabled": bool(row["enabled"]),
        "created_by": str(row["created_by"]) if row.get("created_by") else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_history(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "rule_id": str(row["rule_id"]),
        "rule_name": row.get("rule_name"),
        "severity": row.get("severity"),
        "triggered_at": row["triggered_at"],
        "resolved_at": row.get("resolved_at"),
        "acknowledged_at": row.get("acknowledged_at"),
        "acknowledged_by": str(row["acknowledged_by"]) if row.get("acknowledged_by") else None,
        "status": row["status"],
        "metric_value": float(row["metric_value"]) if row.get("metric_value") is not None else None,
        "message": row.get("message"),
        "notified_channels": row.get("notified_channels") or [],
    }


class AlertRepository:
    """alert_rules / alert_history 를 추상화한 레포지토리."""

    # ---------------------------------------------------------------
    # Rules — CRUD
    # ---------------------------------------------------------------

    def list_rules(
        self,
        conn: psycopg2.extensions.connection,
        *,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT id, name, description, metric_name, condition, severity,
                   channels, channel_config, enabled, created_by, created_at, updated_at
            FROM alert_rules
        """
        params: tuple[Any, ...] = ()
        if enabled_only:
            sql += " WHERE enabled = TRUE"
        sql += " ORDER BY created_at DESC"
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [_row_to_rule(r) for r in cur.fetchall()]

    def get_rule(
        self, conn: psycopg2.extensions.connection, rule_id: str
    ) -> Optional[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, description, metric_name, condition, severity,
                       channels, channel_config, enabled, created_by, created_at, updated_at
                FROM alert_rules WHERE id = %s
                """,
                (rule_id,),
            )
            row = cur.fetchone()
            return _row_to_rule(row) if row else None

    def create_rule(
        self,
        conn: psycopg2.extensions.connection,
        *,
        name: str,
        description: Optional[str],
        metric_name: str,
        condition: dict[str, Any],
        severity: str,
        channels: list[str],
        channel_config: dict[str, Any],
        enabled: bool,
        created_by: Optional[str],
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alert_rules
                    (name, description, metric_name, condition, severity,
                     channels, channel_config, enabled, created_by)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s, %s)
                RETURNING id, name, description, metric_name, condition, severity,
                          channels, channel_config, enabled, created_by, created_at, updated_at
                """,
                (
                    name, description, metric_name,
                    json.dumps(condition), severity,
                    json.dumps(channels), json.dumps(channel_config),
                    enabled, created_by,
                ),
            )
            return _row_to_rule(cur.fetchone())

    def update_rule(
        self,
        conn: psycopg2.extensions.connection,
        rule_id: str,
        fields: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """허용된 필드만 업데이트한다 (SQL injection 방어)."""
        allowed = {
            "name", "description", "metric_name", "condition",
            "severity", "channels", "channel_config", "enabled",
        }
        jsonb_cols = {"condition", "channels", "channel_config"}

        set_parts: list[str] = []
        params: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in jsonb_cols:
                set_parts.append(f"{k} = %s::jsonb")
                params.append(json.dumps(v))
            else:
                set_parts.append(f"{k} = %s")
                params.append(v)
        if not set_parts:
            return self.get_rule(conn, rule_id)

        set_parts.append("updated_at = NOW()")
        params.append(rule_id)

        sql = f"""
            UPDATE alert_rules SET {', '.join(set_parts)}
            WHERE id = %s
            RETURNING id, name, description, metric_name, condition, severity,
                      channels, channel_config, enabled, created_by, created_at, updated_at
        """
        # NOTE: f-string 사용은 컬럼명 화이트리스트(allowed) 검증 후에만 허용.
        # 사용자 입력값은 위 params 로만 전달됨.
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            return _row_to_rule(row) if row else None

    def delete_rule(
        self, conn: psycopg2.extensions.connection, rule_id: str
    ) -> bool:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM alert_rules WHERE id = %s", (rule_id,))
            return cur.rowcount > 0

    # ---------------------------------------------------------------
    # History
    # ---------------------------------------------------------------

    def list_history(
        self,
        conn: psycopg2.extensions.connection,
        *,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """이력 조회. 반환: (items, total)"""
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("h.status = %s")
            params.append(status)
        if severity:
            where.append("r.severity = %s")
            params.append(severity)
        if from_ts:
            where.append("h.triggered_at >= %s")
            params.append(from_ts)
        if to_ts:
            where.append("h.triggered_at <= %s")
            params.append(to_ts)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)::int AS total
                FROM alert_history h
                JOIN alert_rules r ON r.id = h.rule_id
                {where_sql}
                """,
                tuple(params),
            )
            total = (cur.fetchone() or {}).get("total", 0)

            cur.execute(
                f"""
                SELECT h.id, h.rule_id, h.triggered_at, h.resolved_at,
                       h.acknowledged_at, h.acknowledged_by,
                       h.status, h.metric_value, h.message, h.notified_channels,
                       r.name AS rule_name, r.severity AS severity
                FROM alert_history h
                JOIN alert_rules r ON r.id = h.rule_id
                {where_sql}
                ORDER BY h.triggered_at DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params) + (int(limit), int(offset)),
            )
            rows = cur.fetchall()
        return [_row_to_history(r) for r in rows], total

    def get_firing_history(
        self, conn: psycopg2.extensions.connection, rule_id: str
    ) -> Optional[dict[str, Any]]:
        """해당 규칙의 현재 firing 상태 이력 (없으면 None)."""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT h.id, h.rule_id, h.triggered_at, h.resolved_at,
                       h.acknowledged_at, h.acknowledged_by,
                       h.status, h.metric_value, h.message, h.notified_channels,
                       r.name AS rule_name, r.severity AS severity
                FROM alert_history h
                JOIN alert_rules r ON r.id = h.rule_id
                WHERE h.rule_id = %s AND h.status = 'firing'
                LIMIT 1
                """,
                (rule_id,),
            )
            row = cur.fetchone()
            return _row_to_history(row) if row else None

    def insert_firing(
        self,
        conn: psycopg2.extensions.connection,
        *,
        rule_id: str,
        metric_value: float,
        message: str,
        notified_channels: list[str],
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alert_history (rule_id, status, metric_value, message, notified_channels)
                VALUES (%s, 'firing', %s, %s, %s::jsonb)
                RETURNING id, rule_id, triggered_at, resolved_at, acknowledged_at, acknowledged_by,
                          status, metric_value, message, notified_channels
                """,
                (rule_id, metric_value, message, json.dumps(notified_channels)),
            )
            return _row_to_history(cur.fetchone())

    def resolve_firing(
        self, conn: psycopg2.extensions.connection, rule_id: str
    ) -> Optional[dict[str, Any]]:
        """firing → resolved 전이. 없으면 None."""
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE alert_history
                SET status = 'resolved', resolved_at = NOW()
                WHERE rule_id = %s AND status = 'firing'
                RETURNING id, rule_id, triggered_at, resolved_at, acknowledged_at, acknowledged_by,
                          status, metric_value, message, notified_channels
                """,
                (rule_id,),
            )
            row = cur.fetchone()
            return _row_to_history(row) if row else None

    def acknowledge(
        self,
        conn: psycopg2.extensions.connection,
        history_id: str,
        actor_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE alert_history
                SET acknowledged_at = NOW(), acknowledged_by = %s
                WHERE id = %s AND acknowledged_at IS NULL
                RETURNING id, rule_id, triggered_at, resolved_at, acknowledged_at, acknowledged_by,
                          status, metric_value, message, notified_channels
                """,
                (actor_id, history_id),
            )
            row = cur.fetchone()
            return _row_to_history(row) if row else None


alert_repository = AlertRepository()
