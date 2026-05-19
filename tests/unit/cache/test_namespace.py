"""S3 Phase 7 FG 7-1 — Valkey namespace 단위 테스트."""
from __future__ import annotations

import pytest

from app.cache import namespace


@pytest.fixture(autouse=True)
def _clean_namespace_env(monkeypatch):
    monkeypatch.delenv("VALKEY_NAMESPACE", raising=False)
    # settings 캐시 무효화 — pydantic-settings 는 import 시점에 1회 평가하므로
    # 동적 변경은 settings 객체 attribute 를 직접 patch 한다.
    from app.config import settings
    monkeypatch.setattr(settings, "valkey_namespace", "")
    monkeypatch.setattr(settings, "environment", "test")
    yield


class TestNamespacePrefix:
    def test_default_prefix_uses_environment(self):
        assert namespace.namespace_prefix() == "mimir:test"

    def test_override_via_settings(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_namespace", "mimir:prod")
        assert namespace.namespace_prefix() == "mimir:prod"

    def test_override_strips_trailing_colon(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_namespace", "mimir:staging:")
        assert namespace.namespace_prefix() == "mimir:staging"

    def test_environment_unknown_uses_unknown(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "environment", "")
        assert namespace.namespace_prefix() == "mimir:unknown"


class TestMakeKey:
    def test_basic_key(self):
        assert namespace.make_key("viewed", "user-A", "doc-1") == "mimir:test:viewed:user-A:doc-1"

    def test_no_parts(self):
        assert namespace.make_key("scope_policy") == "mimir:test:scope_policy"

    def test_int_part_serialized(self):
        assert namespace.make_key("rate", 42) == "mimir:test:rate:42"

    def test_whitespace_in_part_is_sanitized(self):
        # 키 조작 방어 — whitespace → "_"
        key = namespace.make_key("viewed", "user A", "doc\n1")
        assert " " not in key
        assert "\n" not in key

    def test_empty_part_replaced(self):
        # 빈 segment 가 들어가도 키 구조 보존
        key = namespace.make_key("viewed", "", "doc-1")
        assert key == "mimir:test:viewed:_:doc-1"

    def test_invalid_feature_raises(self):
        with pytest.raises(ValueError):
            namespace.make_key("")
        with pytest.raises(ValueError):
            namespace.make_key("a:b")
        with pytest.raises(ValueError):
            namespace.make_key("a b")

    def test_namespace_isolation_dev_vs_prod(self, monkeypatch):
        """다른 env 의 키가 충돌하지 않는다 (R-I4)."""
        from app.config import settings

        monkeypatch.setattr(settings, "environment", "dev")
        dev_key = namespace.make_key("viewed", "u", "d")

        monkeypatch.setattr(settings, "environment", "prod")
        prod_key = namespace.make_key("viewed", "u", "d")

        assert dev_key != prod_key
        assert dev_key.startswith("mimir:dev:")
        assert prod_key.startswith("mimir:prod:")


class TestMakeChannel:
    def test_channel_without_org(self):
        assert namespace.make_channel("scope_policy") == "mimir:test:cache:invalidate:scope_policy"

    def test_channel_with_org_has_tenant_prefix(self):
        # R-I3 — tenant 격리 prefix 강제
        ch = namespace.make_channel("scope_policy", org_id="org-123")
        assert ch == "mimir:test:tenant:org-123:cache:invalidate:scope_policy"

    def test_channel_invalid_feature_raises(self):
        with pytest.raises(ValueError):
            namespace.make_channel("")
        with pytest.raises(ValueError):
            namespace.make_channel("a:b")

    def test_channel_sanitizes_org_id(self):
        # tenant prefix 의 org_id 도 sanitize 됨
        ch = namespace.make_channel("scope_policy", org_id="org\n123")
        assert "\n" not in ch

    def test_tenant_isolation(self):
        """다른 org 의 채널이 충돌하지 않는다 (R-I3)."""
        a = namespace.make_channel("scope_policy", org_id="org-A")
        b = namespace.make_channel("scope_policy", org_id="org-B")
        assert a != b
        assert "org-A" in a
        assert "org-B" in b
