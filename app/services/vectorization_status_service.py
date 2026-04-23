"""
S3 Phase 0 / FG 0-5 — 문서별 벡터화 상태 판정.

판정 규칙 (task0-5 §2.3):
  - not_applicable : 발행된 버전 없음
  - pending        : 발행됐으나 document_chunks 0건 + 실패 기록 없음
  - failed         : 최근 실패 audit_event 존재 (last_vectorized_at 이후 발생)
  - stale          : latest_published_version_id != any indexed_version_id
  - indexed        : 위 외 정상 (최신 published 가 색인됨)
  - in_progress    : 본 구현에서는 transient marker 없이 판정 불가 — pending 으로 수렴

모든 쿼리는 read-only + 단일 DB connection (get_db). Milvus 에는 접근하지 않는다 (폐쇄망 ⑦).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)


VectorizationStatus = Literal[
    "indexed",
    "pending",
    "in_progress",
    "failed",
    "stale",
    "not_applicable",
]


@dataclass
class VectorizationStatusInfo:
    document_id: str
    status: VectorizationStatus
    latest_published_version_id: Optional[str]
    indexed_version_id: Optional[str]          # 가장 최근에 색인된 버전 (여러개면 최신)
    chunk_count: int
    last_vectorized_at: Optional[datetime]
    last_error: Optional[str]                   # 최근 실패 요약 (있을 때만)
    can_reindex: bool = False
    reindex_cooldown_sec: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "status": self.status,
            "latest_published_version_id": self.latest_published_version_id,
            "indexed_version_id": self.indexed_version_id,
            "chunk_count": self.chunk_count,
            "last_vectorized_at": self.last_vectorized_at.isoformat() if self.last_vectorized_at else None,
            "last_error": self.last_error,
            "can_reindex": self.can_reindex,
            "reindex_cooldown_sec": self.reindex_cooldown_sec,
        }


# --------------------------------------------------------------------------- #
# DB 조회 — 문서 기본 정보
# --------------------------------------------------------------------------- #


def _fetch_document_meta(conn, document_id: str) -> Optional[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, current_published_version_id, created_by
            FROM documents
            WHERE id = %s::uuid
            """,
            (document_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    # RealDictCursor / tuple 양쪽 대응
    if isinstance(row, dict):
        return dict(row)
    return {"id": row[0], "current_published_version_id": row[1], "created_by": row[2]}


def _fetch_index_state(conn, document_id: str) -> tuple[set[str], int, Optional[datetime]]:
    """document_chunks 에서 is_current=TRUE 인 청크를 집계한다.

    Returns:
        (indexed_version_ids, chunk_count, last_vectorized_at)
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                ARRAY_AGG(DISTINCT version_id::text) AS version_ids,
                COUNT(*) AS chunk_count,
                MAX(COALESCE(updated_at, created_at)) AS last_at
            FROM document_chunks
            WHERE document_id = %s::uuid AND is_current = TRUE
            """,
            (document_id,),
        )
        row = cur.fetchone()
    if not row:
        return set(), 0, None
    raw = row if isinstance(row, dict) else {"version_ids": row[0], "chunk_count": row[1], "last_at": row[2]}
    vids = {v for v in (raw.get("version_ids") or []) if v}
    count = int(raw.get("chunk_count") or 0)
    last_at = raw.get("last_at")
    return vids, count, last_at


_VECTORIZATION_EVENT_FAILURE_PATTERN = "vectoriz"


