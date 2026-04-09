"""
시스템 엔드포인트 통합 테스트 (Phase 13-4).

INTEGRATION_TEST=1 환경 변수가 없으면 건너뜀.
"""
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_health_check_with_db(client, integration_db):
    """실제 DB 연결 상태에서 헬스체크가 성공한다."""
    response = client.get("/api/v1/system/health")
    assert response.status_code == 200
    assert response.json()["data"]["healthy"] is True


def test_metrics_endpoint_with_traffic(client, integration_db):
    """트래픽 발생 후 메트릭이 수집된다."""
    # 트래픽 생성
    for _ in range(5):
        client.get("/api/v1/system/health")

    response = client.get("/api/v1/system/metrics")
    assert response.status_code == 200
    body = response.text

    # 헬스체크 경로가 메트릭에 기록되어야 함
    assert "http_requests_total" in body
