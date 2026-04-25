"""
IdempotencyService — write endpoint retry-safe 흐름을 위한 공통 서비스.

책임:
  - Idempotency-Key 유효성 검증
  - request fingerprint 계산 (stable hash)
  - same-key same-request → replay (이전 응답 반환)
  - same-key different-request → conflict (409)
  - same-key in_progress → conflict (409, 진행 중)
  - 신규 key → in_progress reservation 생성
  - write 완료 후 → completed finalize

설계 원칙:
  - 라우터마다 키 검사 로직을 쓰지 않는다. 이 서비스로 공통화.
  - key가 None이면 idempotency 없는 일반 write로 처리 (optional by default).
  - fingerprint에 request_id / trace_id 같은 변동 값은 포함하지 않는다.
  - replay 응답은 원래 success_response envelope과 의미적으로 동일하게 유지.

TODO:
  - TTL / cleanup 정책 정교화
  - PATCH endpoint 적용 확장
  - stronger transaction guarantee (two-phase commit 등)
  - async operation 연계
  - audit/logging 고도화
"""

import hashlib
import json
import logging
from typing import Any, Optional

from app.api.errors.exceptions import ApiIdempotencyError, ApiValidationError
from app.api.responses import SuccessResponse
from app.api.responses.helpers import success_response
from app.db import get_db
from app.repositories.idempotency_repository import idempotency_repository
from app.utils.json_utils import dumps_ko

logger = logging.getLogger(__name__)

_MAX_KEY_LENGTH = 255
_MIN_KEY_LENGTH = 1


def _validate_key(key: str) -> None:
    """Idempotency-Key 형식 유효성 검사."""
    if not key or not key.strip():
        raise ApiValidationError("Idempotency-Key must not be blank")
    if len(key) < _MIN_KEY_LENGTH:
        raise ApiValidationError("Idempotency-Key is too short")
    if len(key) > _MAX_KEY_LENGTH:
        raise ApiValidationError(
            f"Idempotency-Key exceeds maximum length of {_MAX_KEY_LENGTH} characters"
        )


def _compute_fingerprint(
    action: str,
    request_body: dict[str, Any],
    actor_id: Optional[str],
    path_params: Optional[dict[str, str]] = None,
) -> str:
    """request의 안정적인 fingerprint를 계산한다.

    포함 요소:
      - action (canonical endpoint identifier)
      - normalized request body (key-sorted JSON)
      - actor_id (또는 "anonymous")
      - path_params (예: document_id)

    제외 요소 (변동 값):
      - request_id, trace_id, timestamp
    """
    components: dict[str, Any] = {
        "action": action,
        "actor_id": actor_id or "anonymous",
        "body": request_body,
    }
    if path_params:
        components["path"] = path_params

    # key-sorted JSON → stable string → sha256
    stable_str = dumps_ko(components, sort_keys=True, default=str)
    return hashlib.sha256(stable_str.encode()).hexdigest()


class IdempotencyService:
    """Write endpoint idempotency 흐름을 관리하는 공통 서비스."""

    def check_and_replay(
        self,
        key: Optional[str],
        actor_id: Optional[str],
        action: str,
        request_body: dict[str, Any],
        path_params: Optional[dict[str, str]] = None,
        *,
        request_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> Optional[SuccessResponse]:
        """Idempotency-Key가 있을 때 기존 record를 확인하고 replay 여부를 결정한다.

        Returns:
            SuccessResponse: replay 가능한 경우 이전 응답 반환.
            None: 신규 요청이므로 write 진행.

        Raises:
            ApiValidationError:    key 형식 불량.
            ApiIdempotencyError:   same-key, different-request (conflict).
                                   same-key, in_progress (conflict).
        """
        if key is None:
            return None  # key 없음 → 일반 write 진행

        _validate_key(key)
        fingerprint = _compute_fingerprint(action, request_body, actor_id, path_params)

        try:
            with get_db() as conn:
                record = idempotency_repository.get(conn, key, actor_id, action)

                if record is None:
                    # 신규 key → in_progress reservation 생성
                    idempotency_repository.create_in_progress(
                        conn,
                        key,
                        actor_id,
                        action,
                        fingerprint,
                        request_id=request_id,
                        trace_id=trace_id,
                    )
                    logger.info(
                        "Idempotency reservation created: key=***, action=%s, actor=%s, request_id=%s",
                        action,
                        actor_id,
                        request_id,
                    )
                    return None

                # --- 기존 record 존재 ---

                if record.status == "in_progress":
                    logger.warning(
                        "Idempotency in_progress conflict: action=%s, actor=%s, request_id=%s",
                        action,
                        actor_id,
                        request_id,
                    )
                    raise ApiIdempotencyError(
                        "A request with this Idempotency-Key is already in progress",
                    )

                if record.request_fingerprint != fingerprint:
                    logger.warning(
                        "Idempotency fingerprint mismatch: action=%s, actor=%s, request_id=%s",
                        action,
                        actor_id,
                        request_id,
                    )
                    raise ApiIdempotencyError(
                        "Idempotency key was already used for a different request",
                    )

                if record.status == "completed" and record.response_body:
                    # same-key, same-request → replay
                    logger.info(
                        "Idempotency replay: action=%s, actor=%s, resource_id=%s, request_id=%s",
                        action,
                        actor_id,
                        record.resource_id,
                        request_id,
                    )
                    # replay 응답: 원래 data를 그대로 반환
                    return success_response(
                        data=record.response_body.get("data", {}),
                        request_id=request_id,
                        trace_id=trace_id,
                    )

                # failed 또는 completed지만 body 없음 → 신규 write 허용
                return None

        except ApiIdempotencyError:
            raise
        except ApiValidationError:
            raise
        except Exception as exc:
            logger.error(
                "Idempotency check failed (proceeding with write): %s, request_id=%s",
                exc,
                request_id,
            )
            # idempotency store 장애 시 write는 계속 진행 (fail-open)
            return None

    def finalize(
        self,
        key: Optional[str],
        actor_id: Optional[str],
        action: str,
        resource_id: Optional[str],
        response: SuccessResponse,
        *,
        request_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        """write 성공 후 idempotency record를 completed로 갱신한다.

        key가 None이면 no-op.
        """
        if key is None:
            return

        try:
            response_dict = response.model_dump()
            with get_db() as conn:
                idempotency_repository.mark_completed(
                    conn,
                    key,
                    actor_id,
                    action,
                    response_status_code=200,
                    response_body=response_dict,
                    resource_id=resource_id,
                )
            logger.info(
                "Idempotency finalized: action=%s, actor=%s, resource_id=%s",
                action,
                actor_id,
                resource_id,
            )
        except Exception as exc:
            # finalize 실패는 write 성공에 영향 없음 — 로그만 남김
            logger.error(
                "Idempotency finalize failed (record may replay incorrectly): %s, request_id=%s",
                exc,
                request_id,
            )

    def mark_failed(
        self,
        key: Optional[str],
        actor_id: Optional[str],
        action: str,
    ) -> None:
        """write 실패 시 idempotency record를 failed로 갱신한다.

        key가 None이면 no-op.
        """
        if key is None:
            return

        try:
            with get_db() as conn:
                idempotency_repository.mark_failed(conn, key, actor_id, action)
        except Exception as exc:
            logger.error("Idempotency mark_failed error: %s", exc)


# 모듈 수준 싱글턴
idempotency_service = IdempotencyService()
