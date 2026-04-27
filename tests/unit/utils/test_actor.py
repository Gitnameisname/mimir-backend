"""Unit tests for :mod:`app.utils.actor`.

Covers:
    - ``actor_type_str``: 4 ActorType 케이스 매핑 (USER/AGENT/SERVICE/ANONYMOUS).
    - ``None`` / 누락 actor_type 방어적 fallback.
    - ``ActorContext.audit_actor_type`` property 가 본 helper 위에서 동작.
    - 반환값이 AuditEmitter Literal 과 일치 (보안 시맨틱).
Docs: ``docs/함수도서관/backend.md`` §1.6 BE-G4.
"""
from __future__ import annotations

import pytest

from app.api.auth.models import ActorContext, ActorType, AuthMethod
from app.api.errors.exceptions import ApiPermissionDeniedError, ApiValidationError
from app.utils.actor import (
    ADMIN_ROLES,
    actor_type_str,
    require_actor_id,
    require_admin,
)


def _make(actor_type: ActorType, *, is_authenticated: bool = True) -> ActorContext:
    return ActorContext(
        actor_type=actor_type,
        actor_id="test-id" if is_authenticated else None,
        is_authenticated=is_authenticated,
        auth_method=AuthMethod.SESSION if is_authenticated else None,
        tenant_id=None,
    )


# ---------------------------------------------------------------------------
# 1. 4 ActorType 매핑
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actor_type", "expected"),
    [
        (ActorType.USER, "user"),
        (ActorType.AGENT, "agent"),
        (ActorType.SERVICE, "system"),
        (ActorType.ANONYMOUS, "user"),
    ],
)
def test_actor_type_str_mapping(actor_type, expected) -> None:
    actor = _make(actor_type, is_authenticated=(actor_type != ActorType.ANONYMOUS))
    assert actor_type_str(actor) == expected


# ---------------------------------------------------------------------------
# 2. None / 누락 방어
# ---------------------------------------------------------------------------


def test_none_actor_returns_user() -> None:
    assert actor_type_str(None) == "user"


def test_missing_actor_type_attribute_returns_user() -> None:
    """ActorContext 가 아닌 더미 객체 (actor_type 누락) 도 안전하게 'user'."""
    class _Empty:
        pass
    assert actor_type_str(_Empty()) == "user"  # type: ignore[arg-type]


def test_actor_type_set_to_none_returns_user() -> None:
    class _Bad:
        actor_type = None
    assert actor_type_str(_Bad()) == "user"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. AuditEmitter Literal 일치 (보안 시맨틱)
# ---------------------------------------------------------------------------


def test_returns_only_emitter_literal_values() -> None:
    """반환값은 항상 AuditEmitter 의 Literal 3종 안에 있어야 한다."""
    valid = {"user", "agent", "system"}
    for at in [ActorType.USER, ActorType.AGENT, ActorType.SERVICE, ActorType.ANONYMOUS]:
        result = actor_type_str(_make(at))
        assert result in valid, f"{at} → {result!r} is not in {valid}"


def test_service_never_returns_service_literal() -> None:
    """잠재 보안 버그 회귀 가드 — SERVICE → 'service' 반환 금지 (emitter Literal 위반)."""
    actor = _make(ActorType.SERVICE)
    result = actor_type_str(actor)
    assert result == "system"
    assert result != "service"


# ---------------------------------------------------------------------------
# 4. ActorContext.audit_actor_type property 위임
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actor_type", "expected"),
    [
        (ActorType.USER, "user"),
        (ActorType.AGENT, "agent"),
        (ActorType.SERVICE, "system"),
        (ActorType.ANONYMOUS, "user"),
    ],
)
def test_audit_actor_type_property_uses_helper(actor_type, expected) -> None:
    """ActorContext.audit_actor_type 가 actor_type_str 와 동일 결과."""
    actor = _make(actor_type, is_authenticated=(actor_type != ActorType.ANONYMOUS))
    assert actor.audit_actor_type == expected
    assert actor.audit_actor_type == actor_type_str(actor)


# ===========================================================================
# BE-G6 (2026-04-25) — require_actor_id
# ===========================================================================


def _make_with_role(role: str | None, is_authenticated: bool = True) -> ActorContext:
    return ActorContext(
        actor_type=ActorType.USER,
        actor_id="user-1" if is_authenticated else None,
        is_authenticated=is_authenticated,
        auth_method=AuthMethod.SESSION if is_authenticated else None,
        tenant_id=None,
        role=role,
    )


class TestRequireActorId:
    def test_authenticated_actor_returns_id(self) -> None:
        actor = _make(ActorType.USER, is_authenticated=True)
        assert require_actor_id(actor) == "test-id"

    def test_none_actor_raises(self) -> None:
        with pytest.raises(ApiValidationError) as exc_info:
            require_actor_id(None)
        assert "인증된 사용자만" in exc_info.value.message

    def test_anonymous_actor_raises(self) -> None:
        actor = _make(ActorType.ANONYMOUS, is_authenticated=False)
        with pytest.raises(ApiValidationError):
            require_actor_id(actor)

    def test_custom_label_in_message(self) -> None:
        with pytest.raises(ApiValidationError) as exc_info:
            require_actor_id(None, label="폴더")
        assert "인증된 폴더만" in exc_info.value.message

    def test_keyword_only_label(self) -> None:
        actor = _make(ActorType.USER)
        with pytest.raises(TypeError):
            require_actor_id(actor, "폴더")  # type: ignore[misc]

    def test_returns_str(self) -> None:
        actor = _make(ActorType.USER)
        result = require_actor_id(actor)
        assert isinstance(result, str)


# ===========================================================================
# BE-G6 (2026-04-25) — require_admin
# ===========================================================================


class TestRequireAdmin:
    def test_org_admin_passes(self) -> None:
        actor = _make_with_role("ORG_ADMIN")
        # 예외 없이 반환
        assert require_admin(actor) is None

    def test_super_admin_passes(self) -> None:
        actor = _make_with_role("SUPER_ADMIN")
        assert require_admin(actor) is None

    def test_viewer_role_raises(self) -> None:
        actor = _make_with_role("VIEWER")
        with pytest.raises(ApiPermissionDeniedError) as exc_info:
            require_admin(actor)
        assert "관리자 권한" in str(exc_info.value)

    def test_author_role_raises(self) -> None:
        actor = _make_with_role("AUTHOR")
        with pytest.raises(ApiPermissionDeniedError):
            require_admin(actor)

    def test_unauthenticated_raises(self) -> None:
        actor = _make_with_role("ORG_ADMIN", is_authenticated=False)
        with pytest.raises(ApiPermissionDeniedError):
            require_admin(actor)

    def test_no_role_raises(self) -> None:
        actor = _make_with_role(None)
        with pytest.raises(ApiPermissionDeniedError):
            require_admin(actor)


# ===========================================================================
# BE-G6 (2026-04-25) — ADMIN_ROLES 상수
# ===========================================================================


class TestAdminRoles:
    def test_contains_org_admin(self) -> None:
        assert "ORG_ADMIN" in ADMIN_ROLES

    def test_contains_super_admin(self) -> None:
        assert "SUPER_ADMIN" in ADMIN_ROLES

    def test_excludes_lower_roles(self) -> None:
        for role in ["VIEWER", "AUTHOR", "REVIEWER", "APPROVER"]:
            assert role not in ADMIN_ROLES

    def test_immutable_frozenset(self) -> None:
        assert isinstance(ADMIN_ROLES, frozenset)
        with pytest.raises(AttributeError):
            ADMIN_ROLES.add("NEW_ROLE")  # type: ignore[attr-defined]
