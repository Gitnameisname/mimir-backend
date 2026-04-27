"""S3 Phase 3 FG 3-3 — notifications_service 단위 테스트.

대상:
  - enqueue_mention (rate-limit / self-mention skip / anonymous skip)
  - list_for_user / mark_read / count_unread (본인 권한 강제)
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.services import notifications_service as ns_mod
from app.services.notifications_service import (
    DEFAULT_MENTION_RATE_LIMIT_PER_MIN,
    NotificationsService,
)


@pytest.fixture
def _mock_repo(monkeypatch):
    repo = MagicMock()
    repo.enqueue.return_value = MagicMock(id="notif-1")
    repo.count_recent_per_pair.return_value = 0
    repo.list_for_user.return_value = []
    repo.mark_read.return_value = 0
    repo.count_unread.return_value = 0
    monkeypatch.setattr(ns_mod, "notifications_repository", repo)
    return repo


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ANNOTATION_MENTION_RATE_LIMIT_PER_MIN", raising=False)


def _actor(*, actor_id="u-bob", auth=True):
    a = MagicMock()
    a.actor_id = actor_id
    a.is_authenticated = auth
    return a


class TestEnqueueMention:
    def test_normal_creates_notification(self, _mock_repo):
        result = NotificationsService().enqueue_mention(
            conn=MagicMock(),
            author_id="u-alice", recipient_id="u-bob",
            annotation_id="ann-1", document_id="doc-1", snippet="hello",
        )
        assert result is not None
        _mock_repo.enqueue.assert_called_once()
        call_kwargs = _mock_repo.enqueue.call_args.kwargs
        assert call_kwargs["user_id"] == "u-bob"
        assert call_kwargs["kind"] == "annotation.mention"
        assert call_kwargs["payload"]["author_id"] == "u-alice"
        assert call_kwargs["payload"]["snippet"] == "hello"

    def test_self_mention_skipped(self, _mock_repo):
        result = NotificationsService().enqueue_mention(
            conn=MagicMock(),
            author_id="u-alice", recipient_id="u-alice",
            annotation_id="ann-1", document_id="doc-1", snippet="me",
        )
        assert result is None
        _mock_repo.enqueue.assert_not_called()

    def test_anonymous_author_skipped(self, _mock_repo):
        result = NotificationsService().enqueue_mention(
            conn=MagicMock(),
            author_id="", recipient_id="u-bob",
            annotation_id="ann-1", document_id="doc-1", snippet="x",
        )
        assert result is None

    def test_anonymous_recipient_skipped(self, _mock_repo):
        result = NotificationsService().enqueue_mention(
            conn=MagicMock(),
            author_id="u-alice", recipient_id="",
            annotation_id="ann-1", document_id="doc-1", snippet="x",
        )
        assert result is None

    def test_rate_limit_blocks_after_n(self, _mock_repo):
        # 이미 5건 (default) 발송됨 → skip
        _mock_repo.count_recent_per_pair.return_value = DEFAULT_MENTION_RATE_LIMIT_PER_MIN
        result = NotificationsService().enqueue_mention(
            conn=MagicMock(),
            author_id="u-alice", recipient_id="u-bob",
            annotation_id="ann-1", document_id="doc-1", snippet="6번째",
        )
        assert result is None
        _mock_repo.enqueue.assert_not_called()

    def test_rate_limit_just_below_passes(self, _mock_repo):
        _mock_repo.count_recent_per_pair.return_value = DEFAULT_MENTION_RATE_LIMIT_PER_MIN - 1
        result = NotificationsService().enqueue_mention(
            conn=MagicMock(),
            author_id="u-alice", recipient_id="u-bob",
            annotation_id="ann-1", document_id="doc-1", snippet="5번째 OK",
        )
        assert result is not None

    def test_rate_limit_zero_disables(self, _mock_repo, monkeypatch):
        monkeypatch.setenv("ANNOTATION_MENTION_RATE_LIMIT_PER_MIN", "0")
        _mock_repo.count_recent_per_pair.return_value = 9999  # 막대한 양
        result = NotificationsService().enqueue_mention(
            conn=MagicMock(),
            author_id="u-alice", recipient_id="u-bob",
            annotation_id="ann-1", document_id="doc-1", snippet="x",
        )
        assert result is not None  # rate-limit off → enqueue

    def test_snippet_truncated_to_200_chars(self, _mock_repo):
        long = "x" * 500
        NotificationsService().enqueue_mention(
            conn=MagicMock(),
            author_id="u-alice", recipient_id="u-bob",
            annotation_id="ann-1", document_id="doc-1", snippet=long,
        )
        snippet_in_payload = _mock_repo.enqueue.call_args.kwargs["payload"]["snippet"]
        assert len(snippet_in_payload) == 200


class TestListForUser:
    def test_returns_empty_for_anonymous(self, _mock_repo):
        result = NotificationsService().list_for_user(
            conn=MagicMock(), actor=_actor(auth=False),
        )
        assert result == []
        _mock_repo.list_for_user.assert_not_called()

    def test_returns_for_authenticated(self, _mock_repo):
        _mock_repo.list_for_user.return_value = [MagicMock(id="n1"), MagicMock(id="n2")]
        result = NotificationsService().list_for_user(
            conn=MagicMock(), actor=_actor(actor_id="u-bob"),
        )
        assert len(result) == 2
        _mock_repo.list_for_user.assert_called_once()
        assert _mock_repo.list_for_user.call_args.kwargs["user_id"] == "u-bob"


class TestMarkRead:
    def test_anonymous_returns_zero(self, _mock_repo):
        result = NotificationsService().mark_read(
            conn=MagicMock(), actor=_actor(auth=False),
            notification_ids=["n1"],
        )
        assert result == 0

    def test_authenticated_passes_through(self, _mock_repo):
        _mock_repo.mark_read.return_value = 2
        result = NotificationsService().mark_read(
            conn=MagicMock(), actor=_actor(actor_id="u-bob"),
            notification_ids=["n1", "n2"],
        )
        assert result == 2
        # SQL 안 user_id 필터로 본인 알림만 처리
        assert _mock_repo.mark_read.call_args.kwargs["user_id"] == "u-bob"


class TestCountUnread:
    def test_anonymous_returns_zero(self, _mock_repo):
        assert NotificationsService().count_unread(MagicMock(), _actor(auth=False)) == 0

    def test_authenticated_passes_through(self, _mock_repo):
        _mock_repo.count_unread.return_value = 3
        assert NotificationsService().count_unread(MagicMock(), _actor()) == 3
