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

    자동 재구독 (Codex 2차 P2 시정, 2026-05-19):
        - ``start()`` 가 disabled / subscribe 실패하면 supervisor 만 띄움 (subscribe 없이).
        - supervisor 가 주기적으로 ``is_valkey_disabled()`` 와 connection 회복 확인.
        - subscribe 성공 시 message loop 진입. loop 내 connection error → backoff 후 재시도.
    """

    # supervisor reconnect 주기 (초). disabled 모드여도 이 주기로 깨어나 확인.
    RECONNECT_BACKOFF_MIN_SEC: float = 1.0
    RECONNECT_BACKOFF_MAX_SEC: float = 30.0

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
        self._connected = False

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
        """subscriber 시작 — supervisor thread 띄움.

        Returns:
            ``True``: supervisor thread 시작 성공 (subscribe 자체는 비동기 시도).
                disabled 모드여도 ``True`` 반환 — supervisor 가 회복 시 자동 재구독.
                **단** 이전 호환을 위해 disabled 모드는 startup 시점에 ``False`` 반환
                (이미 등록된 호출자가 disabled = no-op 으로 가정).
            ``False``: disabled 모드 (supervisor 미시작).

        Codex 2차 P2 시정 (2026-05-19): supervisor 가 connection 회복 시 자동 재구독.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.debug("Subscriber(%s): already running", self._feature)
            return True
        if is_valkey_disabled():
            logger.info(
                "Subscriber(%s): Valkey disabled — skip start (no supervisor)",
                self._feature,
            )
            return False

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._supervisor,
            name=f"pubsub-{self._feature}",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """subscriber 종료 신호. supervisor + message loop 모두 종료."""
        self._stop_event.set()
        self._connected = False
        if self._pubsub is not None:
            try:
                self._pubsub.close()
            except Exception:
                pass
            self._pubsub = None

    def is_connected(self) -> bool:
        """현재 subscribe 상태. 운영 모니터링 / 테스트 용."""
        return self._connected

    def _try_subscribe(self) -> bool:
        """1회 subscribe 시도. 성공 시 True, 실패 시 False."""
        if is_valkey_disabled():
            return False
        client = get_valkey_or_none()
        if client is None:
            return False
        try:
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(self._channel)
        except Exception as exc:
            logger.debug(
                "Subscriber(%s): subscribe attempt failed: %s",
                self._feature, exc,
            )
            return False
        self._pubsub = pubsub
        self._connected = True
        return True

    def _message_loop(self) -> None:
        """subscribe 된 상태에서 message dispatch loop.

        connection / message error 발생 시 break — supervisor 가 재구독.
        """
        while not self._stop_event.is_set():
            try:
                message = self._pubsub.get_message(timeout=1.0)
            except Exception as exc:
                logger.warning(
                    "Subscriber(%s): get_message error — will reconnect: %s",
                    self._feature, exc,
                )
                return
            if message is not None:
                try:
                    self.dispatch(message)
                except Exception as exc:  # pragma: no cover — dispatch already swallows
                    logger.warning(
                        "Subscriber(%s): dispatch raised: %s",
                        self._feature, exc,
                    )

    def _supervisor(self) -> None:
        """주기적 재구독 supervisor (Codex 2차 P2 시정).

        - subscribe 미연결 시: backoff 후 재시도
        - subscribe 연결 시: message loop 진입. loop 종료 후 재구독 사이클
        """
        backoff = self.RECONNECT_BACKOFF_MIN_SEC
        while not self._stop_event.is_set():
            if not self._connected:
                if self._try_subscribe():
                    logger.info(
                        "Subscriber(%s): connected to %s",
                        self._feature, self._channel,
                    )
                    backoff = self.RECONNECT_BACKOFF_MIN_SEC  # 회복 시 backoff 리셋
                else:
                    # disabled 또는 connection 실패 — backoff 후 재시도
                    if self._stop_event.wait(backoff):
                        return
                    backoff = min(backoff * 2, self.RECONNECT_BACKOFF_MAX_SEC)
                    continue

            # 연결됨 — message loop 진입
            self._message_loop()

            # loop 가 종료됨 (stop 또는 error) → 정리 + 재구독 사이클로
            self._connected = False
            if self._pubsub is not None:
                try:
                    self._pubsub.close()
                except Exception:
                    pass
                self._pubsub = None
