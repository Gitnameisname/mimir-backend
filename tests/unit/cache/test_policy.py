"""S3 Phase 7 FG 7-1 — Valkey fail-open / fail-closed 분류 단위 테스트."""
from __future__ import annotations

import pytest

from app.cache import policy
from app.cache.policy import FailPolicy


@pytest.fixture(autouse=True)
def _clean_policy_env(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "valkey_fail_open_features", "")
    monkeypatch.setattr(settings, "valkey_fail_closed_features", "")
    yield


class TestDefaultPolicy:
    def test_viewed_throttle_is_fail_open(self):
        assert policy.policy_for("viewed_throttle") is FailPolicy.FAIL_OPEN
        assert policy.is_fail_open("viewed_throttle") is True
        assert policy.is_fail_closed("viewed_throttle") is False

    def test_rate_limit_is_fail_open(self):
        assert policy.is_fail_open("rate_limit") is True

    def test_scope_policy_is_fail_closed(self):
        assert policy.policy_for("scope_policy") is FailPolicy.FAIL_CLOSED
        assert policy.is_fail_closed("scope_policy") is True
        assert policy.is_fail_open("scope_policy") is False

    def test_response_cache_is_fail_open(self):
        assert policy.is_fail_open("response_cache") is True

    def test_unknown_feature_is_fail_closed(self):
        """미등록 feature → 보수 default = fail-closed."""
        assert policy.policy_for("never_heard_of_it") is FailPolicy.FAIL_CLOSED


class TestEnvOverride:
    def test_fail_open_override_promotes_fail_closed_feature(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_fail_open_features", "scope_policy")
        # override 가 default 를 덮어씀
        assert policy.is_fail_open("scope_policy") is True

    def test_fail_closed_override_demotes_fail_open_feature(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_fail_closed_features", "viewed_throttle")
        assert policy.is_fail_closed("viewed_throttle") is True

    def test_both_overrides_fail_closed_wins(self, monkeypatch):
        """둘 다 지정된 feature 는 보안 보수 — fail-closed 우선."""
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_fail_open_features", "scope_policy")
        monkeypatch.setattr(settings, "valkey_fail_closed_features", "scope_policy")
        assert policy.is_fail_closed("scope_policy") is True

    def test_csv_parsing_handles_whitespace(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(
            settings,
            "valkey_fail_closed_features",
            " viewed_throttle , rate_limit ,response_cache",
        )
        assert policy.is_fail_closed("viewed_throttle") is True
        assert policy.is_fail_closed("rate_limit") is True
        assert policy.is_fail_closed("response_cache") is True

    def test_empty_override_falls_back_to_default(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_fail_open_features", "")
        monkeypatch.setattr(settings, "valkey_fail_closed_features", "")
        # default 그대로
        assert policy.is_fail_open("viewed_throttle") is True
        assert policy.is_fail_closed("scope_policy") is True
