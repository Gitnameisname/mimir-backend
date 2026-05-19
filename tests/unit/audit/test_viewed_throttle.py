"""S3 Phase 3 FG 3-1 — document.viewed throttle / dedup 단위 테스트."""
from __future__ import annotations

import os
import time as _time
from unittest import mock

import pytest

from app.audit import viewed_throttle


@pytest.fixture(autouse=True)
def _reset_throttle_state():
    viewed_throttle.reset_for_tests()
    yield
    viewed_throttle.reset_for_tests()


@pytest.fixture(autouse=True)
def _force_lru_fallback(monkeypatch):
    """S3 Phase 7 FG 7-2 — 본 파일의 기존 case 는 LRU 경로를 검증한다.

    Valkey 가용한 환경에서도 LRU fallback 만 동작하도록 강제 (monkeypatch).
    Cluster-wide Valkey 경로는 ``test_viewed_throttle_cluster.py`` 가 별도 검증.
    """
    from app.audit import viewed_throttle as vt
    monkeypatch.setattr(vt, "_try_valkey_setnx", lambda actor, doc, window: None)
    yield


@pytest.fixture
def _clean_env(monkeypatch):
    for key in (
        "AUDIT_VIEWED_DEDUP_WINDOW_SEC",
        "AUDIT_VIEWED_DEDUP_MAX_ENTRIES",
    ):
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


class TestShouldEmitView:
    def test_first_call_returns_true(self, _clean_env):
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True

    def test_duplicate_within_window_returns_false(self, _clean_env):
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is False

    def test_different_actor_not_dedup(self, _clean_env):
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True
        assert viewed_throttle.should_emit_view("user-B", "doc-1") is True

    def test_different_document_not_dedup(self, _clean_env):
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True
        assert viewed_throttle.should_emit_view("user-A", "doc-2") is True

    def test_anonymous_actor_returns_false(self, _clean_env):
        assert viewed_throttle.should_emit_view(None, "doc-1") is False
        assert viewed_throttle.should_emit_view("", "doc-1") is False

    def test_empty_document_id_returns_false(self, _clean_env):
        assert viewed_throttle.should_emit_view("user-A", "") is False

    def test_window_expiry_allows_re_emit(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AUDIT_VIEWED_DEDUP_WINDOW_SEC", "10")

        # 가짜 시계 — 첫 호출 t=1000, 두 번째 t=1009 (윈도우 안), 세 번째 t=1015 (윈도우 밖)
        timestamps = iter([1000.0, 1009.0, 1015.0])

        class _FakeDt:
            def timestamp(self):
                return next(timestamps)

        with mock.patch.object(viewed_throttle, "utcnow", return_value=_FakeDt()):
            assert viewed_throttle.should_emit_view("user-A", "doc-1") is True
            assert viewed_throttle.should_emit_view("user-A", "doc-1") is False
            assert viewed_throttle.should_emit_view("user-A", "doc-1") is True

    def test_window_zero_disables_dedup(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AUDIT_VIEWED_DEDUP_WINDOW_SEC", "0")

        for _ in range(5):
            assert viewed_throttle.should_emit_view("user-A", "doc-1") is True

    def test_invalid_window_falls_back_to_default(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AUDIT_VIEWED_DEDUP_WINDOW_SEC", "not-a-number")

        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is False

    def test_negative_window_treated_as_zero(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AUDIT_VIEWED_DEDUP_WINDOW_SEC", "-50")

        # max(0, -50) → 0 → dedup off
        for _ in range(3):
            assert viewed_throttle.should_emit_view("user-A", "doc-1") is True

    def test_lru_eviction_drops_oldest(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AUDIT_VIEWED_DEDUP_MAX_ENTRIES", "3")
        monkeypatch.setenv("AUDIT_VIEWED_DEDUP_WINDOW_SEC", "300")

        # 캐시 채우기: [doc-1, doc-2, doc-3]
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True
        assert viewed_throttle.should_emit_view("user-A", "doc-2") is True
        assert viewed_throttle.should_emit_view("user-A", "doc-3") is True

        # 4번째 entry → 가장 오래된 doc-1 eviction → 캐시: [doc-2, doc-3, doc-4]
        assert viewed_throttle.should_emit_view("user-A", "doc-4") is True

        # doc-2 / doc-3 / doc-4 는 여전히 캐시 안 → dedup
        assert viewed_throttle.should_emit_view("user-A", "doc-2") is False
        assert viewed_throttle.should_emit_view("user-A", "doc-3") is False
        assert viewed_throttle.should_emit_view("user-A", "doc-4") is False

        # doc-1 은 evict 되어 emit 허용 (윈도우 안이지만 캐시에 없음)
        # 이 호출이 성공하면 doc-2 가 새로 evict 됨 → 캐시: [doc-3, doc-4, doc-1]
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True

        # doc-3 는 여전히 캐시 안 → dedup
        assert viewed_throttle.should_emit_view("user-A", "doc-3") is False

    def test_invalid_max_entries_falls_back(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AUDIT_VIEWED_DEDUP_MAX_ENTRIES", "garbage")

        # 정상 동작 (default 5000)
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is False

    def test_concurrent_access_thread_safe(self, _clean_env, monkeypatch):
        """간이 멀티스레드 — 같은 (actor, doc) 에 대해 정확히 1건만 emit 허용."""
        import threading

        monkeypatch.setenv("AUDIT_VIEWED_DEDUP_WINDOW_SEC", "300")

        results: list[bool] = []
        lock = threading.Lock()

        def _call():
            r = viewed_throttle.should_emit_view("user-A", "doc-shared")
            with lock:
                results.append(r)

        threads = [threading.Thread(target=_call) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 정확히 한 번만 True
        assert results.count(True) == 1
        assert results.count(False) == 19


class TestResetForTests:
    def test_reset_clears_cache(self, _clean_env):
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True
        assert viewed_throttle.should_emit_view("user-A", "doc-1") is False

        viewed_throttle.reset_for_tests()

        assert viewed_throttle.should_emit_view("user-A", "doc-1") is True


class TestDefaults:
    def test_default_window_is_300(self):
        assert viewed_throttle.DEFAULT_DEDUP_WINDOW_SEC == 300

    def test_default_max_entries_is_5000(self):
        assert viewed_throttle.DEFAULT_MAX_ENTRIES == 5000
