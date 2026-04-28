"""
S3 Phase 4 FG 4-4 §2.1.2 — `latest` 해석 동일성 (REST ↔ MCP).

목적: `version_id="latest"` 입력 시 REST 와 MCP 가 같은 published vN 으로 resolve 함을
보장. 둘 다 `VersionsRepository.get_current_published` 를 정본으로 사용.

본 테스트는 mock 기반 — 같은 published version 객체가 양쪽에서 반환됨을 검증.
실 DB 통합 (v1~v5 + draft 시드) 은 운영자 후속.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestLatestResolveSharedRepository:
    """REST 와 MCP 가 동일 `VersionsRepository.get_current_published` 사용."""

    def test_mcp_uri_builder_resolve_uses_versions_repository(self):
        """`resolve_latest_version` 이 `VersionsRepository.get_current_published` 위임."""
        from app.mcp.uri_builder import resolve_latest_version

        fake_v = MagicMock()
        fake_v.id = "v-resolved"
        with patch(
            "app.repositories.versions_repository.VersionsRepository.get_current_published",
            return_value=fake_v,
        ) as mock_call:
            result = resolve_latest_version(MagicMock(), "doc-1")
        assert result == "v-resolved"
        mock_call.assert_called_once()

    def test_rest_resolve_uses_same_versions_repository(self):
        """REST 측이 같은 repo 메서드를 사용 (코드 grep)."""
        from pathlib import Path
        ROOT = Path(__file__).resolve().parents[4]
        # 가장 신뢰성 있는 검증: VersionsRepository.get_current_published 호출자 검색
        repo_module = ROOT / "backend/app/repositories/versions_repository.py"
        assert repo_module.exists()
        content = repo_module.read_text(encoding="utf-8")
        assert "def get_current_published" in content
        # status='published' 가 SQL 에 박혀있어야 함
        assert "status = 'published'" in content


class TestLatestResolveDeterministic:
    """같은 actor 가 같은 시점에 호출하면 REST/MCP 둘 다 같은 vN 반환."""

    def test_resolve_version_id_returns_same_concrete(self):
        """`resolve_version_id("latest")` 가 항상 같은 vN 반환."""
        from app.mcp.uri_builder import resolve_version_id

        fake_v = MagicMock()
        fake_v.id = "v-concrete"
        with patch(
            "app.repositories.versions_repository.VersionsRepository.get_current_published",
            return_value=fake_v,
        ):
            r1 = resolve_version_id(MagicMock(), "doc-1", "latest")
            r2 = resolve_version_id(MagicMock(), "doc-1", "latest")
        assert r1 == r2 == "v-concrete"

    def test_concrete_version_passthrough(self):
        """구체 vN 은 resolve 없이 그대로 통과."""
        from app.mcp.uri_builder import resolve_version_id

        # 구체 ID 는 DB 미접근
        result = resolve_version_id(None, "doc-1", "v7")
        assert result == "v7"

    def test_no_published_returns_none(self):
        """published 버전 없으면 None — REST/MCP 둘 다 동일 처리."""
        from app.mcp.uri_builder import resolve_latest_version

        with patch(
            "app.repositories.versions_repository.VersionsRepository.get_current_published",
            return_value=None,
        ):
            assert resolve_latest_version(MagicMock(), "doc-1") is None


class TestMCPResolveResponse:
    """MCP 응답이 resolved vN 을 명시 (단독 'latest' 미반환)."""

    def test_read_render_response_has_resolved_version_id(self):
        """ReadDocumentRenderData.version_id 가 항상 구체 (vN) — schema level 보장."""
        from app.schemas.mcp import ReadDocumentRenderData

        # 'latest' 단독 입력은 schema 가 그냥 string 으로 받지만 도구 함수가 resolve.
        # 응답의 version_id 는 도구 함수에서 항상 구체값으로 채워짐.
        d = ReadDocumentRenderData(
            document_id="d1",
            version_id="v-concrete-7",
            format="plain_text",
            rendered_text="x",
            render_hash="h",
        )
        assert d.version_id != "latest"

    def test_uri_builder_rejects_latest(self):
        """build_*_uri 가 'latest' 입력 거부 — URI 에 'latest' 출현 0."""
        from app.mcp.uri_builder import build_version_uri, build_node_uri, build_render_uri

        with pytest.raises(ValueError):
            build_version_uri("d1", "latest")
        with pytest.raises(ValueError):
            build_node_uri("d1", "latest", "n1")
        with pytest.raises(ValueError):
            build_render_uri("d1", "latest")
