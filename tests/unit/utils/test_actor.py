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
from app.utils.actor import actor_type_str


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
