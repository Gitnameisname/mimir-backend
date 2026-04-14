"""Alert evaluator (Phase 14-13).

활성(enabled) 규칙 전부 순회 → 메트릭 조회 → 조건 판단 → firing 전이/해소.

보안 원칙:
  - `eval()` / `exec()` 사용 금지
  - 연산자는 화이트리스트 dict으로만 분기
  - 메트릭 조회 SQL 은 파라미터 바인딩
"""

from __future__ import annotations

import logging
import operator
from typing import Any, Callable

from app.db.connection import get_db
from app.repositories.alert_repository import alert_repository
from app.services.alert_notifier import alert_notifier

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────
# 1. 안전한 비교 연산자 화이트리스트 — eval() 대체
# ───────────────────────────────────────────────────────────────────
_OPS: dict[str, Callable[[float, float], bool]] = {
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "eq": operator.eq,
    "ne": operator.ne,
}


def evaluate_condition(value: float, condition: dict[str, Any]) -> bool:
    """주어진 metric 값이 condition 을 만족하면 True."""
    op = condition.get("operator")
    threshold = condition.get("threshold")
    if op not in _OPS:
        logger.warning("알 수 없는 operator: %s", op)
        return False
    try:
        return _OPS[op](float(value), float(threshold))
    except (TypeError, ValueError):
        logger.warning("condition 평가 실패: value=%r, threshold=%r", value, threshold)
        return False


# ───────────────────────────────────────────────────────────────────
# 2. 메트릭 조회 — 화이트리스트 dict
# ───────────────────────────────────────────────────────────────────
_METRIC_LABELS: dict[str, str] = {
    "api.response_time_p95": "API 응답 시간 P95 (ms)",
    "api.error_rate_5xx": "5xx 에러율 (%)",
    "db.connection_pool_usage": "DB 커넥션 풀 사용률 (%)",
    "valkey.memory_usage": "Valkey 메모리 사용률 (%)",
    "job.queue_length": "작업 대기열 길이 (건)",
    "search.index_lag": "검색 인덱스 지연 (초)",
}


def list_supported_metrics() -> list[dict[str, str]]:
    return [{"name": k, "label": v} for k, v in _METRIC_LABELS.items()]


def _fetch_metric_value(metric_name: str) -> float | None:
    """메트릭 이름에 맞는 실측 값을 조회한다.

    실제 HTTP APM 미들웨어 부재 — 현재는 background_jobs/audit_events 기반 proxy.
    메트릭이 지원되지 않거나 데이터 없음 → None.
    """
    if metric_name not in _METRIC_LABELS:
        return None

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                if metric_name == "api.response_time_p95":
                    cur.execute(
                        """
                        SELECT COALESCE(
                          percentile_cont(0.95) WITHIN GROUP (
                            ORDER BY EXTRACT(EPOCH FROM (ended_at - started_at)) * 1000
                          ), 0
                        )::numeric AS v
                        FROM background_jobs
                        WHERE ended_at IS NOT NULL
                          AND started_at IS NOT NULL
                          AND ended_at > NOW() - INTERVAL '5 minutes'
                        """
                    )
                elif metric_name == "api.error_rate_5xx":
                    cur.execute(
                        """
                        WITH totals AS (
                          SELECT
                            COUNT(*) FILTER (WHERE action_result IS NOT NULL)::numeric AS total,
                            COUNT(*) FILTER (WHERE action_result = 'failure')::numeric AS failed
                          FROM audit_events
                          WHERE occurred_at > NOW() - INTERVAL '5 minutes'
                        )
                        SELECT CASE WHEN total = 0 THEN 0 ELSE (failed * 100.0 / total) END AS v
                        FROM totals
                        """
                    )
                elif metric_name == "db.connection_pool_usage":
                    cur.execute(
                        """
                        SELECT CASE WHEN setting::int = 0 THEN 0
                               ELSE (count(pid)::numeric * 100.0 / setting::int)
                               END AS v
                        FROM pg_stat_activity, pg_settings
                        WHERE pg_settings.name = 'max_connections'
                        GROUP BY setting
                        """
                    )
                elif metric_name == "valkey.memory_usage":
                    # Valkey 는 info() 사용 — 여기선 proxy 로 0 반환 (별도 구현 필요)
                    return 0.0
                elif metric_name == "job.queue_length":
                    cur.execute(
                        """
                        SELECT COUNT(*)::numeric AS v
                        FROM background_jobs
                        WHERE status IN ('PENDING','RUNNING')
                        """
                    )
                elif metric_name == "search.index_lag":
                    cur.execute(
                        """
                        SELECT COALESCE(
                          EXTRACT(EPOCH FROM (NOW() - MAX(created_at))),
                          0
                        )::numeric AS v
                        FROM document_chunks
                        WHERE has_embedding = FALSE
                        """
                    )
                else:
                    return None

                row = cur.fetchone()
                if not row:
                    return 0.0
                return float(row["v"] or 0)
    except Exception as exc:
        logger.warning("메트릭 조회 실패 [%s]: %s", metric_name, exc)
        return None


# ───────────────────────────────────────────────────────────────────
# 3. 규칙 평가 루프
# ───────────────────────────────────────────────────────────────────
class AlertEvaluator:
    def evaluate_all(self) -> dict[str, int]:
        """전체 활성 규칙 평가. 반환: 통계."""
        stats = {"evaluated": 0, "fired": 0, "resolved": 0, "skipped": 0}
        with get_db() as conn:
            rules = alert_repository.list_rules(conn, enabled_only=True)

        for rule in rules:
            stats["evaluated"] += 1
            value = _fetch_metric_value(rule["metric_name"])
            if value is None:
                stats["skipped"] += 1
                continue

            try:
                if evaluate_condition(value, rule["condition"]):
                    if self._fire_if_new(rule, value):
                        stats["fired"] += 1
                else:
                    if self._resolve_if_firing(rule):
                        stats["resolved"] += 1
            except Exception as exc:
                logger.exception("규칙 평가 실패 [%s]: %s", rule["id"], exc)
                stats["skipped"] += 1

        return stats

    def _fire_if_new(self, rule: dict[str, Any], value: float) -> bool:
        """이미 firing 이면 아무 것도 하지 않고 False. 신규면 INSERT + notify."""
        with get_db() as conn:
            existing = alert_repository.get_firing_history(conn, rule["id"])
            if existing:
                return False

            message = (
                f"{rule['name']}: {rule['metric_name']}={value} "
                f"(조건: {rule['condition'].get('operator')} "
                f"{rule['condition'].get('threshold')})"
            )
            notified = alert_notifier.notify(rule, metric_value=value, message=message)
            alert_repository.insert_firing(
                conn,
                rule_id=rule["id"],
                metric_value=value,
                message=message,
                notified_channels=notified,
            )
            conn.commit()
            return True

    def _resolve_if_firing(self, rule: dict[str, Any]) -> bool:
        with get_db() as conn:
            resolved = alert_repository.resolve_firing(conn, rule["id"])
            if resolved:
                conn.commit()
                return True
        return False


alert_evaluator = AlertEvaluator()