def _fetch_recent_failure(
    conn,
    document_id: str,
    *,
    after: Optional[datetime] = None,
    lookback_hours: int = 24,
) -> Optional[dict[str, Any]]:
    """audit_events 에서 최근 실패한 벡터화 이벤트를 조회.

    after 가 주어지면 그 시점 이후만. 없으면 lookback_hours 기본 24시간.

    반환: `{"occurred_at": datetime, "reason": str}` 또는 None.
    """
    sql_parts = [
        "SELECT occurred_at, reason FROM audit_events",
        "WHERE document_id = %s::uuid",
        "  AND (action_result <> 'success')",
        "  AND (LOWER(event_type) LIKE %s)",
    ]
    params: list[Any] = [document_id, f"%{_VECTORIZATION_EVENT_FAILURE_PATTERN}%"]
    if after is not None:
        sql_parts.append("  AND occurred_at > %s")
        params.append(after)
    else:
        sql_parts.append(
            f"  AND occurred_at > NOW() - INTERVAL '{int(lookback_hours)} hours'"
        )
    sql_parts.append("ORDER BY occurred_at DESC LIMIT 1")
    sql = "\n".join(sql_parts)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    except Exception as exc:
        # audit_events 스키마가 다르거나 부재한 테스트 환경에서도 조용히 fallback.
        logger.debug("fetch_recent_failure failed: %s", exc)
        return None
    if not row:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {"occurred_at": row[0], "reason": row[1]}


# --------------------------------------------------------------------------- #
# 권한 판정 — Admin 또는 작성자(created_by == actor.user_id)
# --------------------------------------------------------------------------- #


_ADMIN_ROLES: frozenset[str] = frozenset({"ADMIN", "SUPER_ADMIN", "ORG_ADMIN"})


def can_user_reindex(
    *,
    actor_user_id: Optional[str],
    actor_role: Optional[str],
    document_created_by: Optional[str],
) -> bool:
    """FG 0-5: Admin 이거나 문서 작성자면 재벡터화 가능."""
    if actor_role and actor_role.upper() in _ADMIN_ROLES:
        return True
    if actor_user_id and document_created_by and str(actor_user_id) == str(document_created_by):
        return True
    return False


# --------------------------------------------------------------------------- #
# 공용 진입점 — 상태 판정
# --------------------------------------------------------------------------- #


def get_vectorization_status(
    conn,
    document_id: str,
    *,
    actor_user_id: Optional[str] = None,
    actor_role: Optional[str] = None,
    cooldown_remaining_sec: int = 0,
) -> Optional[VectorizationStatusInfo]:
    """문서의 벡터화 상태 전체를 조회.

    - 문서 부재 시 None 반환 (라우터에서 404 처리)
    - 실패 기록은 `last_vectorized_at` 이후에 발생한 것만 `failed` 판정
    - `can_reindex` 는 actor 정보 기반 판정. cooldown 은 외부에서 주입.
    """
    meta = _fetch_document_meta(conn, document_id)
    if not meta:
        return None

    latest = meta.get("current_published_version_id")
    latest_str: Optional[str] = str(latest) if latest else None
    created_by = meta.get("created_by")

    indexed_ids, chunk_count, last_at = _fetch_index_state(conn, document_id)
    most_recent_indexed: Optional[str] = None
    if indexed_ids:
        # 'latest' 가 색인됐다면 그것 우선, 아니면 임의의 indexed 하나 (stale 시 참고)
        if latest_str and latest_str in indexed_ids:
            most_recent_indexed = latest_str
        else:
            most_recent_indexed = next(iter(indexed_ids))

    failure = _fetch_recent_failure(conn, document_id, after=last_at)

    # 상태 판정
    status: VectorizationStatus
    last_error: Optional[str] = None

    if latest_str is None:
        status = "not_applicable"
    elif failure is not None:
        status = "failed"
        reason = failure.get("reason") or "vectorization failed (no reason)"
        last_error = str(reason)[:300]
    elif not indexed_ids:
        status = "pending"
    elif latest_str not in indexed_ids:
        status = "stale"
    else:
        status = "indexed"

    can_reindex = can_user_reindex(
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        document_created_by=created_by,
    )

    return VectorizationStatusInfo(
        document_id=document_id,
        status=status,
        latest_published_version_id=latest_str,
        indexed_version_id=most_recent_indexed,
        chunk_count=chunk_count,
        last_vectorized_at=last_at,
        last_error=last_error,
        can_reindex=can_reindex,
        reindex_cooldown_sec=max(0, int(cooldown_remaining_sec)),
    )
