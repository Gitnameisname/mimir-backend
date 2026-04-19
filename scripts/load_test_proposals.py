"""
Mimir 제안 시스템 부하 테스트 (PH5-CARRY-003).

실행 방법:
  locust -f scripts/load_test_proposals.py \
    --host http://localhost:8050 \
    --users 1000 \
    --spawn-rate 50 \
    --run-time 5m \
    --html reports/load_test_$(date +%Y%m%d_%H%M%S).html

주의: 스테이징 환경 전용. 프로덕션에서 절대 실행 금지.
"""

import os
import random
import string

from locust import HttpUser, between, events, task

# 프로덕션 실행 방지
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "staging.mimir.internal"}


def _check_host(host: str) -> None:
    from urllib.parse import urlparse
    hostname = urlparse(host).hostname or ""
    if hostname not in _ALLOWED_HOSTS:
        raise SystemExit(
            f"[BLOCKED] 허용되지 않은 호스트: {hostname}. 스테이징 환경에서만 실행하세요."
        )


def random_content(length: int = 200) -> str:
    return "".join(random.choices(string.ascii_letters + " \n", k=length))


class ProposalUser(HttpUser):
    """에이전트가 제안을 제출하는 시나리오."""

    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        self.api_key = f"mim_sk_test_{random.randint(1, 100):04d}"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        self.document_ids = [f"load-test-doc-{i:04d}" for i in range(1, 101)]

    @task(5)
    def submit_proposal(self) -> None:
        doc_id = random.choice(self.document_ids)
        payload = {
            "document_id": doc_id,
            "proposed_content": random_content(500),
            "reason": f"부하 테스트 제안 {random.randint(1, 9999)}",
        }

        with self.client.post(
            "/api/v1/mcp/tools/call",
            json={"name": "propose_document_change", "arguments": payload},
            headers=self.headers,
            catch_response=True,
            name="submit_proposal",
        ) as response:
            if response.status_code == 429:
                response.success()  # Rate Limit은 정상 응답으로 처리 (지침 7-12-1)
            elif response.status_code not in (200, 201):
                response.failure(f"Unexpected status: {response.status_code}")

    @task(2)
    def list_proposals(self) -> None:
        with self.client.get(
            "/api/v1/proposals",
            headers=self.headers,
            params={"page": 1, "page_size": 20},
            catch_response=True,
            name="list_proposals",
        ) as response:
            if response.status_code == 429:
                response.success()
            elif response.status_code != 200:
                response.failure(f"Unexpected status: {response.status_code}")


class AdminUser(HttpUser):
    """관리자가 제안을 일괄 처리하는 시나리오."""

    wait_time = between(1, 3)
    weight = 1  # 에이전트 1000명당 관리자 1명 비율

    def on_start(self) -> None:
        self.headers = {
            "Authorization": "Bearer admin-test-token",
            "Content-Type": "application/json",
        }

    @task(1)
    def batch_approve_proposals(self) -> None:
        list_resp = self.client.get(
            "/api/v1/admin/proposals",
            headers=self.headers,
            params={"status": "pending", "page_size": 50},
        )
        if list_resp.status_code != 200:
            return

        data = list_resp.json()
        proposals = data.get("data") or data.get("items") or []
        if not proposals:
            return

        ids = [p["id"] for p in proposals[:10]]

        with self.client.post(
            "/api/v1/admin/proposals/batch-approve",
            json={"ids": ids},
            headers=self.headers,
            catch_response=True,
            name="batch_approve",
        ) as response:
            if response.status_code not in (200, 207):
                response.failure(f"Unexpected status: {response.status_code}")


@events.init.add_listener
def on_locust_init(environment, **kwargs) -> None:
    if environment.host:
        _check_host(environment.host)


@events.quitting.add_listener
def on_quitting(environment, **kwargs) -> None:
    stats = environment.runner.stats
    total = stats.total

    print("\n" + "=" * 60)
    print("부하 테스트 결과 요약")
    print("=" * 60)
    print(f"총 요청: {total.num_requests:,}")
    print(f"실패 요청: {total.num_failures:,}")
    print(f"에러율: {total.fail_ratio:.2%}")
    print(f"P50 응답시간: {total.get_response_time_percentile(0.5):.0f}ms")
    print(f"P95 응답시간: {total.get_response_time_percentile(0.95):.0f}ms")
    print(f"P99 응답시간: {total.get_response_time_percentile(0.99):.0f}ms")
    print(f"처리량(RPS): {total.current_rps:.1f}")

    p95 = total.get_response_time_percentile(0.95)
    error_rate = total.fail_ratio

    print("\n판정:")
    if p95 <= 2000 and error_rate <= 0.01:
        print("✅ PASS — P95 ≤ 2s, 에러율 ≤ 1%")
        environment.process_exit_code = 0
    else:
        reasons = []
        if p95 > 2000:
            reasons.append(f"P95 {p95:.0f}ms > 2000ms")
        if error_rate > 0.01:
            reasons.append(f"에러율 {error_rate:.2%} > 1%")
        print(f"❌ FAIL — {', '.join(reasons)}")
        environment.process_exit_code = 1
