"""Valkey pub/sub primitives — S3 Phase 7 FG 7-3.

Cluster-wide cache invalidation 을 위한 pub/sub 헬퍼.

설계 원칙:
    - **자기 채널만 SUBSCRIBE** — 다른 tenant prefix 채널 접근 금지 (R-I3)
    - **loop 방지** — 자기 worker_id 가 발행한 메시지는 dispatch skip
    - **DoS 방어** — 메시지 크기 1KB 상한, JSON parse 실패 silent skip
    - **fail-open** — Valkey 장애 / disabled 시 publish 는 silent skip
      (process-local cache 는 호출자가 이미 비웠으므로 다른 워커는 TTL 만료에 의존)
    - **daemon thread** — subscriber thread 는 daemon=True. 앱 종료 시 자동 종료.

함수도서관: ``docs/함수도서관/backend.md`` §1.11-fg73 (FG 7-3 신설).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Callable, Optional

from app.cache.namespace import make_channel
from app.cache.valkey import get_valkey_or_none, is_valkey_disabled
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "publish_invalidate",
    "Subscriber",
    "WORKER_ID",
    "MAX_MESSAGE_SIZE",
]


# 한 워커의 고유 ID — loop 방지에 사용.
WORKER_ID: str = f"pid-{os.getpid()}"

# 메시지 크기 상한 (R-I3 DoS 방어).
MAX_MESSAGE_SIZE: int = 1024


def publish_invalidate(
    feature: str,
    key: str,
    *,
    org_id: Optional[str] = None,
) -> bool:
    """invalidate broadcast 발행. 성공 시 True, skip / 실패 시 False.

    Args:
        feature: 채널 식별자 (예: ``"scope_policy"``).
        key: invalidate 대상 — profile_id, document_id, 또는 ``"*"`` (전체).
        org_id: 테넌트 ID. 지정 시 ``tenant:<org_id>:`` prefix 채널로 발행.

    Returns:
        ``True``: 발행 성공
        ``False``: disabled / 장애 / 인자 오류 → silent skip

    Notes:
        - 본 호출은 best-effort. 실패해도 호출자 흐름 차단 안 함.
        - 메시지에 ``worker_id`` 포함 — 자기 자신이 받은 경우 loop 방지.
    """
    if is_valkey_disabled():
        return False

    try:
        channel = make_channel(feature, org_id=org_id)
    except ValueError as exc:
        logger.warning("publish_invalidate: invalid feature %r: %s", feature, exc)
        return False

    payload = {
        "key": key,
        "ts": utcnow().timestamp(),
        "worker_id": WORKER_ID,
    }
    try:
        message = json.dumps(payload, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        logger.warning("publish_invalidate: JSON encode failed: %s", exc)
        return False

    if len(message) > MAX_MESSAGE_SIZE:
        logger.warning(
            "publish_invalidate: message size %d > %d, skip",
            len(message),
            MAX_MESSAGE_SIZE,
        )
        return False

    client = get_valkey_or_none()
    if client is None:
        return False

    try:
        client.publish(channel, message)
    except Exception as exc:
        logger.debug(
            "publish_invalidate: publish failed (channel=%s): %s", channel, exc
        )
        return False

    return True


class Subscriber:
    """단일 feature 채널 subscriber.

    Usage:
        sub = Subscriber("scope_policy", on_invalidate=lambda key: ...)
        sub.start()
        # ...
        sub.stop()
    """

    def __init__(
        self,
        feature: str,
        on_invalidate: Callable[[str], None],
        *,
        org_id: Optional[str] = None,
    ):
        self._feature = feature
        self._on_invalidate = on_invalidate
        self._channel = make_channel(feature, org_id=org_id)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pubsub: Any = None

    def dispatch(self, raw_message: Any) -> None:
        """단일 메시지를 처리한다. 단위 테스트에서 직접 호출 가능.

        잘못된 message 는 silent skip — DoS 방어.
        """
        if raw_message is None:
            return
        # redis-py message format: {"type": "message", "channel": ..., "data": ...}
        if isinstance(raw_message, dict):
            data = raw_message.get("data")
            if raw_message.get("type") != "message":
                return  # subscribe confirm 등 무시
        else:
            data = raw_message

        if not isinstance(data, (str, bytes)):
            return
        if isinstance(data, bytes):
            try:
                data = data.decode("utf-8")
            except UnicodeDecodeError:
                return

        if len(data) > MAX_MESSAGE_SIZE:
            logger.warning(
                "Subscriber(%s): message size %d > %d, skip",
                self._feature, len(data), MAX_MESSAGE_SIZE,
            )
            return

        try:
            payload = json.loads(data)
        except (ValueError, TypeError):
            logger.debug("Subscriber(%s): non-JSON message, skip", self._feature)
            return

        if not isinstance(payload, dict):
            return

        # loop 방지 — 자기 worker 가 발행한 메시지면 skip
        if payload.get("worker_id") == WORKER_ID:
            return

        key = payload.get("key")
        if not isinstance(key, str) or not key:
            return

        try:
            self._on_invalidate(key)
        except Exception as exc:
            logger.warning(
                "Subscriber(%s): on_invalidate callback failed: %s",
                self._feature, exc,
            )

    def start(self) -> bool:
        """subscriber thread 시작. disabled 모드면 silent skip + False."""
        if is_valkey_disabled():
            logger.info("Subscriber(%s): Valkey disabled — skip start", self._feature)
            return False

        client = get_valkey_or_none()
        if client is None:
            return False

        try:
            self._pubsub = client.pubsub(ignore_subscribe_messages=True)
            self._pubsub.subscribe(self._channel)
        except Exception as exc:
            logger.warning(
                "Subscriber(%s): subscribe failed: %s", self._feature, exc
            )
            return False

        self._thread = threading.Thread(
            target=self._run,
            name=f"pubsub-{self._feature}",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """subscriber thread 종료 신호."""
        self._stop_event.set()
        if self._pubsub is not None:
            try:
                self._pubsub.close()
            except Exception:
                pass

    def _run(self) -> None:
        if self._pubsub is None:
            return
        while not self._stop_event.is_set():
            try:
                # 짧은 timeout 으로 stop_event 응답성 보장
                message = self._pubsub.get_message(timeout=1.0)
                if message is not None:
                    self.dispatch(message)
            except Exception as exc:
                logger.warning(
                    "Subscriber(%s) loop error: %s", self._feature, exc
                )
                # 백오프 — Valkey 일시 장애에서 burst 회피
                if self._stop_event.wait(1.0):
                    break
