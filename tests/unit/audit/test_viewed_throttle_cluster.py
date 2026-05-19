"""S3 Phase 7 FG 7-2 — viewed_throttle cluster-wide dedup 단위 테스트.

대상: backend/app/audit/viewed_throttle.py
- Valkey ``SET NX EX`` 경로 (정상)
- Valkey 장애 / disabled 시 LRU fallback (R-I2 fail-open)
- namespace 격리 (R-I4)
- 다중 워커 시뮬레이션 — 공유 Valkey mock 으로 정확히 1건만 emit
"""
from __future__ import annotations

import pytest

from app.audit import viewed_throttle


@pytest.fixture(autouse=True)
def _reset_state():
    viewed_throttle.reset_for_tests()
    yield
    viewed_throttle.reset_for_tests()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("AUDIT_VIEWED_DEDUP_WINDOW_SEC", "AUDIT_VIEWED_DEDUP_MAX_ENTRIES"):
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


class _FakeValkey:
    """SET NX EX 의 원자성을 단일 dict 로 시뮬레이트.

    다중 워커 시뮬레이션 — 같은 인스턴스를 여러 'worker' 가 공유.
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        self.calls: list[tuple[str, str, int]] = []

    def set(self, key, value, *, nx=False, ex=None):
        self.calls.append((key, value, ex))
        if nx and key in self.store:
            return None  # SETNX 실패
        self.store[key] = value
        return True


class TestValkeySetnxPath:
    def test_first_call_setnx_succeeds(self, monkeypatch):
        fake = _FakeValkey()
        monkeypatch.setattr(viewed_throttle, "_try_valkey_setnx", _make_setnx_via(fake))

        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True

    def test_duplicate_within_window_skipped(self, monkeypatch):
        fake = _FakeValkey()
        monkeypatch.setattr(viewed_throttle, "_try_valkey_setnx", _make_setnx_via(fake))

        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is False

    def test_different_actor_separate_keys(self, monkeypatch):
        fake = _FakeValkey()
        monkeypatch.setattr(viewed_throttle, "_try_valkey_setnx", _make_setnx_via(fake))

        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True
        assert viewed_throttle.should_emit_view("user-B", "doc-1") is True
        # 같은 doc 이지만 다른 actor 의 키
        assert len(fake.store) == 2


class TestFailOpenFallback:
    def test_valkey_disabled_falls_back_to_lru(self, monkeypatch):
        # _try_valkey_setnx 가 None 반환 → LRU 로직
        monkeypatch.setattr(viewed_throttle, "_try_valkey_setnx", lambda *args, **kw: None)

        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is False  # LRU dedup

    def test_valkey_exception_returns_none_via_helper(self, monkeypatch):
        """실제 helper 가 Exception 을 None 으로 변환하는지 확인."""
        from app.cache import valkey as valkey_mod

        class _BrokenRedis:
            def set(self, *args, **kwargs):
                raise RuntimeError("connection refused")

        monkeypatch.setattr(valkey_mod, "get_valkey_or_none", lambda: _BrokenRedis())

        # helper 가 None 반환 → LRU 경로
        result = viewed_throttle._try_valkey_setnx("user-A", "doc-1", 300)
        assert result is None

    def test_valkey_disabled_via_helper(self, monkeypatch):
        from app.cache import valkey as valkey_mod
        monkeypatch.setattr(valkey_mod, "get_valkey_or_none", lambda: None)

        result = viewed_throttle._try_valkey_setnx("user-A", "doc-1", 300)
        assert result is None


class TestNamespaceIsolation:
    def test_key_contains_environment_prefix(self, monkeypatch):
        fake = _FakeValkey()
        monkeypatch.setattr(viewed_throttle, "_try_valkey_setnx", _make_setnx_via(fake))

        viewed_throttle.should_emit_view("user-A", "doc-1")

        # 키에 env prefix 포함 (R-I4)
        assert any("mimir:test:viewed:" in k for k in fake.store.keys())
        assert any("user-A" in k for k in fake.store.keys())
        assert any("doc-1" in k for k in fake.store.keys())


class TestMultiWorkerSimulation:
    def test_shared_valkey_dedups_across_workers(self, monkeypatch):
        """4 워커가 동일 (actor, doc) view 를 동시 호출해도 정확히 1건만 True."""
        fake = _FakeValkey()

        # 모든 워커가 같은 fake instance 공유 → cluster-wide 시뮬레이션
        def _shared_setnx(actor, doc, window):
            from app.cache import make_key
            key = make_key("viewed", actor, doc)
            result = fake.set(key, "ts", nx=True, ex=window)
            if result is True:
                return True
            return False

        monkeypatch.setattr(viewed_throttle, "_try_valkey_setnx", _shared_setnx)

        results = [
            viewed_throttle.should_emit_view("user-A", "doc-shared")
            for _ in range(4)
        ]
        assert results.count(True) == 1
        assert results.count(False) == 3

    def test_independent_lru_workers_would_double_emit_without_valkey(self, monkeypatch):
        """대비 케이스 — Valkey 없이는 워커별 LRU 분리 시 중복 emit (회귀 기록용)."""
        # _try_valkey_setnx → None 강제 (Valkey off)
        monkeypatch.setattr(viewed_throttle, "_try_valkey_setnx", lambda *a, **kw: None)

        # 워커 1 reset → 1번 emit
        viewed_throttle.reset_for_tests()
        first = viewed_throttle.should_emit_view("user-A", "doc-1")

        # 워커 2 reset → 또 1번 emit (워커별 LRU 분리 시뮬레이션)
        viewed_throttle.reset_for_tests()
        second = viewed_throttle.should_emit_view("user-A", "doc-1")

        assert first is True
        assert second is True  # ← Valkey 없으면 워커별 중복 emit. FG 7-2 가 해결하는 문제.


def _make_setnx_via(fake_valkey):
    """fake Valkey 인스턴스로 _try_valkey_setnx 동작 흉내내는 helper."""
    def _impl(actor, doc, window):
        from app.cache import make_key
        try:
            key = make_key("viewed", actor, doc)
        except ValueError:
            return None
        try:
            result = fake_valkey.set(key, "ts", nx=True, ex=window)
        except Exception:
            return None
        if result is True:
            return True
        return False
    return _impl
