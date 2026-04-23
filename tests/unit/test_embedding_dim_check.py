"""
S3 Phase 0 / FG 0-2 — `app.db.embedding_dim_check` 유닛 테스트.

작업지시서 §4.3 기준 ≥4건.

본 파일은 **단위 테스트** 이므로 실 DB 를 쓰지 않는다. Mock connection 으로
`pg_attribute` 조회 결과를 시뮬레이션해 각 분기를 덮는다. 실 DB 통합
검증은 `backend/tests/integration/test_it07_embedding_dim_consistency.py`
에서 수행된다.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from app.db.embedding_dim_check import (
    DimCheckResult,
    EmbeddingDimMismatchError,
    _parse_vector_dim,
    check_embedding_dim,
    get_db_embedding_column_info,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# 헬퍼
# --------------------------------------------------------------------------- #


def _make_conn_with_fetch(fetch_result: Optional[dict]) -> MagicMock:
    """psycopg2 connection 을 흉내 — cursor().execute() 후 fetchone() 이 `fetch_result` 반환."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    cur.fetchone = MagicMock(return_value=fetch_result)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn


# --------------------------------------------------------------------------- #
# 1) vector dim parsing
# --------------------------------------------------------------------------- #


class TestParseVectorDim:
    def test_parses_standard_format(self):
        assert _parse_vector_dim("vector(768)") == 768
        assert _parse_vector_dim("vector(1536)") == 1536

    def test_parses_with_spaces(self):
        assert _parse_vector_dim("vector ( 384 )") == 384

    def test_returns_none_for_non_vector_type(self):
        assert _parse_vector_dim("text") is None
        assert _parse_vector_dim("jsonb") is None

    def test_returns_none_for_missing_dim(self):
        assert _parse_vector_dim("vector") is None

    def test_returns_none_for_empty(self):
        assert _parse_vector_dim("") is None
        assert _parse_vector_dim(None) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 2) get_db_embedding_column_info
# --------------------------------------------------------------------------- #


class TestGetDbEmbeddingColumnInfo:
    def test_column_absent_returns_false(self):
        conn = _make_conn_with_fetch(None)
        present, dim, formatted = get_db_embedding_column_info(conn)
        assert present is False
        assert dim is None
        assert formatted is None

    def test_column_present_with_dim(self):
        conn = _make_conn_with_fetch({"formatted_type": "vector(768)"})
        present, dim, formatted = get_db_embedding_column_info(conn)
        assert present is True
        assert dim == 768
        assert formatted == "vector(768)"

    def test_column_present_without_dim(self):
        """vector 타입이지만 차원을 못 뽑아낸 비정상 케이스."""
        conn = _make_conn_with_fetch({"formatted_type": "vector"})
        present, dim, formatted = get_db_embedding_column_info(conn)
        assert present is True
        assert dim is None
        assert formatted == "vector"

    def test_exception_returns_safe_fallback(self):
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("DB 접속 불가")
        present, dim, formatted = get_db_embedding_column_info(conn)
        assert present is False
        assert dim is None
        assert formatted is None


# --------------------------------------------------------------------------- #
# 3) check_embedding_dim — 네 가지 분기
# --------------------------------------------------------------------------- #


