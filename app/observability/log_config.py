"""
구조화 JSON 로그 설정 (Phase 13-2).

목표:
  - 모든 로그를 JSON 형태로 stdout에 출력한다
  - 민감 정보(토큰, 비밀번호, 이메일, IP)를 자동 마스킹한다
  - 로그 레벨을 환경별로 분리한다
  - 타임스탬프/레벨/모듈명/메시지를 일관된 필드로 기록한다

로그 레벨 정책:
  - production  : WARNING 이상 (INFO는 운영 로그인 경우만)
  - staging     : INFO 이상
  - development : DEBUG 이상

민감 정보 마스킹 대상:
  - Authorization 헤더 값
  - X-Service-Token 값
  - password / secret / token / key 이름을 가진 필드 값
  - 이메일 주소 (부분 마스킹)
"""
from __future__ import annotations

import json
import logging
import logging.config
import re
import sys
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.utils.json_utils import dumps_ko

# --------------------------------------------------------------------------- #
# 민감 필드 마스킹
# --------------------------------------------------------------------------- #
_SENSITIVE_FIELD_PATTERN = re.compile(
    r"(password|secret|token|api[-_]?key|authorization|jwt|private[-_]?key)",
    re.IGNORECASE,
)
_EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

MASKED = "***"


def mask_sensitive(value: str) -> str:
    """문자열에서 이메일을 부분 마스킹한다."""
    def _mask_email(m: re.Match[str]) -> str:
        email = m.group(0)
        local, domain = email.split("@", 1)
        masked_local = local[0] + MASKED if len(local) > 1 else MASKED
        return f"{masked_local}@{domain}"

    return _EMAIL_PATTERN.sub(_mask_email, value)


def _sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
    """로그 record에서 민감 필드를 마스킹한다."""
    sanitized: dict[str, Any] = {}
    for k, v in record.items():
        if _SENSITIVE_FIELD_PATTERN.search(k):
            sanitized[k] = MASKED
        elif isinstance(v, str):
            sanitized[k] = mask_sensitive(v)
        elif isinstance(v, dict):
            sanitized[k] = _sanitize_record(v)
        else:
            sanitized[k] = v
    return sanitized


# --------------------------------------------------------------------------- #
# JSON Formatter
# --------------------------------------------------------------------------- #
class JsonFormatter(logging.Formatter):
    """로그 레코드를 JSON 한 줄로 직렬화하는 Formatter."""

    def format(self, record: logging.LogRecord) -> str:
        # time.strftime은 %f(마이크로초)를 지원하지 않으므로 datetime을 직접 사용
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        log_entry: dict[str, Any] = {
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond:06d}+00:00",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # exc_info가 있으면 traceback 추가
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # extra 필드 (log_api_event 등이 추가하는 구조화 데이터)
        skip = {
            "msg", "args", "created", "filename", "funcName", "levelname",
            "levelno", "lineno", "module", "msecs", "message", "name",
            "pathname", "process", "processName", "relativeCreated",
            "thread", "threadName", "exc_info", "exc_text", "stack_info",
        }
        for key, val in record.__dict__.items():
            if key not in skip:
                log_entry[key] = val

        # 민감 필드 마스킹
        sanitized = _sanitize_record(log_entry)

        return dumps_ko(sanitized, default=str)


# --------------------------------------------------------------------------- #
# 환경별 로그 레벨 정책
# --------------------------------------------------------------------------- #
_LEVEL_BY_ENV: dict[str, str] = {
    # production: INFO — WARNING만 남기면 정상 감사 로그(success/validation_error)가 누락됨.
    # mimir.* 로거는 INFO 이상을 수집하여 감사 추적을 유지한다.
    # 외부 라이브러리(uvicorn, openai 등)는 별도로 WARNING으로 억제한다.
    "production": "INFO",
    "staging": "INFO",
    "development": "DEBUG",
    "test": "DEBUG",
}


def _get_log_level() -> str:
    return _LEVEL_BY_ENV.get(settings.environment, "INFO")


# --------------------------------------------------------------------------- #
# 로깅 설정 초기화
# --------------------------------------------------------------------------- #
def configure_logging() -> None:
    """애플리케이션 시작 시 1회 호출하여 로깅을 설정한다."""
    level = _get_log_level()

    # Formatter 인스턴스
    json_formatter = JsonFormatter()

    # stdout 핸들러
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(json_formatter)
    handler.setLevel(level)

    # 루트 로거 설정
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    # 기존 핸들러 제거 후 교체 (중복 방지)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # 외부 라이브러리 로그 레벨 조정 (노이즈 억제)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("fastapi").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)

    logging.getLogger("mimir").setLevel(level)
