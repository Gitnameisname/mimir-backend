"""S3 Phase 3 FG 3-1 — contributors_service 단위 테스트.

대상: backend/app/services/contributors_service.py

커버 범위:
  - _normalize_actor_type (4 케이스 + alien)
  - _placeholder_display_name
  - get_contributors:
      * 빈 카테고리 (creator 만 있음)
      * editors 만 있음
      * editors + approvers + viewers 정상
      * creator 가 editors 에서 제외됨
      * include_viewers=False 면 repository.list_viewers 미호출
      * include_viewers=True + since None → 30일 default 적용
      * since 명시 시 그 값 그대로 전달
      * limit_per_section clamp (1~200)
      * actor_type alien 값 → 'user' default
      * agent / system actor 의 placeholder 표시명
      * ACL 차단 (documents_service.get_document 가 raise) 시 그대로 전파
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services import contributors_service as cs_mod
from app.services.contributors_service import (
    DEFAULT_LIMIT_PER_SECTION,
    DEFAULT_VIEWER_SINCE_DAYS,
    MAX_LIMIT_PER_SECTION,
    ContributorsService,
    _normalize_actor_type,
    _placeholder_display_name,
    _resolve_display_name,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def _mock_repo(monkeypatch):
    """contributors_repository 의 4 메서드를 MagicMock 으로 교체."""
    repo = MagicMock()
    repo.get_creator = MagicMock(return_value=None)
    repo.list_editors = MagicMock(return_value=[])
    repo.list_approvers = MagicMock(return_value=[])
    repo.list_viewers = MagicMock(return_value=[])
    monkeypatch.setattr(cs_mod, "contributors_repository", repo)
    return repo


@pytest.fixture
def _mock_users(monkeypatch):
    """UsersRepository.get_many_by_ids 를 mock — 기본 빈 dict."""
    fake = MagicMock()
    fake.get_many_by_ids = MagicMock(return_value={})
    monkeypatch.setattr(cs_mod, "_users_repository", fake)
    return fake


@pytest.fixture
def _mock_get_document(monkeypatch):
    """documents_service.get_document 가 ACL 통과시키도록 stub."""
    monkeypatch.setattr(
        cs_mod.documents_service,
        "get_document",
        MagicMock(return_value=SimpleNamespace(id="doc-1")),
    )
    return cs_mod.documents_service.get_document


@pytest.fixture
def _mock_policy_gate(monkeypatch):
    """should_expose_viewers default → True (테스트 편의).
    정책 차단 케이스는 별도 fixture override 로 검증.
    """
    gate = MagicMock(return_value=True)
    monkeypatch.setattr(cs_mod, "should_expose_viewers", gate)
    return gate


def _user(id: str, name: str, role: str | None = None):
    return SimpleNamespace(id=id, display_name=name, role_name=role)


# ---------------------------------------------------------------------------
# _normalize_actor_type
# ---------------------------------------------------------------------------

class TestNormalizeActorType:
    def test_user(self):
        assert _normalize_actor_type("user") == "user"

    def test_agent(self):
        assert _normalize_actor_type("agent") == "agent"

    def test_system(self):
        assert _normalize_actor_type("system") == "system"

    def test_uppercase(self):
        assert _normalize_actor_type("Agent") == "agent"

    def test_service_legacy_maps_to_user(self):
        # BE-G4 이전 레거시 'service' 잔존 시 → user (보수적)
        assert _normalize_actor_type("service") == "user"

    def test_none_defaults_to_user(self):
        assert _normalize_actor_type(None) == "user"

    def test_empty_defaults_to_user(self):
        assert _normalize_actor_type("") == "user"

    def test_alien_value_defaults_to_user(self):
        assert _normalize_actor_type("xyz") == "user"


# ---------------------------------------------------------------------------
# _placeholder_display_name
# ---------------------------------------------------------------------------

class TestPlaceholderDisplayName:
    def test_user_placeholder(self):
        assert _placeholder_display_name("user") == "(알 수 없는 사용자)"

    def test_agent_placeholder(self):
        assert _placeholder_display_name("agent") == "에이전트"

    def test_system_placeholder(self):
        assert _placeholder_display_name("system") == "Mimir 시스템"


# ---------------------------------------------------------------------------
# _resolve_display_name
# ---------------------------------------------------------------------------

class TestResolveDisplayName:
    def test_user_with_known_display_name(self):
        user_map = {"u1": _user("u1", "최철균")}
        assert _resolve_display_name("u1", "user", user_map) == "최철균"

    def test_user_unknown_falls_back_to_placeholder(self):
        assert _resolve_display_name("u-missing", "user", {}) == "(알 수 없는 사용자)"

    def test_user_with_blank_display_name(self):
        user_map = {"u1": _user("u1", "")}
        assert _resolve_display_name("u1", "user", user_map) == "(알 수 없는 사용자)"

    def test_agent_placeholder_regardless_of_user_map(self):
        user_map = {"agent-1": _user("agent-1", "should-not-be-used")}
        assert _resolve_display_name("agent-1", "agent", user_map) == "에이전트"

    def test_system_placeholder(self):
        assert _resolve_display_name("system-id", "system", {}) == "Mimir 시스템"


# ---------------------------------------------------------------------------
# ContributorsService.get_contributors
# ---------------------------------------------------------------------------

class TestGetContributors:
    def test_empty_document_only_creator(self, _mock_repo, _mock_users, _mock_get_document):
        _mock_repo.get_creator.return_value = {
            "actor_id": "u-creator",
            "last_activity_at": datetime(2026, 4, 27, tzinfo=timezone.utc),
        }
        _mock_users.get_many_by_ids.return_value = {
            "u-creator": _user("u-creator", "Alice", "AUTHOR"),
        }

        bundle = ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1",
        )

        assert bundle.creator is not None
        assert bundle.creator.actor_id == "u-creator"
        assert bundle.creator.display_name == "Alice"
        assert bundle.creator.actor_type == "user"
        assert bundle.creator.role_badge == "AUTHOR"
        assert bundle.editors == []
        assert bundle.approvers == []
        assert bundle.viewers == []
        assert bundle.viewers_included is False

    def test_editors_excludes_creator(self, _mock_repo, _mock_users, _mock_get_document):
        _mock_repo.get_creator.return_value = {
            "actor_id": "u-creator",
            "last_activity_at": datetime(2026, 4, 27, tzinfo=timezone.utc),
        }
        _mock_repo.list_editors.return_value = [
            {
                "actor_id": "u-creator",  # creator → 제외 대상
                "actor_type": "user",
                "actor_role": "AUTHOR",
                "last_activity_at": datetime(2026, 4, 26, tzinfo=timezone.utc),
            },
            {
                "actor_id": "u-editor",
                "actor_type": "user",
                "actor_role": "AUTHOR",
                "last_activity_at": datetime(2026, 4, 25, tzinfo=timezone.utc),
            },
        ]
        _mock_users.get_many_by_ids.return_value = {
            "u-creator": _user("u-creator", "Alice"),
            "u-editor": _user("u-editor", "Bob"),
        }

        bundle = ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1",
        )

        assert bundle.creator.actor_id == "u-creator"
        assert len(bundle.editors) == 1
        assert bundle.editors[0].actor_id == "u-editor"
        assert bundle.editors[0].display_name == "Bob"

    def test_include_viewers_false_skips_repo_call(self, _mock_repo, _mock_users, _mock_get_document):
        _mock_repo.get_creator.return_value = None

        bundle = ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1", include_viewers=False,
        )

        _mock_repo.list_viewers.assert_not_called()
        assert bundle.viewers_included is False
        assert bundle.viewers == []

    def test_include_viewers_true_default_since_30_days(
        self, _mock_repo, _mock_users, _mock_get_document, _mock_policy_gate, monkeypatch,
    ):
        fixed_now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(cs_mod, "utcnow", lambda: fixed_now)

        _mock_repo.get_creator.return_value = None
        _mock_repo.list_viewers.return_value = []

        ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1", include_viewers=True,
        )

        _mock_repo.list_viewers.assert_called_once()
        kwargs = _mock_repo.list_viewers.call_args.kwargs
        expected_since = fixed_now - timedelta(days=DEFAULT_VIEWER_SINCE_DAYS)
        assert kwargs["since"] == expected_since
        assert kwargs["limit"] == DEFAULT_LIMIT_PER_SECTION

    def test_include_viewers_true_with_explicit_since(
        self, _mock_repo, _mock_users, _mock_get_document, _mock_policy_gate,
    ):
        explicit_since = datetime(2026, 4, 1, tzinfo=timezone.utc)
        _mock_repo.get_creator.return_value = None
        _mock_repo.list_viewers.return_value = []

        ContributorsService().get_contributors(
            conn=MagicMock(),
            document_id="doc-1",
            since=explicit_since,
            include_viewers=True,
        )

        kwargs = _mock_repo.list_viewers.call_args.kwargs
        # since 명시 → viewer_since 도 그 값 그대로
        assert kwargs["since"] == explicit_since

    def test_limit_per_section_clamped_low(self, _mock_repo, _mock_users, _mock_get_document):
        _mock_repo.get_creator.return_value = None

        ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1", limit_per_section=0,
        )

        # 0 → 1 로 clamp
        assert _mock_repo.list_editors.call_args.kwargs["limit"] == 1
        assert _mock_repo.list_approvers.call_args.kwargs["limit"] == 1

    def test_limit_per_section_clamped_high(self, _mock_repo, _mock_users, _mock_get_document):
        _mock_repo.get_creator.return_value = None

        ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1", limit_per_section=99999,
        )

        assert _mock_repo.list_editors.call_args.kwargs["limit"] == MAX_LIMIT_PER_SECTION

    def test_agent_actor_uses_placeholder(self, _mock_repo, _mock_users, _mock_get_document):
        _mock_repo.get_creator.return_value = None
        _mock_repo.list_editors.return_value = [
            {
                "actor_id": "agent-uuid-1",
                "actor_type": "agent",
                "actor_role": None,
                "last_activity_at": datetime(2026, 4, 27, tzinfo=timezone.utc),
            },
        ]

        bundle = ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1",
        )

        assert len(bundle.editors) == 1
        assert bundle.editors[0].actor_type == "agent"
        assert bundle.editors[0].display_name == "에이전트"

    def test_system_actor_uses_placeholder(self, _mock_repo, _mock_users, _mock_get_document):
        _mock_repo.get_creator.return_value = None
        _mock_repo.list_editors.return_value = [
            {
                "actor_id": "system-1",
                "actor_type": "system",
                "actor_role": None,
                "last_activity_at": datetime(2026, 4, 27, tzinfo=timezone.utc),
            },
        ]

        bundle = ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1",
        )

        assert bundle.editors[0].actor_type == "system"
        assert bundle.editors[0].display_name == "Mimir 시스템"

    def test_alien_actor_type_defaults_to_user(self, _mock_repo, _mock_users, _mock_get_document):
        _mock_repo.get_creator.return_value = None
        _mock_repo.list_editors.return_value = [
            {
                "actor_id": "x-1",
                "actor_type": "robot",  # alien
                "actor_role": None,
                "last_activity_at": datetime(2026, 4, 27, tzinfo=timezone.utc),
            },
        ]

        bundle = ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1",
        )

        assert bundle.editors[0].actor_type == "user"
        # user_map 에 없음 → placeholder
        assert bundle.editors[0].display_name == "(알 수 없는 사용자)"

    def test_acl_failure_propagates(self, _mock_repo, _mock_users, monkeypatch):
        from app.api.errors.exceptions import ApiNotFoundError

        def _raise(*a, **kw):
            raise ApiNotFoundError("문서를 찾을 수 없습니다: doc-1")

        monkeypatch.setattr(cs_mod.documents_service, "get_document", _raise)

        with pytest.raises(ApiNotFoundError):
            ContributorsService().get_contributors(
                conn=MagicMock(), document_id="doc-1",
            )

        # repository 호출 안 됨 (ACL 차단)
        _mock_repo.get_creator.assert_not_called()

    def test_creator_with_unknown_user(self, _mock_repo, _mock_users, _mock_get_document):
        _mock_repo.get_creator.return_value = {
            "actor_id": "u-deleted",
            "last_activity_at": datetime(2026, 4, 27, tzinfo=timezone.utc),
        }
        _mock_users.get_many_by_ids.return_value = {}  # user 삭제됨

        bundle = ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1",
        )

        assert bundle.creator.actor_id == "u-deleted"
        assert bundle.creator.display_name == "(알 수 없는 사용자)"
        assert bundle.creator.role_badge is None

    def test_approvers_normal(self, _mock_repo, _mock_users, _mock_get_document):
        _mock_repo.get_creator.return_value = None
        _mock_repo.list_approvers.return_value = [
            {
                "actor_id": "u-approver-1",
                "actor_role": "APPROVER",
                "last_activity_at": datetime(2026, 4, 27, tzinfo=timezone.utc),
            },
            {
                "actor_id": "u-approver-2",
                "actor_role": "ORG_ADMIN",
                "last_activity_at": datetime(2026, 4, 26, tzinfo=timezone.utc),
            },
        ]
        _mock_users.get_many_by_ids.return_value = {
            "u-approver-1": _user("u-approver-1", "Carol"),
            "u-approver-2": _user("u-approver-2", "Dave"),
        }

        bundle = ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1",
        )

        assert len(bundle.approvers) == 2
        assert bundle.approvers[0].display_name == "Carol"
        assert bundle.approvers[0].role_badge == "APPROVER"
        assert bundle.approvers[1].display_name == "Dave"

    def test_viewers_marked_included_when_requested(
        self, _mock_repo, _mock_users, _mock_get_document, _mock_policy_gate,
    ):
        _mock_repo.get_creator.return_value = None
        _mock_repo.list_viewers.return_value = [
            {
                "actor_id": "u-viewer",
                "actor_type": "user",
                "actor_role": None,
                "last_activity_at": datetime(2026, 4, 27, tzinfo=timezone.utc),
            },
        ]
        _mock_users.get_many_by_ids.return_value = {
            "u-viewer": _user("u-viewer", "Eve"),
        }

        bundle = ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1", include_viewers=True,
        )

        assert bundle.viewers_included is True
        assert len(bundle.viewers) == 1
        assert bundle.viewers[0].display_name == "Eve"


# ---------------------------------------------------------------------------
# FG 3-2 결합 — should_expose_viewers 정책 게이트
# ---------------------------------------------------------------------------

class TestPolicyGateIntegration:
    def test_policy_blocks_viewers_even_when_user_requests(
        self, _mock_repo, _mock_users, _mock_get_document, monkeypatch,
    ):
        # 정책 게이트가 False 반환 — 사용자가 include_viewers=True 라도 차단
        gate = MagicMock(return_value=False)
        monkeypatch.setattr(cs_mod, "should_expose_viewers", gate)

        _mock_repo.get_creator.return_value = None

        bundle = ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1", include_viewers=True,
        )

        assert bundle.viewers_included is False
        assert bundle.viewers == []
        # repository.list_viewers 호출 자체가 안 됨 (성능 + 보안)
        _mock_repo.list_viewers.assert_not_called()

    def test_policy_does_not_force_true_when_user_says_false(
        self, _mock_repo, _mock_users, _mock_get_document, monkeypatch,
    ):
        # 정책이 True 라도 사용자가 include_viewers=False 면 차단 (사용자 의사 우선)
        gate = MagicMock(return_value=True)
        monkeypatch.setattr(cs_mod, "should_expose_viewers", gate)

        _mock_repo.get_creator.return_value = None

        bundle = ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1", include_viewers=False,
        )

        assert bundle.viewers_included is False
        _mock_repo.list_viewers.assert_not_called()

    def test_policy_gate_called_with_viewer_actor(
        self, _mock_repo, _mock_users, _mock_get_document, monkeypatch,
    ):
        gate = MagicMock(return_value=True)
        monkeypatch.setattr(cs_mod, "should_expose_viewers", gate)
        _mock_repo.get_creator.return_value = None
        viewer = SimpleNamespace(
            actor_id="u-1", is_authenticated=True, scope_profile_id="sp-1",
        )

        ContributorsService().get_contributors(
            conn=MagicMock(),
            document_id="doc-1",
            viewer_actor=viewer,
            include_viewers=True,
        )

        gate.assert_called_once_with(viewer)

    def test_both_user_and_policy_true_allows_viewers(
        self, _mock_repo, _mock_users, _mock_get_document, monkeypatch,
    ):
        gate = MagicMock(return_value=True)
        monkeypatch.setattr(cs_mod, "should_expose_viewers", gate)
        _mock_repo.get_creator.return_value = None
        _mock_repo.list_viewers.return_value = []

        bundle = ContributorsService().get_contributors(
            conn=MagicMock(), document_id="doc-1", include_viewers=True,
        )

        assert bundle.viewers_included is True
        _mock_repo.list_viewers.assert_called_once()