class TestCheckEmbeddingDim:
    def test_column_absent_is_ok(self, monkeypatch):
        """컬럼 부재는 현재 Milvus 중심 아키텍처 기대값 — ok=True."""
        monkeypatch.setenv("EMBEDDING_DIM", "768")
        conn = _make_conn_with_fetch(None)
        result = check_embedding_dim(conn, check_milvus=False)
        assert result.column_present is False
        assert result.match is None
        assert result.ok is True
        assert "부재" in result.reason

    def test_column_present_and_match(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_DIM", "768")
        conn = _make_conn_with_fetch({"formatted_type": "vector(768)"})
        result = check_embedding_dim(conn, check_milvus=False)
        assert result.column_present is True
        assert result.db_dim == 768
        assert result.match is True
        assert result.ok is True

    def test_column_present_and_mismatch(self, monkeypatch):
        """config=768 vs DB vector(1536) → BUG-04 케이스 — ok=False."""
        monkeypatch.setenv("EMBEDDING_DIM", "768")
        conn = _make_conn_with_fetch({"formatted_type": "vector(1536)"})
        result = check_embedding_dim(conn, check_milvus=False)
        assert result.column_present is True
        assert result.db_dim == 1536
        assert result.config_dim == 768
        assert result.match is False
        assert result.ok is False
        assert "불일치" in result.reason

    def test_to_dict_shape(self, monkeypatch):
        """healthcheck 직렬화 형태 확인."""
        monkeypatch.setenv("EMBEDDING_DIM", "768")
        conn = _make_conn_with_fetch({"formatted_type": "vector(768)"})
        d = check_embedding_dim(conn, check_milvus=False).to_dict()
        assert d["config"] == 768
        assert d["db"] == 768
        assert d["match"] is True
        assert d["column_present"] is True
        assert "reason" in d
        assert "milvus" not in d  # check_milvus=False 이면 포함되지 않음

    def test_env_override_beats_settings(self, monkeypatch):
        """환경변수 EMBEDDING_DIM 이 settings 보다 우선한다."""
        monkeypatch.setenv("EMBEDDING_DIM", "1024")
        conn = _make_conn_with_fetch({"formatted_type": "vector(768)"})
        result = check_embedding_dim(conn, check_milvus=False)
        assert result.config_dim == 1024
        assert result.db_dim == 768
        assert result.match is False


# --------------------------------------------------------------------------- #
# 4) healthcheck 엔드포인트 — monkeypatch 로 get_db + check 대체
# --------------------------------------------------------------------------- #


class TestHealthcheckIntegration:
    def test_health_returns_embedding_dim_block_match(self, monkeypatch):
        """정합 상태 healthcheck — data.embedding_dim.match=true."""
        from fastapi.testclient import TestClient

        # check_embedding_dim 을 모듈 수준에서 교체
        def _fake_check(conn, *, check_milvus=False):
            return DimCheckResult(
                config_dim=768, db_dim=768, column_present=True, match=True,
                column_type="vector(768)", reason="일치",
            )

        import app.api.v1.system as sys_router
        import app.db.embedding_dim_check as check_mod
        monkeypatch.setattr(check_mod, "check_embedding_dim", _fake_check)

        # get_db 를 dummy context 로 교체
        @contextmanager
        def _fake_get_db():
            conn = MagicMock()
            conn.rollback = MagicMock()
            yield conn

        monkeypatch.setattr("app.db.get_db", _fake_get_db)

        # system.py 가 import 시점에 로컬 바인딩을 만드는지 재확인 —
        # 본 테스트는 함수 내부 import 이므로 patch 가 매번 적용된다.
        from app.main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v1/system/health")
        assert resp.status_code == 200
        body = resp.json()
        data = body.get("data", body)
        assert data["healthy"] is True
        assert "embedding_dim" in data
        assert data["embedding_dim"]["match"] is True
        assert data["embedding_dim"]["config"] == 768
        assert data["embedding_dim"]["db"] == 768

    def test_health_reports_mismatch_as_degraded(self, monkeypatch):
        """불일치 healthcheck — healthy=false + degraded=true."""
        from fastapi.testclient import TestClient

        def _fake_check(conn, *, check_milvus=False):
            return DimCheckResult(
                config_dim=768, db_dim=1536, column_present=True, match=False,
                column_type="vector(1536)", reason="불일치",
            )

        import app.db.embedding_dim_check as check_mod
        monkeypatch.setattr(check_mod, "check_embedding_dim", _fake_check)

        @contextmanager
        def _fake_get_db():
            conn = MagicMock()
            conn.rollback = MagicMock()
            yield conn

        monkeypatch.setattr("app.db.get_db", _fake_get_db)

        from app.main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v1/system/health")
        assert resp.status_code == 200
        data = resp.json().get("data", {})
        assert data["healthy"] is False
        assert data.get("degraded") is True
        assert data["embedding_dim"]["match"] is False

    def test_health_column_absent_is_healthy(self, monkeypatch):
        """컬럼 부재 (현재 아키텍처) 는 healthy=true + match=null."""
        from fastapi.testclient import TestClient

        def _fake_check(conn, *, check_milvus=False):
            return DimCheckResult(
                config_dim=768, db_dim=None, column_present=False, match=None,
                column_type=None, reason="컬럼 부재",
            )

        import app.db.embedding_dim_check as check_mod
        monkeypatch.setattr(check_mod, "check_embedding_dim", _fake_check)

        @contextmanager
        def _fake_get_db():
            conn = MagicMock()
            conn.rollback = MagicMock()
            yield conn

        monkeypatch.setattr("app.db.get_db", _fake_get_db)

        from app.main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v1/system/health")
        assert resp.status_code == 200
        data = resp.json().get("data", {})
        assert data["healthy"] is True
        # degraded 는 없거나 False
        assert data.get("degraded") in (None, False)
        assert data["embedding_dim"]["match"] is None
        assert data["embedding_dim"]["column_present"] is False


# --------------------------------------------------------------------------- #
# 5) Alembic revision 동작 — 직접 함수 호출로 검증
# --------------------------------------------------------------------------- #


class TestAlembicRevision:
    """revision upgrade() 가 불일치 시 EmbeddingDimMismatchError 를 던지는지 확인."""

    def _load_revision_module(self):
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[2]
            / "app/db/migrations/versions/20260423_1500_s3_p0_embedding_dim_check.py"
        )
        spec = importlib.util.spec_from_file_location("_s3p0_rev", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod

    def test_upgrade_passes_when_column_absent(self, monkeypatch):
        mod = self._load_revision_module()

        # op.get_bind().connection 을 가짜로
        fake_bind = SimpleNamespace(connection=_make_conn_with_fetch(None))
        monkeypatch.setattr(mod.op, "get_bind", lambda: fake_bind)
        monkeypatch.setenv("EMBEDDING_DIM", "768")

        # 예외 없이 반환되어야 한다
        mod.upgrade()

    def test_upgrade_fails_on_mismatch(self, monkeypatch):
        mod = self._load_revision_module()

        fake_bind = SimpleNamespace(
            connection=_make_conn_with_fetch({"formatted_type": "vector(1536)"})
        )
        monkeypatch.setattr(mod.op, "get_bind", lambda: fake_bind)
        monkeypatch.setenv("EMBEDDING_DIM", "768")

        with pytest.raises(EmbeddingDimMismatchError) as exc_info:
            mod.upgrade()
        msg = str(exc_info.value)
        assert "불일치" in msg
        assert "768" in msg
        assert "1536" in msg

    def test_downgrade_is_noop(self):
        mod = self._load_revision_module()
        assert mod.downgrade() is None

    def test_revision_metadata(self):
        mod = self._load_revision_module()
        assert mod.revision == "s3_p0_embedding_dim_check"
        assert mod.down_revision == "p7_2_c_uppercase_doc_type"
