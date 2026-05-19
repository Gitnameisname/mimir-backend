"""S3 Phase 7 FG 7-3 — Valkey pub/sub primitives 단위 테스트."""
from __future__ import annotations

import json
import threading

import pytest

from app.cache import pubsub


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "valkey_disabled", "")
    monkeypatch.setattr(settings, "valkey_host", "localhost")
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(settings, "valkey_namespace", "")
    yield monkeypatch


class _FakeClient:
    def __init__(self, *, raise_on_publish=False):
        self.published: list[tuple[str, str]] = []
        self.raise_on_publish = raise_on_publish

    def publish(self, channel, message):
        if self.raise_on_publish:
            raise RuntimeError("connection refused")
        self.published.append((channel, message))
        return 1


class TestPublishInvalidate:
    def test_disabled_returns_false(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_disabled", "1")

        assert pubsub.publish_invalidate("scope_policy", "sp-1") is False

    def test_invalid_feature_returns_false(self, monkeypatch):
        client = _FakeClient()
        monkeypatch.setattr(pubsub, "get_valkey_or_none", lambda: client)

        assert pubsub.publish_invalidate("a:b", "sp-1") is False
        assert pubsub.publish_invalidate("", "sp-1") is False
        assert len(client.published) == 0

    def test_successful_publish_includes_worker_id(self, monkeypatch):
        client = _FakeClient()
        monkeypatch.setattr(pubsub, "get_valkey_or_none", lambda: client)

        assert pubsub.publish_invalidate("scope_policy", "sp-1") is True
        assert len(client.published) == 1
        channel, msg = client.published[0]
        assert channel == "mimir:test:cache:invalidate:scope_policy"
        payload = json.loads(msg)
        assert payload["key"] == "sp-1"
        assert payload["worker_id"] == pubsub.WORKER_ID
        assert "ts" in payload

    def test_publish_failure_returns_false(self, monkeypatch):
        client = _FakeClient(raise_on_publish=True)
        monkeypatch.setattr(pubsub, "get_valkey_or_none", lambda: client)

        assert pubsub.publish_invalidate("scope_policy", "sp-1") is False

    def test_wildcard_key_supported(self, monkeypatch):
        client = _FakeClient()
        monkeypatch.setattr(pubsub, "get_valkey_or_none", lambda: client)

        assert pubsub.publish_invalidate("scope_policy", "*") is True
        payload = json.loads(client.published[0][1])
        assert payload["key"] == "*"

    def test_org_id_routes_to_tenant_channel(self, monkeypatch):
        client = _FakeClient()
        monkeypatch.setattr(pubsub, "get_valkey_or_none", lambda: client)

        pubsub.publish_invalidate("scope_policy", "sp-1", org_id="org-A")
        channel = client.published[0][0]
        assert "tenant:org-A" in channel


class TestSubscriberDispatch:
    def test_skips_subscribe_confirm_message(self):
        captured: list[str] = []
        sub = pubsub.Subscriber("scope_policy", on_invalidate=captured.append)

        sub.dispatch({"type": "subscribe", "channel": "x", "data": 1})
        assert captured == []

    def test_invokes_callback_on_valid_message(self):
        captured: list[str] = []
        sub = pubsub.Subscriber("scope_policy", on_invalidate=captured.append)

        msg = json.dumps({"key": "sp-42", "ts": 100.0, "worker_id": "pid-other"})
        sub.dispatch({"type": "message", "channel": "ch", "data": msg})
        assert captured == ["sp-42"]

    def test_skips_loop_self_worker(self):
        """자기 자신이 발행한 메시지는 dispatch skip (loop 방지)."""
        captured: list[str] = []
        sub = pubsub.Subscriber("scope_policy", on_invalidate=captured.append)

        msg = json.dumps({"key": "sp-42", "ts": 100.0, "worker_id": pubsub.WORKER_ID})
        sub.dispatch({"type": "message", "channel": "ch", "data": msg})
        assert captured == []

    def test_rejects_oversized_message(self):
        captured: list[str] = []
        sub = pubsub.Subscriber("scope_policy", on_invalidate=captured.append)

        big = json.dumps({"key": "x" * 2000, "ts": 0, "worker_id": "x"})
        sub.dispatch({"type": "message", "channel": "ch", "data": big})
        assert captured == []

    def test_rejects_non_json_message(self):
        captured: list[str] = []
        sub = pubsub.Subscriber("scope_policy", on_invalidate=captured.append)

        sub.dispatch({"type": "message", "channel": "ch", "data": "not-json{"})
        assert captured == []

    def test_rejects_non_dict_payload(self):
        captured: list[str] = []
        sub = pubsub.Subscriber("scope_policy", on_invalidate=captured.append)

        sub.dispatch({"type": "message", "channel": "ch", "data": json.dumps([1, 2, 3])})
        assert captured == []

    def test_rejects_missing_key(self):
        captured: list[str] = []
        sub = pubsub.Subscriber("scope_policy", on_invalidate=captured.append)

        msg = json.dumps({"ts": 0, "worker_id": "other"})  # key 없음
        sub.dispatch({"type": "message", "channel": "ch", "data": msg})
        assert captured == []

    def test_rejects_empty_key(self):
        captured: list[str] = []
        sub = pubsub.Subscriber("scope_policy", on_invalidate=captured.append)

        msg = json.dumps({"key": "", "ts": 0, "worker_id": "other"})
        sub.dispatch({"type": "message", "channel": "ch", "data": msg})
        assert captured == []

    def test_callback_exception_does_not_propagate(self):
        """on_invalidate 가 raise 해도 dispatch 는 통과 (subscriber loop 보호)."""
        def _raises(_key):
            raise RuntimeError("oops")

        sub = pubsub.Subscriber("scope_policy", on_invalidate=_raises)
        msg = json.dumps({"key": "sp-1", "ts": 0, "worker_id": "other"})
        # 예외 전파 안 함
        sub.dispatch({"type": "message", "channel": "ch", "data": msg})

    def test_bytes_message_decoded(self):
        captured: list[str] = []
        sub = pubsub.Subscriber("scope_policy", on_invalidate=captured.append)

        msg = json.dumps({"key": "sp-1", "ts": 0, "worker_id": "other"}).encode("utf-8")
        sub.dispatch({"type": "message", "channel": "ch", "data": msg})
        assert captured == ["sp-1"]

    def test_invalid_utf8_bytes_skipped(self):
        captured: list[str] = []
        sub = pubsub.Subscriber("scope_policy", on_invalidate=captured.append)

        sub.dispatch({"type": "message", "channel": "ch", "data": b"\xff\xfe\x00"})
        assert captured == []


class TestSubscriberStart:
    def test_start_skips_when_disabled(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_disabled", "1")

        sub = pubsub.Subscriber("scope_policy", on_invalidate=lambda k: None)
        # disabled 시 supervisor 미시작 (이전 호환)
        assert sub.start() is False
        assert sub.is_connected() is False

    def test_try_subscribe_returns_false_when_get_returns_none(self, monkeypatch):
        """get_valkey_or_none None 일 때 _try_subscribe False (supervisor 가 backoff)."""
        monkeypatch.setattr(pubsub, "get_valkey_or_none", lambda: None)
        monkeypatch.setattr(pubsub, "is_valkey_disabled", lambda: False)

        sub = pubsub.Subscriber("scope_policy", on_invalidate=lambda k: None)
        assert sub._try_subscribe() is False
        assert sub.is_connected() is False


class TestSubscriberAutoReconnect:
    """Codex 2차 P2 시정 — supervisor 자동 재구독."""

    def test_supervisor_retries_after_failure(self, monkeypatch):
        """첫 2회 subscribe 실패 → backoff 후 3회째 성공 (call count ≥ 3 검증)."""
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_disabled", "")
        # backoff 단축
        monkeypatch.setattr(
            pubsub.Subscriber, "RECONNECT_BACKOFF_MIN_SEC", 0.01,
        )
        monkeypatch.setattr(
            pubsub.Subscriber, "RECONNECT_BACKOFF_MAX_SEC", 0.05,
        )

        call_count = {"n": 0}
        connect_event = threading.Event()

        sub = pubsub.Subscriber("scope_policy", on_invalidate=lambda k: None)

        def _fake_try_subscribe():
            call_count["n"] += 1
            if call_count["n"] < 3:
                return False
            # 3회째 성공 → connect_event set 후 stop 신호 (test 종료)
            sub._connected = True
            sub._pubsub = _ImmediateStopPubSub(sub, signal=connect_event)
            return True

        monkeypatch.setattr(sub, "_try_subscribe", _fake_try_subscribe)

        # supervisor 시작
        assert sub.start() is True

        # 최대 1초 안에 connect_event signaled
        signaled = connect_event.wait(timeout=1.0)

        try:
            assert signaled is True, f"connect_event not signaled. call_count={call_count['n']}"
            assert call_count["n"] >= 3
        finally:
            sub.stop()

    def test_supervisor_reconnects_after_loop_error(self, monkeypatch):
        """message_loop 가 error 로 break 한 후 supervisor 가 재구독 시도."""
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_disabled", "")
        monkeypatch.setattr(
            pubsub.Subscriber, "RECONNECT_BACKOFF_MIN_SEC", 0.01,
        )

        attempt_count = {"n": 0}
        sub = pubsub.Subscriber("scope_policy", on_invalidate=lambda k: None)

        class _OneShotPubSub:
            def __init__(self, attempt):
                self.attempt = attempt
                self.calls = 0
            def get_message(self, timeout=None):
                self.calls += 1
                if self.calls > 2:
                    raise RuntimeError("connection lost (simulated)")
                return None
            def close(self):
                pass

        def _fake_try_subscribe():
            attempt_count["n"] += 1
            sub._connected = True
            sub._pubsub = _OneShotPubSub(attempt_count["n"])
            return True

        monkeypatch.setattr(sub, "_try_subscribe", _fake_try_subscribe)

        assert sub.start() is True

        # 첫 subscribe 후 loop 가 error 로 break → 재구독 발생할 시간
        import time
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if attempt_count["n"] >= 2:
                break
            time.sleep(0.05)

        try:
            assert attempt_count["n"] >= 2
        finally:
            sub.stop()

    def test_stop_clears_state(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "valkey_disabled", "")
        monkeypatch.setattr(
            pubsub.Subscriber, "RECONNECT_BACKOFF_MIN_SEC", 0.01,
        )

        sub = pubsub.Subscriber("scope_policy", on_invalidate=lambda k: None)
        monkeypatch.setattr(sub, "_try_subscribe", lambda: False)  # 영원히 실패

        assert sub.start() is True

        sub.stop()

        # supervisor 가 곧 종료되어야 함
        import time
        if sub._thread is not None:
            sub._thread.join(timeout=1.0)
            assert not sub._thread.is_alive()
        assert sub.is_connected() is False


class _ImmediateStopPubSub:
    """get_message 호출 시 sub._stop_event 를 set 해서 message loop 즉시 종료.

    signal 인자가 있으면 stop 직전에 set — 테스트 동기화용.
    """

    def __init__(self, sub, signal=None):
        self.sub = sub
        self._signal = signal

    def get_message(self, timeout=None):
        if self._signal is not None:
            self._signal.set()
        self.sub._stop_event.set()
        return None

    def close(self):
        pass
