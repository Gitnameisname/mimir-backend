"""
IT-00 — FG 0-1 Block A 인프라 베이스라인 스모크.

목적:
  - conftest 픽스처가 실 PostgreSQL(+pgvector) + Valkey + FastAPI app 을 올바르게 조립했는지 검증.
  - IT-01 ~ IT-10 시나리오(Block B) 에 들어가기 전 인프라 사전 점검용.

검증 포인트:
  1. DB 접속 가능
  2. pgvector 확장 설치됨 (BUG-04 회귀 탐지 기반)
  3. Alembic HEAD 가 적용됨 (alembic_version.version_num 존재)
  4. init_db() 가 실 DB 에 핵심 테이블들을 만들었음
  5. `/api/v1/system/health` 가 200 (앱 startup 이 DB 없이도 죽지 않고, DB 있으면 정상 응답)
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# 1) DB 접속 + pgvector 확장
# --------------------------------------------------------------------------- #


def test_it00_db_connection_reachable(db_conn) -> None:
    """DB 연결이 살아있고, 단순 SELECT 가 성공한다."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        row = cur.fetchone()
    assert row and row["ok"] == 1


def test_it00_pgvector_extension_installed(db_conn) -> None:
    """pgvector 확장이 설치됐다 (이미지가 pgvector/pgvector:pg16 이어야 함)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extname, extversion FROM pg_extension WHERE extname = %s",
            ("vector",),
        )
        row = cur.fetchone()
    assert row is not None, (
        "pgvector 확장이 없습니다. CI services 의 이미지를 pgvector/pgvector:pg16 으로 설정하거나 "
        "로컬에서 INTEGRATION_PG_IMAGE=pgvector/pgvector:pg16 환경변수를 확인하세요."
    )
    # 버전 문자열은 환경마다 다를 수 있으나 비어있지 않아야 한다.
    assert row["extversion"], "pg_extension.extversion 이 비어 있음"


# --------------------------------------------------------------------------- #
# 2) Alembic HEAD + 핵심 스키마
# --------------------------------------------------------------------------- #


def test_it00_alembic_head_applied(db_conn) -> None:
    """conftest 의 `alembic upgrade head` 가 성공적으로 적용됐다."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'alembic_version'
            ) AS exists
            """
        )
        row = cur.fetchone()
    assert row and row["exists"], "alembic_version 테이블이 없음 — migration 미적용"

    with db_conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        versions = [r["version_num"] for r in cur.fetchall()]
    assert versions, "alembic_version.version_num 이 비어 있음 — upgrade head 실패"


@pytest.mark.parametrize(
    "table_name",
    [
        "documents",
        "versions",
        "document_chunks",
        "users",
        "scope_profiles",
        "audit_events",
    ],
)
def test_it00_core_tables_present(db_conn, table_name: str) -> None:
    """init_db() + Alembic 이후 핵심 테이블들이 존재한다."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
            ) AS exists
            """,
            (table_name,),
        )
        row = cur.fetchone()
    assert row and row["exists"], f"테이블 '{table_name}' 이 없음"


# --------------------------------------------------------------------------- #
# 3) FastAPI app 기동 + 헬스체크
# --------------------------------------------------------------------------- #


def test_it00_fastapi_health_ok(client) -> None:
    """앱 startup 이 DB 연결 성공 상태에서 완료되고, /system/health 가 200."""
    resp = client.get("/api/v1/system/health")
    # 프로젝트에 따라 200 또는 다층 health (200/503) 일 수 있으나,
    # DB 가 정상이면 최소 200 이어야 한다.
    assert resp.status_code == 200, f"health 비정상: {resp.status_code} / {resp.text[:200]}"


def test_it00_capabilities_endpoint_ok(client) -> None:
    """Capabilities 엔드포인트가 200 응답하고 최소 필드를 포함한다 (Phase 1 준비 확인)."""
    resp = client.get("/api/v1/capabilities")
    if resp.status_code == 404:
        pytest.skip("/capabilities 엔드포인트 미노출 — 환경 설정 차이로 스킵")
    assert resp.status_code == 200, f"capabilities 비정상: {resp.status_code}"
    data = resp.json()
    # 최소 계약: dict 여야 하고, 일부 키 포함 예상 (존재만 확인)
    assert isinstance(data, dict)


# --------------------------------------------------------------------------- #
# 4) 테스트 격리 확인 — 연속 테스트가 서로 영향 주지 않아야 함
# --------------------------------------------------------------------------- #


def test_it00_isolation_first_write(db_conn) -> None:
    """첫 테스트에서 임시 테이블에 쓰기."""
    with db_conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _it00_tmp (v INT)")
        cur.execute("INSERT INTO _it00_tmp (v) VALUES (42)")
        cur.execute("SELECT COUNT(*) AS c FROM _it00_tmp")
        assert cur.fetchone()["c"] == 1


def test_it00_isolation_second_read(db_conn) -> None:
    """두 번째 테스트에선 앞 테스트의 TEMP 테이블이 보이지 않아야 한다 (세션 격리)."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema LIKE 'pg_temp%' AND table_name = '_it00_tmp'
            ) AS exists
            """
        )
        row = cur.fetchone()
    # TEMP 테이블은 connection 수명에 묶이므로, 새 connection 에선 보이지 않아야 한다.
    assert not row["exists"], "TEMP 테이블이 테스트 경계를 넘어 공유됨 — 격리 실패"
