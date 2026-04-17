"""
ScopeProfile 도메인 모델 — Phase 4 (S2).

S2 원칙 ⑤: 접근 범위(scope)는 코드에 하드코딩 금지.
관리자가 ScopeProfile을 생성하고, 에이전트 API Key에 바인딩하여 동적으로 제어한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class FilterCondition:
    """단일 ACL 필터 조건.

    field: 필터 대상 필드명 (organization_id, team_id, visibility, classification 등)
    op:    연산자 — eq | neq | in | not_in | contains
    value: 비교값 또는 $ctx 동적 변수 (예: "$ctx.organization_id")
    """
    field: str
    op: str  # eq | neq | in | not_in | contains
    value: Any

    _ALLOWED_OPS = frozenset({"eq", "neq", "in", "not_in", "contains"})
    _ALLOWED_FIELDS = frozenset({
        "organization_id", "team_id", "visibility", "classification",
        "document_type", "is_public", "accessible_roles", "accessible_org_ids",
    })

    def validate(self) -> None:
        if self.op not in self._ALLOWED_OPS:
            raise ValueError(f"Unsupported op: {self.op!r}. Allowed: {self._ALLOWED_OPS}")
        if self.field not in self._ALLOWED_FIELDS:
            raise ValueError(f"Unsupported field: {self.field!r}. Allowed: {self._ALLOWED_FIELDS}")


@dataclass
class FilterExpression:
    """and/or 조합 ACL 필터 표현식.

    at most one of `and_` or `or_` is populated; both may be empty (pass-through).
    """
    and_: list[FilterCondition] = field(default_factory=list)
    or_: list[FilterCondition] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.and_ and not self.or_

    def to_dict(self) -> dict:
        result: dict = {}
        if self.and_:
            result["and"] = [{"field": c.field, "op": c.op, "value": c.value} for c in self.and_]
        if self.or_:
            result["or"] = [{"field": c.field, "op": c.op, "value": c.value} for c in self.or_]
        return result


@dataclass
class ScopeDefinition:
    """ScopeProfile 내 단일 scope 항목 (scope_name → acl_filter 매핑)."""
    id: str
    scope_profile_id: str
    scope_name: str
    description: Optional[str]
    acl_filter: dict  # raw JSON — FilterExpression으로 파싱 필요
    created_at: datetime


@dataclass
class ScopeProfile:
    """에이전트에 바인딩되는 ACL 필터 템플릿."""
    id: str
    name: str
    description: Optional[str]
    organization_id: Optional[str]
    created_at: datetime
    updated_at: datetime
    scopes: list[ScopeDefinition] = field(default_factory=list)
