"""
backend/tests/integration/ 전용 pytest 픽스처.

설계 원칙 (FG 0-1):
  - 실 PostgreSQL(+pgvector) + Valkey 위에서 실행되는 통합 테스트의 공통 토대.
  - CI 모드: `USE_EXISTING_DB=1` — 외부에서 기동된 서비스(POSTGRES_HOST 등)에 접속.
  - 로컬 모드: testcontainers 가 설치되어 있으면 자동으로 컨테이너 기동.
  - 둘 다 불가능하면 전체 integration 테스트를 `skip` 처리 (단위 테스트는 영향 없음).

세션 scope 픽스처가 DB 를 1회만 기동하고 `init_db()` + `alembic upgrade head` 를 수행한다.
함수 scope `db_conn` 은 외부 savepoint 를 열고 테스트 종료 후 롤백하여 격리를 유지한다.

CI 환경변수 예:
    USE_EXISTING_DB=1
    POSTGRES_HOST=localhost
    POSTGRES_PORT=5432
    POSTGRES_USER=mimir_test
    POSTGRES_PASSWORD=<ci-secret>
    POSTGRES_DB=mimir_test
    VALKEY_HOST=localhost
    VALKEY_PORT=6379
    JWT_SECRET=<ci-secret>
    INTERNAL_SERVICE_SECRET=<ci-secret>
    ENVIRONMENT=test
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from typing import Any, Generator

import pytest

# --------------------------------------------------------------------------- #
# 공통: 경로 / 환경 기본값
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve()
_BACKEND_ROOT = _HERE.parents[2]  # backend/
_REPO_ROOT = _BACKEND_ROOT.parent

# backend/ 를 sys.path 최상위로 올려 `app.*` 임포트를 보장
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

# 테스트 모드 강제 — app.config.Settings 가 production 시크릿 검증을 우회하도록.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DEBUG", "true")

# --------------------------------------------------------------------------- #
# 세션 scope: DB / Valkey 엔드포인트 확보
# --------------------------------------------------------------------------- #


def _truthy(v: str | None) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on") if v else False


def _use_existing_db() -> bool:
    """CI 가 이미 서비스 컨테이너를 기동해 두었는가."""
    return _truthy(os.environ.get("USE_EXISTING_DB"))


def _integration_disabled() -> bool:
    """사용자가 명시적으로 통합 테스트를 비활성화했는가.

    (예: `pytest -m "not integration"` 이 아닌, 환경변수로도 스킵 가능)
    """
    return _truthy(os.environ.get("SKIP_INTEGRATION_TESTS"))


@pytest.fixture(scope="session")
def _postgres_endpoint() -> Generator[dict[str, Any], None, None]:
    """세션 당 1회만 PostgreSQL(+pgvector) 엔드포인트를 확보한다.

    우선순위:
      1) USE_EXISTING_DB=1 → 환경변수에서 그대로 읽음
      2) testcontainers → pgvector/pgvector:pg16 이미지로 컨테이너 기동
      3) 둘 다 불가능 → `pytest.skip` 으로 전체 integration 스킵
    """
    if _integration_disabled():
        pytest.skip("SKIP_INTEGRATION_TESTS=1 — integration 테스트 비활성화")

    if _use_existing_db():
        host = os.environ.get("POSTGRES_HOST", "localhost")
        port = int(os.environ.get("POSTGRES_PORT", "5432"))
        user = os.environ.get("POSTGRES_USER", "master")
        pw = os.environ.get("POSTGRES_PASSWORD", "")
        db = os.environ.get("POSTGRES_DB", "mimir")
        yield {
            "host": host,
            "port": port,
            "user": user,
            "password": pw,
            "db": db,
            "url": f"postgresql://{user}:{pw}@{host}:{port}/{db}",
            "mode": "existing",
        }
        return

    # testcontainers 경로
    try:
        from testcontainers.postgres import PostgresContainer  # type: ignore
    except Exception as exc:  # pragma: no cover - import guard
        pytest.skip(
            "integration 테스트 실행 불가: `USE_EXISTING_DB=1` 도 아니고 "
            f"testcontainers 도 사용 불가 ({exc!r}). "
            "CI 에서는 services 블록을 사용하거나, 로컬에서는 "
            "`pip install testcontainers[postgres]` 후 Docker 를 띄우세요."
        )
        return

    # pgvector 지원 이미지. digest pin 이 권장되나, 테스트 환경 편의를 위해 태그 pin.
    image = os.environ.get("INTEGRATION_PG_IMAGE", "pgvector/pgvector:pg16")
    container = PostgresContainer(image, username="mimir_test", password=secrets.token_urlsafe(16), dbname="mimir_test")
    container.start()
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(5432))
        yield {
            "host": host,
            "port": port,
            "user": container.username,
            "password": container.password,
            "db": container.dbname,
            "url": f"postgresql://{container.username}:{container.password}@{host}:{port}/{container.dbname}",
            "mode": "testcontainers",
        }
    finally:
        container.stop()


@pytest.fixture(scope="session")
def _valkey_endpoint() -> Generator[dict[str, Any], None, None]:
    """세션 당 1회만 Valkey(Redis 호환) 엔드포인트를 확보한다.

    Valkey 를 요구하지 않는 테스트가 많고, 기동 비용이 있으므로 **lazy** 로 둔다.
    실제로 valkey 가 필요한 테스트에서만 이 픽스처에 의존하게 설계한다.
    """
    if _integration_disabled():
        pytest.skip("SKIP_INTEGRATION_TESTS=1")

    if _use_existing_db():
        yield {
            "host": os.environ.get("VALKEY_HOST", "localhost"),
            "port": int(os.environ.get("VALKEY_PORT", "6379")),
            "mode": "existing",
        }
        return

    try:
        from testcontainers.core.container import DockerContainer  # type: ignore
        from testcontainers.core.waiting_utils import wait_for_logs  # type: ignore
    except Exception as exc:  # pragma: no cover - import guard
        pytest.skip(f"Valkey testcontainers 불가: {exc!r}")
        return

    image = os.environ.get("INTEGRATION_VALKEY_IMAGE", "valkey/valkey:8")
    container = DockerContainer(image).with_exposed_ports(6379)
    container.start()
    try:
        # 로그 기반 ready 체크. Valkey 8 은 "Ready to accept connections" 를 출력.
        wait_for_logs(container, "Ready to accept connections", timeout=30)
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(6379))
        yield {"host": host, "port": port, "mode": "testcontainers"}
    finally:
        container.stop()


# --------------------------------------------------------------------------- #
# 세션 scope: 스키마 생성 (pgvector + init_db + alembic)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def _apply_db_env(_postgres_endpoint) -> Generator[dict[str, Any], None, None]:
    """POSTGRES_* 환경변수를 세션 전체에 주입한다.

    `app.config.settings` 는 import 시점에 값이 읽히므로, import 이전에 값이 세팅되어야 한다.
    `conftest.py` 는 pytest collection 시 가장 먼저 실행되지만, 픽스처 단계에서는 이미
    `app.config` 가 import 되어 있을 수 있다. 따라서 필요 시 `settings` 를 재빌드한다.
    """
    ep = _postgres_endpoint
    os.environ["POSTGRES_HOST"] = ep["host"]
    os.environ["POSTGRES_PORT"] = str(ep["port"])
    os.environ["POSTGRES_USER"] = ep["user"]
    os.environ["POSTGRES_PASSWORD"] = ep["password"]
    os.environ["POSTGRES_DB"] = ep["db"]

    # settings 재빌드 — import 이후에 환경이 바뀌었으므로.
    import importlib

    import app.config as _cfg  # noqa: WPS433

    importlib.reload(_cfg)
    # 연결 풀도 새 설정을 반영하도록 재생성 플래그 처리.
    import app.db.connection as _conn  # noqa: WPS433

    _conn._pool = None  # type: ignore[attr-defined]

    yield ep


@pytest.fixture(scope="session")
def _apply_schema(_apply_db_env) -> Generator[dict[str, Any], None, None]:
    """pgvector 확장 + 애플리케이션 스키마 + Alembic HEAD 까지 적용한다."""
    ep = _apply_db_env

    import psycopg2

    # 1) pgvector 확장 (이미지가 pgvector 를 포함하면 CREATE EXTENSION 만으로 동작)
    conn = psycopg2.connect(
        host=ep["host"], port=ep["port"], user=ep["user"], password=ep["password"], dbname=ep["db"]
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception as exc:  # pragma: no cover - env guard
                pytest.skip(
                    "pgvector 확장을 만들 수 없습니다 (이미지가 pgvector 미포함일 가능성): "
                    f"{exc!r}. CI 는 pgvector/pgvector:pg16 이미지를 사용하세요."
                )
                return
    finally:
        conn.close()

    # 2) init_db() — 레거시 DDL 일괄 생성 (idempotent)
    from app.db import init_db

    init_db()

    # 3) Alembic upgrade head — S2-5 이후 마이그레이션 반영
    from alembic import command  # type: ignore
    from alembic.config import Config as AlembicConfig  # type: ignore

    alembic_ini = _BACKEND_ROOT / "alembic.ini"
    cfg = AlembicConfig(str(alembic_ini))
    # env.py 의 _resolve_database_url 이 settings.database_url 을 읽도록 환경만 세팅해 둔다.
    os.environ.pop("ALEMBIC_DATABASE_URL", None)
    os.environ.pop("ALEMBIC_POSTGRES_USER", None)
    os.environ.pop("ALEMBIC_POSTGRES_PASSWORD", None)
    command.upgrade(cfg, "head")

    yield ep


# --------------------------------------------------------------------------- #
# 세션 scope: FastAPI app 및 기본 시드
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def fastapi_app(_apply_schema):
    """FastAPI 앱 — 실 DB 스키마가 준비된 뒤에만 import 한다."""
    # startup 훅(init_db 등)이 실 DB 에 접속하려면 이 시점에는 POSTGRES_* 가 이미 세팅됨.
    from app.main import app
    return app


@pytest.fixture(scope="session")
def client(fastapi_app):
    """TestClient — startup 이벤트 실행 후 요청 가능."""
    from fastapi.testclient import TestClient

    with TestClient(fastapi_app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
async def async_client(fastapi_app):
    """AsyncClient — 비동기 엔드포인트 검증용."""
    import httpx
    from httpx import ASGITransport

    transport = ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# --------------------------------------------------------------------------- #
# 함수 scope: 트랜잭션 격리된 raw psycopg2 커넥션
# --------------------------------------------------------------------------- #


@pytest.fixture
def db_conn(_apply_schema) -> Generator[Any, None, None]:
    """함수 scope psycopg2 connection — 테스트 종료 후 롤백.

    주의: FastAPI 엔드포인트를 통해 변경되는 레코드는 별도 connection 에서 커밋되므로
    테스트 격리를 위해서는 엔드포인트 테스트 전/후 `TRUNCATE` 를 별도로 수행해야 한다.
    본 픽스처는 **직접 SQL 을 쓰는 레퍼런스 검증** 용이다.
    """
    import psycopg2
    import psycopg2.extras

    ep = _apply_schema
    conn = psycopg2.connect(
        host=ep["host"],
        port=ep["port"],
        user=ep["user"],
        password=ep["password"],
        dbname=ep["db"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    conn.autocommit = False
    try:
        yield conn
        conn.rollback()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 인증 헤더 — debug=True 개발 헤더 방식 (conftest 상위와 동일 포맷)
# --------------------------------------------------------------------------- #


@pytest.fixture
def auth_admin_header() -> dict[str, str]:
    return {"X-Actor-Id": "it-admin-001", "X-Actor-Role": "SUPER_ADMIN"}


@pytest.fixture
def auth_author_header() -> dict[str, str]:
    return {"X-Actor-Id": "it-author-001", "X-Actor-Role": "AUTHOR"}


@pytest.fixture
def auth_approver_header() -> dict[str, str]:
    return {"X-Actor-Id": "it-approver-001", "X-Actor-Role": "APPROVER"}


@pytest.fixture
def auth_viewer_header() -> dict[str, str]:
    return {"X-Actor-Id": "it-viewer-001", "X-Actor-Role": "VIEWER"}


# --------------------------------------------------------------------------- #
# 마커 등록 — pyproject.toml 의 markers 와 중복되지 않게 here 에서는 재정의만
# --------------------------------------------------------------------------- #


def pytest_collection_modifyitems(config, items):
    """이 디렉터리 아래의 모든 테스트에 `integration` 마커 자동 부여."""
    integration_mark = pytest.mark.integration
    for item in items:
        if "tests/integration/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(integration_mark)


# --------------------------------------------------------------------------- #
# 상위 레벨 헬퍼 — 문서 생성 / Draft 저장 / 워크플로 전이를 test 에서 한 줄로 호출
#
# 이들은 TestClient 기반 fixture 로, 실 DB 상에서 엔드포인트를 호출해
# IT-01 ~ IT-10 시나리오를 DRY 하게 쓰도록 돕는다.
# --------------------------------------------------------------------------- #


def _unwrap_data(resp_json: Any) -> Any:
    """success_response envelope 의 `.data` 를 꺼낸다 (없으면 원본)."""
    if isinstance(resp_json, dict) and "data" in resp_json:
        return resp_json["data"]
    return resp_json


@pytest.fixture
def make_document(client, auth_author_header):
    """문서 생성 헬퍼 — (document_id, doc_dict) 반환."""
    def _create(
        *,
        title: str = "IT-samplé 문서",
        document_type: str = "policy",
        headers: dict[str, str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        h = dict(headers or auth_author_header)
        body = {"title": title, "document_type": document_type}
        if metadata is not None:
            body["metadata"] = metadata
        resp = client.post("/api/v1/documents", json=body, headers=h)
        assert resp.status_code == 201, f"문서 생성 실패: {resp.status_code} / {resp.text[:300]}"
        data = _unwrap_data(resp.json())
        return data["id"], data

    return _create


@pytest.fixture
def save_initial_draft(client, auth_author_header):
    """단일 paragraph 를 가진 초기 Draft 저장 헬퍼 — version_id 반환."""
    def _save(
        document_id: str,
        *,
        title: str = "초안 제목",
        paragraph_text: str = "본문 단락. pgvector / FTS 둘 다 인덱싱됩니다.",
        headers: dict[str, str] | None = None,
    ) -> str:
        h = dict(headers or auth_author_header)
        body = {
            "title": title,
            "summary": "초안 요약",
            "change_summary": "initial draft for integration test",
            "content_snapshot": {
                "type": "document",
                "children": [
                    {"type": "paragraph", "content": paragraph_text},
                ],
            },
        }
        resp = client.put(f"/api/v1/documents/{document_id}/draft", json=body, headers=h)
        assert resp.status_code == 200, f"Draft 저장 실패: {resp.status_code} / {resp.text[:300]}"
        return _unwrap_data(resp.json())["id"]

    return _save


@pytest.fixture
def run_workflow_action(client):
    """임의의 워크플로 액션(`submit-review` / `approve` / `publish` …) 실행 헬퍼."""
    def _run(
        document_id: str,
        version_id: str,
        action_slug: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
        expected_status: int = 200,
    ):
        url = f"/api/v1/documents/{document_id}/versions/{version_id}/workflow/{action_slug}"
        resp = client.post(url, json=body or {}, headers=headers)
        assert resp.status_code == expected_status, (
            f"{action_slug} 실패 (expected {expected_status}): "
            f"{resp.status_code} / {resp.text[:300]}"
        )
        return resp

    return _run
