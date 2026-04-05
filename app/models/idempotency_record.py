"""
IdempotencyRecord 도메인 모델 (순수 Python dataclass).

write endpoint의 retry-safe 흐름을 지원하기 위한 record.

상태 전이:
  in_progress → completed : write 성공 완료
  in_progress → failed    : write 실패 (재시도 가능)
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass
class IdempotencyRecord:
    """Idempotency record 도메인 모델.

    Attributes:
        id                  : UUID
        idempotency_key     : 클라이언트가 제공한 키
        actor_id            : 요청 주체 (None = anonymous)
        resource_action     : 수행 중인 action (예: document.create)
        request_fingerprint : 요청 의미의 stable hash
        status              : in_progress / completed / failed
        response_status_code: 성공 시 저장한 HTTP status code
        response_body       : 성공 시 저장한 response JSON (replay용)
        resource_id         : 생성된 리소스 UUID
        request_id          : 상관관계 추적
        trace_id            : 상관관계 추적
        tenant_id           : 멀티테넌시 scope
        created_at          : 최초 요청 시각
        updated_at          : 마지막 상태 갱신 시각
        expires_at          : TTL (None = 만료 없음)
    """

    id: str
    idempotency_key: str
    resource_action: str
    request_fingerprint: str
    status: str
    created_at: datetime
    updated_at: datetime
    actor_id: Optional[str] = None
    response_status_code: Optional[int] = None
    response_body: Optional[dict[str, Any]] = None
    resource_id: Optional[str] = None
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    tenant_id: Optional[str] = None
    expires_at: Optional[datetime] = None
