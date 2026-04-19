"""
부하 테스트용 시드 데이터 생성/정리 스크립트 (PH5-CARRY-003).

실행:
  python scripts/seed_load_test_data.py --action seed --backend-url http://localhost:8050
  python scripts/seed_load_test_data.py --action cleanup --backend-url http://localhost:8050

주의: 스테이징 환경 전용. 프로덕션에서 절대 실행 금지 (지침 7-12-2).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from urllib.parse import urlparse

import httpx

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "staging.mimir.internal"}
_DOC_COUNT = 100
_AGENT_COUNT = 100


def _guard_staging(backend_url: str) -> None:
    hostname = urlparse(backend_url).hostname or ""
    if hostname not in _ALLOWED_HOSTS:
        sys.exit(f"[BLOCKED] 허용되지 않은 호스트: {hostname}. 스테이징 환경에서만 실행하세요.")


async def seed(backend_url: str, admin_token: str) -> None:
    _guard_staging(backend_url)

    async with httpx.AsyncClient(base_url=backend_url, timeout=30.0) as client:
        headers = {
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
        }

        print(f"문서 시드 데이터 생성 중... ({_DOC_COUNT}개)")
        doc_errors = 0
        for i in range(1, _DOC_COUNT + 1):
            resp = await client.post(
                "/api/v1/admin/documents",
                json={
                    "id": f"load-test-doc-{i:04d}",
                    "title": f"부하 테스트 문서 {i}",
                    "content": f"이 문서는 부하 테스트를 위해 생성되었습니다. 문서 번호: {i}",
                    "type_code": "general",
                },
                headers=headers,
            )
            if resp.status_code not in (200, 201, 409):
                doc_errors += 1
                print(f"  경고: 문서 {i} 생성 실패 ({resp.status_code})")

        print(f"에이전트 시드 데이터 생성 중... ({_AGENT_COUNT}개)")
        agent_errors = 0
        for i in range(1, _AGENT_COUNT + 1):
            resp = await client.post(
                "/api/v1/admin/agents",
                json={
                    "name": f"load-test-agent-{i:04d}",
                    "api_key": f"mim_sk_test_{i:04d}",
                    "expires_at": "2099-12-31T00:00:00Z",
                    "scope_profile_id": "default-scope",
                },
                headers=headers,
            )
            if resp.status_code not in (200, 201, 409):
                agent_errors += 1
                print(f"  경고: 에이전트 {i} 생성 실패 ({resp.status_code})")

        print(
            f"시드 완료: 문서 {_DOC_COUNT - doc_errors}개, "
            f"에이전트 {_AGENT_COUNT - agent_errors}개 생성"
        )
        if doc_errors or agent_errors:
            print(f"  실패: 문서 {doc_errors}건, 에이전트 {agent_errors}건")


async def cleanup(backend_url: str, admin_token: str) -> None:
    _guard_staging(backend_url)

    async with httpx.AsyncClient(base_url=backend_url, timeout=30.0) as client:
        headers = {"Authorization": f"Bearer {admin_token}"}

        print("테스트 데이터 정리 중...")
        for i in range(1, _DOC_COUNT + 1):
            await client.delete(
                f"/api/v1/admin/documents/load-test-doc-{i:04d}",
                headers=headers,
            )
        for i in range(1, _AGENT_COUNT + 1):
            await client.delete(
                f"/api/v1/admin/agents/load-test-agent-{i:04d}",
                headers=headers,
            )
        print("테스트 데이터 정리 완료")


def main() -> None:
    parser = argparse.ArgumentParser(description="부하 테스트 시드 데이터 관리")
    parser.add_argument("--action", choices=["seed", "cleanup"], required=True)
    parser.add_argument("--backend-url", default="http://localhost:8050")
    parser.add_argument("--admin-token", default="admin-test-token")
    args = parser.parse_args()

    if args.action == "seed":
        asyncio.run(seed(args.backend_url, args.admin_token))
    else:
        asyncio.run(cleanup(args.backend_url, args.admin_token))


if __name__ == "__main__":
    main()
