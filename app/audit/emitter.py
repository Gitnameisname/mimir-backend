"""
AuditEmitter — 감사 이벤트 emit interface.

역할:
  - 플랫폼 내 "누가 무엇을 했는지"를 audit_events 테이블에 기록한다.
  - 구조화 로그도 병행 출력 (운영 모니터링용).

DB 저장 전략:
  - emit()은 메인 트랜잭션과 독립된 별도 커넥션을 사용한다.
  - 메인 요청이 성공한 후 호출되므로, 감사 이벤트는 항상 성공 케이스만 기록.
  - DB 저장 실패 시 로그만 남기고 요청은 계속 진행한다 (non-blocking).

audit_events 컬럼 매핑:
  - resource_type="document" → document_id = resource_id
  - resource_type="version"  → version_id = resource_id,
                                document_id = metadata["document_id"]
  - target_version_id / previous_state / new_state는 선택 파라미터로 수신

안전 원칙:
  - raw auth token, 전체 request body, 민감 metadata는 절대 포함 안 함.
  - actor_id (정규화 식별자), resource_id, action, result만 기록.

폐쇄망 정책 (S2 ⑦, R-D1 2026-04-25 명문화):
  - **현재 외부 sink 0건**: emitter 가 호출하는 외부 시스템은 PostgreSQL
    (내부 인프라) + Python logging 모듈 (로컬) 뿐. 폐쇄망에서 그대로 작동.
  - DB INSERT 실패 → ``_persist`` 의 try/except 가 logging fallback (이미 작동).
  - **새 외부 sink 추가 PR (예: Slack 알람 / PagerDuty webhook / 외부 SIEM)
    의 의무**:
      1. 환경변수 가드 — ``AUDIT_<SINK>_ENABLED=false`` 같은 명시적 toggle.
         off 일 때 sink 호출 자체를 skip.
      2. FileSink fallback — sink 호출 실패 (timeout / 401 / DNS) 시 logging
         으로 자동 전환. 핵심 audit 기록이 외부 sink 장애로 누락되지 않도록.
      3. 폐쇄망 회귀 테스트 — sink off / sink 실패 두 케이스 모두 emit 이
         예외를 던지지 않고 핵심 DB INSERT 가 성공함을 검증.
  - 위 의무를 누락하면 폐쇄망 환경에서 audit 누락 또는 요청 5xx 위험.
"""

import contextvars
import logging
from datetime import datetime
from typing import Any, Literal, Optional
from app.utils.time import utcnow_iso

# R9 (2026-04-25): trace_id 자동 전파용 ContextVar.
#   - RequestContextMiddleware 가 요청 진입 시 set_trace_id 호출 → 본 emitter 가
#     자동으로 trace_id 첨부 (호출자가 명시하지 않아도 됨).
#   - 본 ContextVar 가 None 이면 emit 시 trace_id 도 None — 기존 동작 유지.
#   - asyncio.Task 안전 — ContextVar 는 task-local.
_trace_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "audit_trace_id", default=None
)


def set_trace_id(trace_id: Optional[str]) -> contextvars.Token:
    """현재 컨텍스트의 trace_id 를 설정한다. 미들웨어 진입 시 호출.

    :returns: ``reset_trace_id(token)`` 에 넘길 토큰. 미들웨어 dispatch 종료 시
        호출해 contextvar 를 이전 값으로 복원 (재진입 안전).
    """
    return _trace_id_var.set(trace_id)


def reset_trace_id(token: contextvars.Token) -> None:
    """``set_trace_id`` 가 반환한 토큰으로 contextvar 를 복원한다."""
    _trace_id_var.reset(token)


def current_trace_id() -> Optional[str]:
    """현재 contextvar 의 trace_id 를 읽는다 (테스트·로깅 보조)."""
    return _trace_id_var.get()

