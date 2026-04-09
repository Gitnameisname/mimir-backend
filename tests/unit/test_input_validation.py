"""
입력 검증 유틸리티 단위 테스트 (Phase 13-1).
"""
import pytest

pytestmark = pytest.mark.unit


def test_sanitize_filename_removes_traversal():
    """Path traversal 문자를 제거한다."""
    from app.api.security.input_validation import sanitize_filename

    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("../secret.txt") == "secret.txt"
    assert sanitize_filename("normal_file.pdf") == "normal_file.pdf"


def test_sanitize_filename_handles_empty():
    """빈 파일명을 '_'로 대체한다."""
    from app.api.security.input_validation import sanitize_filename

    assert sanitize_filename("") == "_"
    assert sanitize_filename(".") == "_"
    assert sanitize_filename("..") == "_"


def test_contains_null_byte():
    """널바이트 감지 함수가 올바르게 동작한다."""
    from app.api.security.input_validation import contains_null_byte

    assert contains_null_byte("normal string") is False
    assert contains_null_byte("string\x00with\x00nulls") is True


def test_validate_uuid_param_valid():
    """유효한 UUID v4를 통과시킨다."""
    from app.api.security.input_validation import validate_uuid_param

    assert validate_uuid_param("550e8400-e29b-41d4-a716-446655440000") is True


def test_validate_uuid_param_invalid():
    """유효하지 않은 UUID를 거부한다."""
    from app.api.security.input_validation import validate_uuid_param

    assert validate_uuid_param("not-a-uuid") is False
    assert validate_uuid_param("'; DROP TABLE users; --") is False
    assert validate_uuid_param("") is False


def test_request_size_limit_rejects_large_payload(client):
    """10MB 초과 요청을 413으로 거부한다."""
    response = client.post(
        "/api/v1/documents",
        content=b"x" * (10 * 1024 * 1024 + 1),
        headers={"Content-Length": str(10 * 1024 * 1024 + 1), "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_request_size_limit_allows_normal_payload(client):
    """정상 크기 요청은 거부하지 않는다."""
    response = client.get("/api/v1/system/health")
    assert response.status_code == 200
