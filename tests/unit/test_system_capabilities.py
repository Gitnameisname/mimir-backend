"""
/api/v1/system/capabilities 3-tier 엔드포인트 단위 테스트 — Task 0-8.

보안 분리 검증:
  - Tier 1 (health): 인증 불필요, 내부 구성 정보 미노출
  - Tier 2 (capabilities): 인증 필요, rag/chunking만 노출
  - Tier 3 (admin/system/capabilities): Admin 전용, 전체 정보 노출

테스트 대상:
  - 인증 계층별 접근 제어 (401/403)
  - 정보 격리 (tier별 비노출 필드 검증)
  - PGVECTOR_ENABLED 환경변수 on/off 별 응답 (Tier 3)
  - rag_available: pgvector + LLM 키 조합 검증 (Tier 3)
  - Cache-Control 헤더 분리 (public vs private)
  - 5분 캐시 TTL 로직
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 헬퍼: 캐시 초기화 (각 테스트 간 격리)
# ---------------------------------------------------------------------------

def _reset_cap_cache() -> None:
    """system.py 모듈 레벨 캐시를 초기화한다."""
    import app.api.v1.system as sys_mod
    sys_mod._cap_cache["data"] = None
    sys_mod._cap_cache["expires"] = datetime.min.replace(tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def reset_cache():
    _reset_cap_cache()
    yield
    _reset_cap_cache()


# ===========================================================================
# Tier 1: /api/v1/system/health (인증 불필요)
# ===========================================================================

class TestHealthTier1:
    """Tier 1 — health 엔드포인트는 인증 없이 접근 가능하며, 내부 정보를 노출하지 않는다."""

    def test_accessible_without_auth(self, client):
        """인증 없이 200 반환."""
        r = client.get("/api/v1/system/health")
        assert r.status_code == 200

    def test_no_internal_info_exposed(self, client):
        """health 응답에 내부 구성 정보가 포함되지 않는다."""
        r = client.get("/api/v1/system/health")
        data = r.json().get("data", {})
        # 내부 구성 필드 미노출 확인
        for field in ("pgvector_enabled", "supported_providers",
                       "rag_available", "chunking_enabled",
                       "deployment_type", "closed_network"):
            assert field not in data, f"health에 '{field}' 노출됨 — Tier 1 위반"


# ===========================================================================
# Tier 2: /api/v1/system/capabilities (인증 필요)
# ===========================================================================

class TestCapabilitiesTier2:
    """Tier 2 — capabilities는 인증된 사용자만 접근 가능하고, 내부 구성 정보를 제외한다."""

    def test_unauthenticated_returns_401(self, client):
        """인증 없이 접근 시 401."""
        r = client.get("/api/v1/system/capabilities")
        assert r.status_code == 401

    def test_authenticated_viewer_returns_200(self, client, auth_viewer):
        """인증된 VIEWER 접근 가능."""
        r = client.get("/api/v1/system/capabilities", headers=auth_viewer)
        assert r.status_code == 200

    def test_response_includes_tier2_fields(self, client, auth_viewer):
        """Tier 2 응답에 rag_available, chunking_enabled, version 포함."""
        r = client.get("/api/v1/system/capabilities", headers=auth_viewer)
        data = r.json()["data"]
        for field in ("version", "rag_available", "chunking_enabled"):
            assert field in data, f"Tier 2 응답에 '{field}' 없음"

    def test_response_excludes_admin_fields(self, client, auth_viewer):
        """Tier 2 응답에 pgvector_enabled, supported_providers 등 미포함."""
        r = client.get("/api/v1/system/capabilities", headers=auth_viewer)
        data = r.json()["data"]
        for field in ("pgvector_enabled", "supported_providers",
                       "deployment_type", "closed_network"):
            assert field not in data, f"Tier 2에 '{field}' 노출됨 — 정보 격리 위반"

    def test_cache_control_private(self, client, auth_viewer):
        """Tier 2 Cache-Control은 private."""
        r = client.get("/api/v1/system/capabilities", headers=auth_viewer)
        cc = r.headers.get("cache-control", "")
        assert "private" in cc, "Tier 2 캐시가 public으로 설정됨"
        assert "max-age=300" in cc

    def test_mcp_spec_version_is_none(self, client, auth_viewer):
        """현 Phase에서 mcp_spec_version=null."""
        r = client.get("/api/v1/system/capabilities", headers=auth_viewer)
        assert r.json()["data"]["mcp_spec_version"] is None


# ===========================================================================
# Tier 3: /api/v1/admin/system/capabilities (Admin 전용)
# ===========================================================================

class TestAdminCapabilitiesTier3:
    """Tier 3 — admin capabilities는 Admin만 접근 가능하고, 전체 정보를 반환한다."""

    def test_unauthenticated_returns_401(self, client):
        """인증 없이 접근 시 401."""
        r = client.get("/api/v1/admin/system/capabilities")
        assert r.status_code == 401

    def test_viewer_returns_403(self, client, auth_viewer):
        """일반 VIEWER 접근 시 403."""
        r = client.get("/api/v1/admin/system/capabilities", headers=auth_viewer)
        assert r.status_code == 403

    def test_author_returns_403(self, client, auth_author):
        """AUTHOR 접근 시 403."""
        r = client.get("/api/v1/admin/system/capabilities", headers=auth_author)
        assert r.status_code == 403

    def test_admin_returns_200(self, client, auth_admin):
        """Admin(SUPER_ADMIN) 접근 가능."""
        r = client.get("/api/v1/admin/system/capabilities", headers=auth_admin)
        assert r.status_code == 200

    def test_full_response_schema(self, client, auth_admin):
        """Tier 3 응답에 전체 필드 포함."""
        r = client.get("/api/v1/admin/system/capabilities", headers=auth_admin)
        data = r.json()["data"]
        for field in ("version", "pgvector_enabled", "rag_available",
                       "chunking_enabled", "supported_providers",
                       "mcp_spec_version"):
            assert field in data, f"Tier 3 응답에 '{field}' 없음"

    def test_supported_providers_is_list(self, client, auth_admin):
        """supported_providers는 리스트."""
        r = client.get("/api/v1/admin/system/capabilities", headers=auth_admin)
        assert isinstance(r.json()["data"]["supported_providers"], list)

    def test_cache_control_private(self, client, auth_admin):
        """Tier 3 Cache-Control은 최소한 private 이상의 제약 (실제 구현은 no-store 로 더 엄격)."""
        r = client.get("/api/v1/admin/system/capabilities", headers=auth_admin)
        cc = r.headers.get("cache-control", "").lower()
        # private 또는 no-store 둘 중 하나 이상 있으면 OK (no-store 는 private 보다 엄격)
        assert ("private" in cc) or ("no-store" in cc)

    # --- pgvector / chunking / Milvus + RAG 조합 검증 (Tier 3에서만 가능) ---
    # 2026-05-11 정정: 본 시스템은 Milvus 가 벡터 정본. pgvector_enabled 응답은 정보성으로
    # 유지하되 RAG / chunking 가용성은 Milvus + PG document_chunks 기반으로 결정한다.

    def test_pgvector_enabled_true_reports_information(self, client, auth_admin):
        """PGVECTOR_ENABLED=true → pgvector_enabled=true (정보성 필드)."""
        _reset_cap_cache()
        with patch.dict("os.environ", {"PGVECTOR_ENABLED": "true"}):
            r = client.get("/api/v1/admin/system/capabilities", headers=auth_admin)
        cap = r.json()["data"]
        assert cap["pgvector_enabled"] is True

    def test_chunking_enabled_independent_of_pgvector(self, client, auth_admin):
        """chunking_enabled 은 PG document_chunks 기반이라 pgvector 와 무관 — 항상 True."""
        _reset_cap_cache()
        with patch.dict("os.environ", {"PGVECTOR_ENABLED": "false"}):
            r = client.get("/api/v1/admin/system/capabilities", headers=auth_admin)
        cap = r.json()["data"]
        # 정정 (logical fix): pgvector 가 비활성이어도 chunking 은 PG 기반이므로 가용.
        assert cap["pgvector_enabled"] is False
        assert cap["chunking_enabled"] is True

    def test_milvus_unavailable_rag_unavailable(self, client, auth_admin):
        """Milvus 미가용이면 LLM 키가 있어도 rag_available=false."""
        _reset_cap_cache()
        import app.api.v1.system as sys_mod
        from unittest.mock import MagicMock

        fake_null_milvus = MagicMock()
        fake_null_milvus.is_available = MagicMock(return_value=False)

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            with patch.object(sys_mod.settings, "openai_api_key", "sk-test"):
                with patch("app.db.milvus.get_milvus", return_value=fake_null_milvus):
                    r = client.get("/api/v1/admin/system/capabilities", headers=auth_admin)
        assert r.json()["data"]["rag_available"] is False

    def test_milvus_available_no_llm_rag_unavailable(self, client, auth_admin):
        """Milvus 가용 + LLM 부재 → rag_available=false.

        DB 의 llm_providers 가 이미 등록돼 있을 수 있어 has_llm 이 DB 경로로도 True 되지
        않도록 llm_providers 조회를 빈 결과로 패치.
        """
        _reset_cap_cache()
        import app.api.v1.system as sys_mod
        from unittest.mock import MagicMock

        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        fake_cur.fetchall = MagicMock(return_value=[])
        fake_conn = MagicMock()
        fake_conn.cursor = MagicMock(return_value=fake_cur)
        fake_ctx = MagicMock()
        fake_ctx.__enter__ = MagicMock(return_value=fake_conn)
        fake_ctx.__exit__ = MagicMock(return_value=False)

        fake_milvus = MagicMock()
        fake_milvus.is_available = MagicMock(return_value=True)

        with patch.object(sys_mod.settings, "openai_api_key", ""):
            with patch.object(sys_mod.settings, "anthropic_api_key", ""):
                with patch("app.db.connection.get_db", return_value=fake_ctx):
                    with patch("app.db.milvus.get_milvus", return_value=fake_milvus):
                        r = client.get("/api/v1/admin/system/capabilities", headers=auth_admin)
        assert r.json()["data"]["rag_available"] is False

    def test_milvus_available_with_openai_rag_available(self, client, auth_admin):
        """Milvus 가용 + openai_api_key 설정 → rag_available=true."""
        _reset_cap_cache()
        import app.api.v1.system as sys_mod
        from unittest.mock import MagicMock

        fake_milvus = MagicMock()
        fake_milvus.is_available = MagicMock(return_value=True)

        with patch.object(sys_mod.settings, "openai_api_key", "sk-test"):
            with patch("app.db.milvus.get_milvus", return_value=fake_milvus):
                r = client.get("/api/v1/admin/system/capabilities", headers=auth_admin)
        assert r.json()["data"]["rag_available"] is True

    # --- supported_providers 동작 ---

    def test_no_api_keys_empty_providers(self, client, auth_admin):
        """API 키 없으면 supported_providers=[]."""
        import app.api.v1.system as sys_mod
        _reset_cap_cache()
        with patch.object(sys_mod.settings, "openai_api_key", ""):
            with patch.object(sys_mod.settings, "anthropic_api_key", ""):
                r = client.get("/api/v1/admin/system/capabilities", headers=auth_admin)
        assert r.json()["data"]["supported_providers"] == []

    def test_openai_key_adds_openai_provider(self, client, auth_admin):
        """openai_api_key 설정 → supported_providers에 'openai' 포함."""
        import app.api.v1.system as sys_mod
        _reset_cap_cache()
        with patch.object(sys_mod.settings, "openai_api_key", "sk-test"):
            with patch.object(sys_mod.settings, "anthropic_api_key", ""):
                r = client.get("/api/v1/admin/system/capabilities", headers=auth_admin)
        assert "openai" in r.json()["data"]["supported_providers"]


# ===========================================================================
# 캐시 동작 검증 (Tier 공통)
# ===========================================================================

class TestCapabilitiesCache:
    """capabilities 캐시가 tier 간 공유되고 TTL이 정상 동작하는지 확인한다."""

    def test_module_cache_populated_after_first_call(self, client, auth_viewer):
        """첫 호출 후 모듈 캐시가 채워진다."""
        import app.api.v1.system as sys_mod
        _reset_cap_cache()
        assert sys_mod._cap_cache["data"] is None
        client.get("/api/v1/system/capabilities", headers=auth_viewer)
        assert sys_mod._cap_cache["data"] is not None

    def test_cache_not_recomputed_within_ttl(self, client, auth_viewer):
        """TTL 내 두 번째 호출은 캐시를 재사용한다."""
        import app.api.v1.system as sys_mod
        _reset_cap_cache()

        call_count = {"n": 0}
        original_build = sys_mod._build_capabilities

        def counting_build():
            call_count["n"] += 1
            return original_build()

        with patch.object(sys_mod, "_build_capabilities", counting_build):
            client.get("/api/v1/system/capabilities", headers=auth_viewer)
            client.get("/api/v1/system/capabilities", headers=auth_viewer)

        assert call_count["n"] == 1, "_build_capabilities 가 두 번 이상 호출됨 (캐시 미동작)"


# ===========================================================================
# 정보 격리 교차 검증
# ===========================================================================

class TestInformationIsolation:
    """각 tier의 응답이 다른 tier의 필드를 노출하지 않는지 교차 검증한다."""

    def test_tier2_never_exposes_pgvector_even_with_admin_role(self, client, auth_admin):
        """Admin이 Tier 2를 호출해도 pgvector_enabled는 노출되지 않는다.

        정보 격리는 역할이 아닌 엔드포인트 tier에 의해 결정된다.
        """
        r = client.get("/api/v1/system/capabilities", headers=auth_admin)
        assert r.status_code == 200
        data = r.json()["data"]
        assert "pgvector_enabled" not in data
        assert "supported_providers" not in data
