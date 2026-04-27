"""S3 Phase 3 FG 3-2 — scope_profile_policy 단위 테스트.

대상: backend/app/services/scope_profile_policy.py

커버:
  - should_expose_viewers (None / unauthenticated / no scope_profile_id / 정상)
  - 캐시 hit / miss
  - DB / repository 실패 → fail-closed False
  - invalidate_cache (전체 / 특정 profile)
  - settings.expose_viewers true / false
  - 캐시 TTL 0 → 무캐시
  - 잘못된 env (TTL) → default fallback
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import scope_profile_policy as policy


@pytest.fixture(autouse=True)
def _reset_cache():
    policy.invalidate_cache()
    yield
    policy.invalidate_cache()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SCOPE_PROFILE_POLICY_CACHE_TTL_SEC", raising=False)


@pytest.fixture(autouse=True)
def _mock_get_db(monkeypatch):
    """get_db() context manager 를 mock — 실제 DB 연결 회피."""
    from contextlib import contextmanager

    @contextmanager
    def _fake_db():
        yield MagicMock(name="conn")

    monkeypatch.setattr(policy, "get_db", _fake_db)


def _actor(*, authenticated=True, scope_profile_id="sp-1"):
    return SimpleNamespace(
        is_authenticated=authenticated,
        scope_profile_id=scope_profile_id,
    )


class TestShouldExposeViewers:
    def test_none_actor_returns_false(self):
        assert policy.should_expose_viewers(None) is False

    def test_unauthenticated_actor_returns_false(self):
        actor = _actor(authenticated=False)
        assert policy.should_expose_viewers(actor) is False

    def test_no_scope_profile_id_returns_false(self):
        actor = _actor(scope_profile_id=None)
        assert policy.should_expose_viewers(actor) is False

    def test_empty_scope_profile_id_returns_false(self):
        actor = _actor(scope_profile_id="")
        assert policy.should_expose_viewers(actor) is False

    def test_settings_expose_viewers_true_returns_true(self, monkeypatch):
        # repository.get_by_id 가 settings.expose_viewers=True 인 profile 반환
        fake_profile = SimpleNamespace(
            settings=SimpleNamespace(expose_viewers=True),
        )

        class _FakeRepo:
            def __init__(self, conn):
                pass
            def get_by_id(self, pid):
                return fake_profile

        monkeypatch.setattr(policy, "ScopeProfileRepository", _FakeRepo)

        actor = _actor(scope_profile_id="sp-A")
        assert policy.should_expose_viewers(actor) is True

    def test_settings_expose_viewers_false_returns_false(self, monkeypatch):
        fake_profile = SimpleNamespace(
            settings=SimpleNamespace(expose_viewers=False),
        )

        class _FakeRepo:
            def __init__(self, conn):
                pass
            def get_by_id(self, pid):
                return fake_profile

        monkeypatch.setattr(policy, "ScopeProfileRepository", _FakeRepo)

        actor = _actor(scope_profile_id="sp-B")
        assert policy.should_expose_viewers(actor) is False

    def test_profile_not_found_fails_closed(self, monkeypatch):
        class _FakeRepo:
            def __init__(self, conn):
                pass
            def get_by_id(self, pid):
                return None  # not found

        monkeypatch.setattr(policy, "ScopeProfileRepository", _FakeRepo)

        actor = _actor(scope_profile_id="sp-X")
        assert policy.should_expose_viewers(actor) is False

    def test_db_exception_fails_closed(self, monkeypatch):
        class _FakeRepo:
            def __init__(self, conn):
                raise RuntimeError("db down")

        monkeypatch.setattr(policy, "ScopeProfileRepository", _FakeRepo)

        actor = _actor(scope_profile_id="sp-X")
        assert policy.should_expose_viewers(actor) is False


class TestCache:
    def test_cache_hit_avoids_db(self, monkeypatch):
        fake_profile = SimpleNamespace(
            settings=SimpleNamespace(expose_viewers=True),
        )
        call_count = {"n": 0}

        class _FakeRepo:
            def __init__(self, conn):
                pass
            def get_by_id(self, pid):
                call_count["n"] += 1
                return fake_profile

        monkeypatch.setattr(policy, "ScopeProfileRepository", _FakeRepo)

        actor = _actor(scope_profile_id="sp-cache")
        assert policy.should_expose_viewers(actor) is True
        assert policy.should_expose_viewers(actor) is True
        # 두번째 호출은 캐시 hit
        assert call_count["n"] == 1

    def test_invalidate_cache_specific(self, monkeypatch):
        fake_profile = SimpleNamespace(
            settings=SimpleNamespace(expose_viewers=True),
        )
        call_count = {"n": 0}

        class _FakeRepo:
            def __init__(self, conn):
                pass
            def get_by_id(self, pid):
                call_count["n"] += 1
                return fake_profile

        monkeypatch.setattr(policy, "ScopeProfileRepository", _FakeRepo)

        actor = _actor(scope_profile_id="sp-1")
        policy.should_expose_viewers(actor)
        policy.invalidate_cache("sp-1")
        policy.should_expose_viewers(actor)
        assert call_count["n"] == 2

    def test_invalidate_cache_all(self, monkeypatch):
        fake_profile = SimpleNamespace(
            settings=SimpleNamespace(expose_viewers=True),
        )
        call_count = {"n": 0}

        class _FakeRepo:
            def __init__(self, conn):
                pass
            def get_by_id(self, pid):
                call_count["n"] += 1
                return fake_profile

        monkeypatch.setattr(policy, "ScopeProfileRepository", _FakeRepo)

        for sp in ("sp-A", "sp-B"):
            policy.should_expose_viewers(_actor(scope_profile_id=sp))
        policy.invalidate_cache()  # 전체
        for sp in ("sp-A", "sp-B"):
            policy.should_expose_viewers(_actor(scope_profile_id=sp))
        # 첫 라운드 2 + 두번째 라운드 2 = 4
        assert call_count["n"] == 4

    def test_ttl_zero_disables_cache(self, monkeypatch):
        monkeypatch.setenv("SCOPE_PROFILE_POLICY_CACHE_TTL_SEC", "0")
        fake_profile = SimpleNamespace(
            settings=SimpleNamespace(expose_viewers=True),
        )
        call_count = {"n": 0}

        class _FakeRepo:
            def __init__(self, conn):
                pass
            def get_by_id(self, pid):
                call_count["n"] += 1
                return fake_profile

        monkeypatch.setattr(policy, "ScopeProfileRepository", _FakeRepo)

        actor = _actor(scope_profile_id="sp-no-cache")
        for _ in range(3):
            policy.should_expose_viewers(actor)
        # ttl 0 → 매 호출 DB 조회
        assert call_count["n"] == 3

    def test_invalid_ttl_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SCOPE_PROFILE_POLICY_CACHE_TTL_SEC", "garbage")
        # 단순히 default 30 이 사용되는지만 확인 (직접 측정은 _cache_ttl 호출)
        assert policy._cache_ttl() == 30

    def test_default_ttl_constant(self):
        assert policy.DEFAULT_CACHE_TTL_SEC == 30
