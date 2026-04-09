"""
Prometheus 메트릭 수집 단위 테스트 (Phase 13-3).
"""
import pytest

pytestmark = pytest.mark.unit


def test_metrics_endpoint_returns_text(client):
    """GET /api/v1/system/metrics가 Prometheus text 형식을 반환한다."""
    # 먼저 요청 몇 건 발생시켜 메트릭 채움
    client.get("/api/v1/system/health")
    client.get("/api/v1/system/info")

    response = client.get("/api/v1/system/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers.get("content-type", "")

    body = response.text
    assert "http_requests_total" in body
    assert "http_request_duration_ms" in body
    assert "active_connections" in body


def test_metrics_contain_help_and_type(client):
    """메트릭 텍스트에 # HELP와 # TYPE 라인이 있다."""
    client.get("/api/v1/system/health")
    response = client.get("/api/v1/system/metrics")
    body = response.text

    assert "# HELP http_requests_total" in body
    assert "# TYPE http_requests_total counter" in body


def test_normalize_path_replaces_uuid():
    """UUID 경로 세그먼트가 {id}로 정규화된다."""
    from app.observability.metrics import _normalize_path

    path = "/api/v1/documents/550e8400-e29b-41d4-a716-446655440000/versions"
    normalized = _normalize_path(path)
    assert "550e8400" not in normalized
    assert "{id}" in normalized


def test_generate_metrics_text_format():
    """generate_metrics_text가 올바른 Prometheus 형식을 반환한다."""
    from app.observability.metrics import generate_metrics_text

    text = generate_metrics_text()
    assert isinstance(text, str)
    assert text.endswith("\n")
    # 각 줄은 주석(#)이거나 메트릭 라인
    for line in text.strip().split("\n"):
        assert line.startswith("#") or " " in line, f"잘못된 형식: {line!r}"
