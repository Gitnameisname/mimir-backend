"""Admin API organization_id 격리 가드 — S3 Phase 6 FG 6-4 (2026-05-18).

배경 (Phase 6 §1.2 R-O4):
  admin 이 다른 조직의 자원 (scope_profile / agent / user) 을 변경 가능한 잠재
  위험 (FG 3-2 §3 T5) 을 일괄 차단. ORG_ADMIN 은 본인 조직 자원만, SUPER_ADMIN
  만 조직 횡단 허용 (단, audit_events 로 별 event_type emit).

제공 함수:
  - :func:`actor_org_ids`           — actor 가 ORG_ADMIN 으로 속한 organization id 집합.
  - :func:`is_super_admin`          — SUPER_ADMIN role 여부.
  - :func:`ensure_actor_can_access_org` — actor 가 ``target_org_id`` 자원에 접근 가능한지
    검증. 위반 시 ``ApiPermissionDeniedError``. SUPER_ADMIN 횡단은 audit emit.

설계 원칙:
  - **fail-closed** — target_org_id 가 None 또는 조회 불가 시 SUPER_ADMIN 만 통과.
  - **READ vs WRITE 별 메시지** — 보안 영향 차등이지만 둘 다 거부.
  - **단일 진입점** — admin.py / scope_profiles.py / agents 등이 모두 본 helper 호출.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.api.auth.models import ActorContext
from app.api.errors.exceptions import ApiPermissionDeniedError
from app.audit.emitter import audit_emitter

logger = logging.getLogger(__name__)

__all__ = [
    "actor_org_ids",
    "is_super_admin",
    "ensure_actor_can_access_org",
]


def is_super_admin(actor: ActorContext | None) -> bool:
    if actor is None:
        return False
    return bool(actor.is_authenticated) and actor.role == "SUPER_ADMIN"


def actor_org_ids(
    conn,
    actor: ActorContext | None,
    *,
    role_filter: Optional[frozenset[str]] = None,
) -> frozenset[str]:
    """actor 가 (선택 role 로) 속한 organization id 집합.

    :param conn: psycopg2 connection.
    :param actor: ``ActorContext`` 또는 None.
    :param role_filter: 특정 role 만 카운트 (예: ``frozenset({"ORG_ADMIN"})``). None
        이면 모든 role.
    :returns: organization id 문자열 frozenset. actor 가 None / 미인증 / actor_id 부재
        시 빈 set.
    """
    if actor is None or not actor.is_authenticated or not actor.actor_id:
        return frozenset()
    sql = "SELECT org_id FROM user_org_roles WHERE user_id = %s"
    params: list = [actor.actor_id]
    if role_filter:
        sql += " AND role_name = ANY(%s)"
        params.append(list(role_filter))
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    except Exception as exc:
        logger.warning("admin_org_guard: actor_org_ids query failed: %s", exc)
        return frozenset()
    ids: set[str] = set()
    for row in rows or []:
        if isinstance(row, dict):
            val = row.get("org_id")
        else:
            val = row[0]
        if val is not None:
            ids.add(str(val))
    return frozenset(ids)


def ensure_actor_can_access_org(
    conn,
    actor: ActorContext | None,
    *,
    target_org_id: Optional[str],
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
) -> None:
    """target_org_id 자원에 대해 actor 가 접근 가능한지 검증.

    분기:
      - SUPER_ADMIN  → 항상 통과. ``admin.cross_org_access`` audit emit.
      - target_org_id == None → SUPER_ADMIN 만 통과. 그 외는 거부 (자원 소속 불명
        → fail-closed).
      - ORG_ADMIN 이고 ``target_org_id`` 가 본인 ORG_ADMIN 소속 org 에 포함 → 통과.
      - 외 모든 경우 → ``ApiPermissionDeniedError``.

    :raises ApiPermissionDeniedError: 위반 시.
    """
    if actor is None or not actor.is_authenticated:
        raise ApiPermissionDeniedError("관리자 권한이 필요합니다.")

    if is_super_admin(actor):
        # 별 event_type 으로 cross-org 접근 기록 (R-O4 의무).
        try:
            audit_emitter.emit(
                event_type="admin.cross_org_access",
                action=action,
                actor_id=actor.actor_id,
                actor_type=actor.audit_actor_type,
                resource_type=resource_type,
                resource_id=resource_id,
                result="success",
                metadata={
                    "target_org_id": target_org_id,
                    "actor_role": actor.role,
                },
            )
        except Exception as exc:
            logger.warning("admin_org_guard: cross_org audit emit failed: %s", exc)
        return

    # 비 SUPER_ADMIN — target_org_id 가 분명해야 함.
    if target_org_id is None:
        raise ApiPermissionDeniedError(
            "자원의 조직 정보를 확인할 수 없어 접근이 거부되었습니다."
        )

    allowed = actor_org_ids(conn, actor, role_filter=frozenset({"ORG_ADMIN", "SUPER_ADMIN"}))
    if str(target_org_id) in allowed:
        return

    raise ApiPermissionDeniedError(
        "다른 조직의 자원은 변경할 수 없습니다."
    )