# F-07 시정(2026-04-18): actor_type 을 타입 레벨에서 강제.
#   S2 원칙 ⑥ "AI 에이전트는 사람과 동등한 API 소비자" 에 따라 모든 감사 이벤트는
#   actor_type 을 반드시 기록해야 한다. 기존 Optional[str] 은 런타임 누락을 허용하여
#   감사 누수의 원인이 되었으므로 Literal["user", "agent", "system"] 로 제약한다.
#   - "user"   : 사람(End-user / Admin / Publisher 등)
#   - "agent"  : LLM / MCP / 외부 서비스 계정
#   - "system" : 스케줄러, 배치 잡, 서버 자체가 주체인 기록(만료 정리 등)
ActorType = Literal["user", "agent", "system"]

_audit_logger = logging.getLogger("mimir.audit")


class AuditEmitter:
    """감사 이벤트를 emit하는 인터페이스.

    emit() 호출 시:
      1. structured log 출력
      2. audit_events 테이블 INSERT (별도 트랜잭션)
    """

    def emit(
        self,
        *,
        event_type: str,
        action: str,
        actor_id: Optional[str],
        # F-07 시정(2026-04-18): actor_type 필수화 (Literal). 기본값 제거.
        #   매 호출부는 "user" | "agent" | "system" 중 하나를 명시해야 한다.
        actor_type: ActorType,
        resource_type: str,
        resource_id: Optional[str] = None,
        result: str,
        request_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        # 확장 파라미터 (Phase 4 감사 이벤트용)
        actor_role: Optional[str] = None,
        target_version_id: Optional[str] = None,
        previous_state: Optional[str] = None,
        new_state: Optional[str] = None,
    ) -> None:
        """감사 이벤트를 emit한다.

        Args:
            event_type        : 이벤트 식별자 (예: document.created, draft.updated)
            action            : 수행된 action (예: document.create)
            actor_id          : 수행 주체 actor_id (None = anonymous)
            resource_type     : 대상 리소스 타입 (document / version / node / conversation)
            resource_id       : 대상 리소스 UUID
            result            : 결과 분류 (success / failure / denied / conflict / replayed)
            request_id        : 상관관계 추적
            trace_id          : 상관관계 추적 (로그 전용)
            tenant_id         : 테넌트 scope (로그 전용)
            metadata          : 추가 컨텍스트 — document_id / version_number 등 포함 가능
            actor_role        : 수행 주체 역할 (viewer/editor/publisher/admin)
            actor_type        : 수행 주체 종류 — user | agent (S2 원칙 ⑥)
            target_version_id : 복원 대상 버전 UUID 등 보조 버전 참조
            previous_state    : 상태 전이 전 값 (예: draft)
            new_state         : 상태 전이 후 값 (예: published)
        """
        # R9 (2026-04-25): trace_id 가 명시되지 않으면 ContextVar 에서 가져온다.
        # 미들웨어가 set_trace_id 한 값을 호출자가 매번 명시하지 않아도 자동 첨부.
        # 호출자가 명시한 trace_id 가 우선 (None 일 때만 fallback).
        effective_trace_id = trace_id if trace_id is not None else _trace_id_var.get()

        # --- 1. structured log ---
        log_event: dict[str, Any] = {
            "audit_event": event_type,
            "action": action,
            "actor_id": actor_id or "anonymous",
            # F-07: actor_type 은 필수 필드로 항상 기록.
            "actor_type": actor_type,
            "resource_type": resource_type,
            "result": result,
            "timestamp": utcnow_iso(),
        }
        if resource_id:
            log_event["resource_id"] = resource_id
        if request_id:
            log_event["request_id"] = request_id
        if effective_trace_id:
            log_event["trace_id"] = effective_trace_id
        if tenant_id:
            log_event["tenant_id"] = tenant_id
        if metadata:
            log_event["metadata"] = metadata

        _audit_logger.info("AUDIT %s", log_event)

        # --- 2. DB persistence ---
        self._persist(
            event_type=event_type,
            actor_id=actor_id,
            actor_role=actor_role,
            actor_type=actor_type,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            request_id=request_id,
            metadata=metadata,
            target_version_id=target_version_id,
            previous_state=previous_state,
            new_state=new_state,
        )

    def _persist(
        self,
        *,
        event_type: str,
        actor_id: Optional[str],
        actor_role: Optional[str],
        # F-07: actor_type 은 _persist 에서도 필수.
        actor_type: ActorType,
        resource_type: str,
        resource_id: Optional[str],
        result: str,
        request_id: Optional[str],
        metadata: Optional[dict[str, Any]],
        target_version_id: Optional[str],
        previous_state: Optional[str],
        new_state: Optional[str],
    ) -> None:
        """audit_events 테이블에 INSERT한다 (독립 트랜잭션).

        실패 시 로그만 남기고 조용히 반환 — 감사 저장 실패가 요청을 막지 않도록.
        """
        # resource_type에 따라 document_id / version_id 매핑
        document_id: Optional[str] = None
        version_id: Optional[str] = None

        if resource_type == "document":
            document_id = resource_id
        elif resource_type == "version":
            version_id = resource_id
            if metadata:
                document_id = metadata.get("document_id")

        # metadata에서 document_id 보정 (resource_type 무관)
        if document_id is None and metadata:
            document_id = metadata.get("document_id")

        sql = """
            INSERT INTO audit_events (
                event_type, actor_user_id, actor_role, actor_type,
                document_id, version_id, target_version_id,
                previous_state, new_state,
                action_result, request_id
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s
            )
        """
        params = (
            event_type,
            actor_id,
            actor_role,
            actor_type,
            document_id,
            version_id,
            target_version_id,
            previous_state,
            new_state,
            result,
            request_id,
        )

        try:
            from app.db.connection import get_db
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
        except Exception as exc:  # noqa: BLE001
            _audit_logger.error(
                "audit_persist_failed event_type=%s resource_id=%s error=%s",
                event_type, resource_id, exc,
            )


    def emit_for_actor(
        self,
        *,
        event_type: str,
        action: str,
        actor: Any,  # ActorContext — 순환 import 방지를 위해 Any 사용
        resource_type: str,
        resource_id: Optional[str] = None,
        request_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        previous_state: Optional[str] = None,
        new_state: Optional[str] = None,
        target_version_id: Optional[str] = None,
    ) -> None:
        """ActorContext를 받아 actor_id / actor_role을 자동 추출하고 emit한다.

        라우터에서 반복되는::

            audit_emitter.emit(
                event_type=..., action=...,
                actor_id=actor_id,
                actor_role=actor.role,
                resource_type=..., result="success", ...
            )

        패턴을 단순화한다.
        """
        # S2 원칙 ⑥: ActorType.SERVICE → "agent", ActorType.USER → "user".
        # F-07(2026-04-18): actor_type 이 Literal 로 강제되어 None 은 허용되지 않으므로
        #   ActorContext 가 actor_type 을 지니지 않은 드문 케이스(테스트/초기화 오류 등)는
        #   기본값 "user" 로 폴백. 운영에서는 ActorContext resolver 가 항상 지정한다.
        raw_actor_type = getattr(actor, "actor_type", None)
        if raw_actor_type is not None:
            raw_value = getattr(raw_actor_type, "value", str(raw_actor_type)).lower()
            actor_type_str: ActorType = "agent" if raw_value in ("service", "agent") else "user"
        else:
            actor_type_str = "user"

        self.emit(
            event_type=event_type,
            action=action,
            actor_id=getattr(actor, "actor_id", None) if getattr(actor, "is_authenticated", False) else None,
            actor_role=getattr(actor, "role", None),
            actor_type=actor_type_str,
            resource_type=resource_type,
            resource_id=resource_id,
            result="success",
            request_id=request_id,
            trace_id=trace_id,
            metadata=metadata,
            previous_state=previous_state,
            new_state=new_state,
            target_version_id=target_version_id,
        )


# 모듈 수준 싱글턴
audit_emitter = AuditEmitter()
