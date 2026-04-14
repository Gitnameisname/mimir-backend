"""
SMTP 이메일 발송 서비스 (Phase 14-5).

책임:
  - SMTP를 통한 이메일 발송 (동기 방식, smtplib 사용)
  - 비밀번호 재설정 이메일 템플릿
  - 이메일 인증 이메일 템플릿
  - 발송 실패 시 예외를 삼키고 로깅 (사용자 흐름 블로킹 방지)

보안 원칙:
  - 이메일 본문에 사용자 비밀번호 절대 포함 금지
  - TLS/STARTTLS 사용
  - SMTP 인증 정보는 환경 변수로만 관리
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


class EmailService:
    """SMTP 이메일 발송 서비스."""

    def __init__(self):
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.username = settings.smtp_username
        self.password = settings.smtp_password
        self.from_address = settings.smtp_from_address
        self.use_tls = settings.smtp_use_tls

    @property
    def is_configured(self) -> bool:
        """SMTP가 설정되어 있는지 확인."""
        return bool(self.host and self.from_address)

    def send_email(self, to: str, subject: str, body: str) -> bool:
        """이메일을 발송한다.

        발송 실패 시 False를 반환하고 로깅한다 (예외 전파 없음).

        Args:
            to: 수신자 이메일 주소.
            subject: 메일 제목.
            body: 메일 본문 (텍스트).

        Returns:
            발송 성공 여부.
        """
        if not self.is_configured:
            logger.warning("email_send: SMTP not configured, skipping email to %s", to)
            return False

        msg = EmailMessage()
        msg["From"] = self.from_address
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        try:
            if self.use_tls:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=10) as server:
                    if self.username and self.password:
                        server.login(self.username, self.password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=10) as server:
                    server.ehlo()
                    if self.port != 25:
                        server.starttls()
                        server.ehlo()
                    if self.username and self.password:
                        server.login(self.username, self.password)
                    server.send_message(msg)

            logger.info("email_sent to=%s subject=%s", to, subject)
            return True

        except Exception:
            logger.exception("email_send_failed to=%s subject=%s", to, subject)
            return False

    # ─── 템플릿 메서드 ───

    def send_password_reset_email(self, to: str, reset_url: str) -> bool:
        """비밀번호 재설정 이메일을 발송한다.

        Args:
            to: 수신자 이메일.
            reset_url: 비밀번호 재설정 링크.

        Returns:
            발송 성공 여부.
        """
        subject = "[Mimir] 비밀번호 재설정 요청"
        body = (
            f"안녕하세요,\n\n"
            f"비밀번호 재설정 요청을 받았습니다.\n"
            f"아래 링크를 클릭하여 새 비밀번호를 설정해 주세요:\n\n"
            f"{reset_url}\n\n"
            f"이 링크는 30분 동안 유효하며, 1회만 사용할 수 있습니다.\n\n"
            f"비밀번호 재설정을 요청하지 않으셨다면 이 이메일을 무시해 주세요.\n\n"
            f"감사합니다,\n"
            f"Mimir 팀"
        )
        return self.send_email(to, subject, body)

    def send_email_verification(self, to: str, verify_url: str) -> bool:
        """이메일 인증 이메일을 발송한다.

        Args:
            to: 수신자 이메일.
            verify_url: 이메일 인증 링크.

        Returns:
            발송 성공 여부.
        """
        subject = "[Mimir] 이메일 인증"
        body = (
            f"안녕하세요,\n\n"
            f"Mimir에 가입해 주셔서 감사합니다.\n"
            f"아래 링크를 클릭하여 이메일을 인증해 주세요:\n\n"
            f"{verify_url}\n\n"
            f"이 링크는 24시간 동안 유효하며, 1회만 사용할 수 있습니다.\n\n"
            f"감사합니다,\n"
            f"Mimir 팀"
        )
        return self.send_email(to, subject, body)


# 모듈 수준 싱글턴
email_service = EmailService()
