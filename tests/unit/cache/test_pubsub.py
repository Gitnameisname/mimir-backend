"""S3 Phase 7 FG 7-3 — Valkey pub/sub primitives 단위 테스트."""
from __future__ import annotations

import json

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
        assert sub.start() is False

    def test_start_skips_when_get_returns_none(self, monkeypatch):
        monkeypatch.setattr(pubsub, "get_valkey_or_none", lambda: None)
        monkeypatch.setattr(pubsub, "is_valkey_disabled", lambda: False)

        sub = pubsub.Subscriber("scope_policy", on_invalidate=lambda k: None)
        assert sub.start() is False
