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


# --------------------------------------------------------------------------- #
# S3 Phase 7 FG 7-3 — cluster-wide invalidation
# --------------------------------------------------------------------------- #


class TestClusterWideInvalidation:
    def test_broadcast_true_calls_publish(self, monkeypatch):
        """invalidate_cache(broadcast=True) 가 pubsub.publish_invalidate 호출."""
        from app.cache import pubsub

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            pubsub,
            "publish_invalidate",
            lambda feature, key, **kw: calls.append((feature, key)) or True,
        )

        policy.invalidate_cache("sp-1", broadcast=True)
        assert calls == [("scope_policy", "sp-1")]

    def test_broadcast_false_skips_publish(self, monkeypatch):
        """loop 방지 — broadcast=False 면 publish 호출 안 함."""
        from app.cache import pubsub

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            pubsub,
            "publish_invalidate",
            lambda feature, key, **kw: calls.append((feature, key)) or True,
        )

        policy.invalidate_cache("sp-1", broadcast=False)
        assert calls == []

    def test_default_broadcast_is_false(self, monkeypatch):
        """기본값은 broadcast=False — 명시적 호출자만 broadcast."""
        from app.cache import pubsub

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            pubsub,
            "publish_invalidate",
            lambda feature, key, **kw: calls.append((feature, key)) or True,
        )

        policy.invalidate_cache("sp-1")
        assert calls == []

    def test_wildcard_when_no_profile_id(self, monkeypatch):
        from app.cache import pubsub

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            pubsub,
            "publish_invalidate",
            lambda feature, key, **kw: calls.append((feature, key)) or True,
        )

        policy.invalidate_cache(None, broadcast=True)
        assert calls == [("scope_policy", "*")]

    def test_remote_invalidate_clears_specific_key(self, monkeypatch):
        """다른 워커가 발행한 메시지 수신 시 local cache 비움."""
        from app.cache import pubsub

        # publish 호출 없음 — loop 방지 확인
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            pubsub,
            "publish_invalidate",
            lambda feature, key, **kw: calls.append((feature, key)) or True,
        )

        # local cache 에 값 세팅
        fake_profile = SimpleNamespace(
            settings=SimpleNamespace(expose_viewers=True),
        )

        class _FakeRepo:
            def __init__(self, conn):
                pass
            def get_by_id(self, pid):
                return fake_profile

        monkeypatch.setattr(policy, "ScopeProfileRepository", _FakeRepo)

        actor = _actor(scope_profile_id="sp-remote")
        policy.should_expose_viewers(actor)  # populate
        assert "sp-remote" in policy._cache

        # remote 메시지 처리
        policy._handle_remote_invalidate("sp-remote")
        assert "sp-remote" not in policy._cache
        # broadcast 발행 없음 (loop 방지)
        assert calls == []

    def test_remote_invalidate_wildcard_clears_all(self, monkeypatch):
        from app.cache import pubsub
        monkeypatch.setattr(pubsub, "publish_invalidate", lambda *a, **kw: True)

        policy._cache["sp-A"] = (True, 0.0)
        policy._cache["sp-B"] = (False, 0.0)

        policy._handle_remote_invalidate("*")

        assert len(policy._cache) == 0

    def test_broadcast_swallows_pubsub_import_failure(self, monkeypatch):
        """pubsub 모듈 import 실패해도 local invalidate 는 통과."""
        # local cache 에 값 미리 세팅
        policy._cache["sp-X"] = (True, 0.0)

        # sys.modules 에서 강제 제거 + import 막기
        import sys
        original = sys.modules.pop("app.cache.pubsub", None)

        class _ImportBlocker:
            def find_module(self, name, path=None):
                if name == "app.cache.pubsub":
                    return self
                return None
            def load_module(self, name):
                raise ImportError("blocked for test")

        sys.meta_path.insert(0, _ImportBlocker())
        try:
            # 예외 전파 안 함
            policy.invalidate_cache("sp-X", broadcast=True)
            # local cache 는 비워짐
            assert "sp-X" not in policy._cache
        finally:
            sys.meta_path.pop(0)
            if original is not None:
                sys.modules["app.cache.pubsub"] = original
