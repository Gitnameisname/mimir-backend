"""
AuditEmitter — 감사 이벤트 emit interface.

역할:
  - 플랫폼 내 "누가 무엇을 했는지"를 audit candidate로 emit한다.
  - 현재는 structured log + stub (no-op persistence) 구현.
  - 이후 DB 저장 / message bus 발행 / webhook 발행으로 확장 가능.

운영 로그와의 구분:
  - 운영 로그: 성능/오류/추적 (middleware 레벨)
  - 감사 이벤트: 누가 무엇을 했는지의 변경 추적 (write action 레벨)

audit candidate 기준:
  - document create / update
  - version create
  - authorization denied
  - idempotency conflict
  (health check, 단순 read는 포함 안 함)

안전 원칙:
  - raw auth token, 전체 request body, 민감 metadata는 절대 포함 안 함.
  - actor_id (정규화 식별자), resource_id, action, result만 기록.

TODO:
  - audit_events 테이블 persistence backend 연결
  - message bus (Kafka/Redis Streams) 발행
  - webhook delivery correlation
  - retention policy (TTL, archival)
  - admin API를 통한 audit log 조회
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

_audit_logger = logging.getLogger("mimir.audit")


class AuditEmitter:
    """감사 이벤트를 emit하는 인터페이스.

    현재: structured log 기반 stub (no-op persistence).
    이후: emit → DB insert / message publish 로 확장.
    """

    def emit(
        self,
        *,
        event_type: str,
        action: str,
        actor_id: Optional[str],
        resource_type: str,
        resource_id: Optional[str] = None,
        result: str,
        request_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """감사 이벤트를 emit한다.

        Args:
            event_type    : 이벤트 식별자 (예: document.created, version.created)
            action        : 수행된 action (예: document.create)
            actor_id      : 수행 주체 actor_id (None = anonymous)
            resource_type : 대상 리소스 타입 (document / version / node)
            resource_id   : 대상 리소스 UUID
            result        : 결과 분류 (success / failure / denied / conflict / replayed)
            request_id    : 상관관계 추적
            trace_id      : 상관관계 추적
            tenant_id     : 테넌트 scope
            metadata      : 추가 컨텍스트 (민감하지 않은 항목만)
        """
        event: dict[str, Any] = {
            "audit_event": event_type,
            "action": action,
            "actor_id": actor_id or "anonymous",
            "resource_type": resource_type,
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if resource_id:
            event["resource_id"] = resource_id
        if request_id:
            event["request_id"] = request_id
        if trace_id:
            event["trace_id"] = trace_id
        if tenant_id:
            event["tenant_id"] = tenant_id
        if metadata:
            event["metadata"] = metadata

        _audit_logger.info("AUDIT %s", event)

        # TODO: persist to DB / publish to message bus
        # self._persist(event)
        # self._publish(event)


# 모듈 수준 싱글턴 (stub/no-op persistence)
audit_emitter = AuditEmitter()
