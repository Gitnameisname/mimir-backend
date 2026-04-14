"""Alert notifier (Phase 14-13).

지원 채널: email / webhook (문자열 키로 관리 — 향후 확장).
발송 실패는 격리하여 전체 흐름을 멈추지 않는다.
웹훅 타임아웃 10초 고정.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import urllib.request
import json as _json
import ssl

from app.services.email_service import EmailService

logger = logging.getLogger(__name__)

# 허용된 웹훅 스킴만 — file://, gopher://, ftp:// 등 차단 (SSRF 방어)
_ALLOWED_WEBHOOK_SCHEMES = {"https", "http"}

# 내부 호스트(loopback/link-local) 차단 — SSRF 기본 방어
_BLOCKED_HOST_PREFIXES = ("localhost", "127.", "0.", "169.254.", "10.", "172.", "192.168.")

_WEBHOOK_TIMEOUT_SECONDS = 10


def _is_safe_webhook_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in _ALLOWED_WEBHOOK_SCHEMES:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # SSRF: private/loopback 차단
    if any(host.startswith(p) for p in _BLOCKED_HOST_PREFIXES):
        return False
    return True


class AlertNotifier:
    def __init__(self) -> None:
        self._email = EmailService()

    def notify(
        self,
        rule: dict[str, Any],
        *,
        metric_value: float,
        message: str,
    ) -> list[str]:
        """각 채널로 발송 시도. 성공한 채널 이름 리스트 반환."""
        channels: list[str] = rule.get("channels") or []
        channel_config: dict[str, Any] = rule.get("channel_config") or {}
        notified: list[str] = []

        for ch in channels:
            try:
                if ch == "email":
                    if self._send_email(rule, metric_value, message, channel_config):
                        notified.append("email")
                elif ch == "webhook":
                    if self._send_webhook(rule, metric_value, message, channel_config):
                        notified.append("webhook")
                else:
                    logger.info("지원하지 않는 채널 무시: %s", ch)
            except Exception as exc:
                logger.warning("알림 발송 실패 [%s]: %s", ch, exc)

        return notified

    # ---- email ----------------------------------------------------
    def _send_email(
        self,
        rule: dict[str, Any],
        metric_value: float,
        message: str,
        channel_config: dict[str, Any],
    ) -> bool:
        if not self._email.is_configured:
            return False
        recipients = channel_config.get("email_recipients") or []
        if not recipients or not isinstance(recipients, list):
            return False
        subject = f"[Mimir Alert] [{rule.get('severity', 'info').upper()}] {rule.get('name')}"
        body = (
            f"규칙: {rule.get('name')}\n"
            f"메트릭: {rule.get('metric_name')}\n"
            f"현재 값: {metric_value}\n"
            f"메시지: {message}\n"
        )
        sent_any = False
        for addr in recipients[:10]:  # 수신자 수 상한 — DoS 방어
            if not isinstance(addr, str) or "@" not in addr:
                continue
            if self._email.send_email(addr, subject, body):
                sent_any = True
        return sent_any

    # ---- webhook --------------------------------------------------
    def _send_webhook(
        self,
        rule: dict[str, Any],
        metric_value: float,
        message: str,
        channel_config: dict[str, Any],
    ) -> bool:
        url = channel_config.get("webhook_url")
        if not isinstance(url, str) or not _is_safe_webhook_url(url):
            logger.info("웹훅 URL 안전 검증 실패: %s", url)
            return False

        payload = {
            "rule_id": rule.get("id"),
            "rule_name": rule.get("name"),
            "severity": rule.get("severity"),
            "metric_name": rule.get("metric_name"),
            "metric_value": metric_value,
            "message": message,
        }
        try:
            data = _json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "Mimir-Alert/1.0"},
            )
            # TLS context — 시스템 CA 사용
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT_SECONDS, context=ctx) as resp:
                status = getattr(resp, "status", 200)
                return 200 <= int(status) < 300
        except Exception as exc:
            logger.warning("웹훅 호출 실패 %s: %s", url, exc)
            return False


alert_notifier = AlertNotifier()
