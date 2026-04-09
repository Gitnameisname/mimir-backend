"""
구조화 로깅 단위 테스트 (Phase 13-2).
"""
import json
import logging

import pytest

pytestmark = pytest.mark.unit


def test_json_formatter_outputs_valid_json():
    """JsonFormatter가 유효한 JSON을 출력한다."""
    from app.observability.log_config import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="hello world", args=(), exc_info=None
    )
    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed["message"] == "hello world"
    assert parsed["level"] == "INFO"
    assert "timestamp" in parsed


def test_json_formatter_masks_password_field():
    """password 필드가 마스킹된다."""
    from app.observability.log_config import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="login attempt", args=(), exc_info=None
    )
    record.password = "super_secret_password"
    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed.get("password") == "***"
    assert "super_secret_password" not in output


def test_json_formatter_masks_email_in_message():
    """이메일 주소가 부분 마스킹된다."""
    from app.observability.log_config import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="user john.doe@example.com logged in", args=(), exc_info=None
    )
    output = formatter.format(record)
    parsed = json.loads(output)

    assert "john.doe@example.com" not in parsed["message"]
    assert "@example.com" in parsed["message"]  # 도메인은 유지


def test_mask_sensitive_function():
    """mask_sensitive 함수가 이메일을 올바르게 마스킹한다."""
    from app.observability.log_config import mask_sensitive

    result = mask_sensitive("contact alice@company.org for info")
    assert "alice@company.org" not in result
    assert "@company.org" in result
    assert result.startswith("contact a")


def test_sanitize_token_field():
    """token 키를 가진 필드가 마스킹된다."""
    from app.observability.log_config import _sanitize_record

    record = {"event": "auth", "access_token": "eyJhbGciOi...", "user_id": "abc123"}
    sanitized = _sanitize_record(record)

    assert sanitized["access_token"] == "***"
    assert sanitized["user_id"] == "abc123"
    assert sanitized["event"] == "auth"
