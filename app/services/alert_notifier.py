"""Alert notifier (Phase 14-13).

지원 채널: email / webhook (문자열 키로 관리 — 향후 확장).
발송 실패는 격리하여 전체 흐름을 멈추지 않는다.
웹훅 타임아웃 10초 고정.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from app.services.email_service import EmailService

logger = logging.getLogger(__name__)

# 허용된 웹훅 스킴만 — file://, gopher://, ftp:// 등 차단 (SSRF 방어)
_ALLOWED_WEBHOOK_SCHEMES = {"https", "http"}

_WEBHOOK_CONNECT_TIMEOUT = 5   # TCP 연결 수립 제한
_WEBHOOK_READ_TIMEOUT = 10     # 응답 수신 제한 (슬로우 리드 방어)


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
    # SSRF: localhost 문자열 차단
    if host == "localhost":
        return False
    # SSRF: IP 주소인 경우 ipaddress 모듈로 private/loopback 검사 (IPv4 + IPv6)
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_multicast:
            return False
    except ValueError:
        # 도메인명인 경우 — IP 파싱 실패는 정상
        pass
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
        timeout = httpx.Timeout(
            connect=_WEBHOOK_CONNECT_TIMEOUT,
            read=_WEBHOOK_READ_TIMEOUT,
            write=5.0,
            pool=2.0,
        )
        try:
            with httpx.Client(timeout=timeout, verify=True) as client:
                resp = client.post(
                    url,
                    json=payload,
                    headers={"User-Agent": "Mimir-Alert/1.0"},
                )
                return 200 <= resp.status_code < 300
        except Exception as exc:
            logger.warning("웹훅 호출 실패 %s: %s", url, exc)
            return False


alert_notifier = AlertNotifier()
