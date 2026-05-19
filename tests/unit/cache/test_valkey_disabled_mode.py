"""S3 Phase 7 FG 7-1 — Valkey disabled / 폐쇄망 fallback 단위 테스트."""
from __future__ import annotations

import pytest

from app.cache import valkey as valkey_mod


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch):
    """싱글톤 클라이언트 격리 — 테스트마다 새로 build."""
    monkeypatch.setattr(valkey_mod, "_client", None)
    yield


@pytest.fixture
def _settings_clean(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "valkey_disabled", "")
    monkeypatch.setattr(settings, "valkey_host", "localhost")
    monkeypatch.setattr(settings, "valkey_port", 6379)
    yield monkeypatch


class TestIsValkeyDisabled:
    def test_default_settings_enabled(self, _settings_clean):
        assert valkey_mod.is_valkey_disabled() is False

    def test_explicit_disabled_flag_1(self, _settings_clean, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_disabled", "1")
        assert valkey_mod.is_valkey_disabled() is True

    def test_explicit_disabled_flag_true(self, _settings_clean, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_disabled", "true")
        assert valkey_mod.is_valkey_disabled() is True

    def test_explicit_disabled_flag_uppercase(self, _settings_clean, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_disabled", "YES")
        assert valkey_mod.is_valkey_disabled() is True

    def test_empty_host_disables(self, _settings_clean, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_host", "")
        assert valkey_mod.is_valkey_disabled() is True

    def test_unrecognized_flag_value_enabled(self, _settings_clean, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_disabled", "maybe")
        # disabled 가 명시적이지 않으면 enabled
        assert valkey_mod.is_valkey_disabled() is False


class TestGetValkeyOrNone:
    def test_disabled_mode_returns_none(self, _settings_clean, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_disabled", "1")
        assert valkey_mod.get_valkey_or_none() is None

    def test_empty_host_returns_none(self, _settings_clean, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_host", "")
        assert valkey_mod.get_valkey_or_none() is None

    def test_enabled_returns_redis_instance(self, _settings_clean):
        import redis as redis_mod

        client = valkey_mod.get_valkey_or_none()
        assert client is not None
        assert isinstance(client, redis_mod.Redis)


class TestPingValkey:
    def test_disabled_mode_returns_false(self, _settings_clean, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_disabled", "1")
        assert valkey_mod.ping_valkey() is False
