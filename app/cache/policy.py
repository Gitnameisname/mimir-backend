"""Valkey feature 별 fail-open / fail-closed 분류 — S3 Phase 7 FG 7-1.

R-I2 명문화. 각 feature 는 Valkey 장애 시 다음 두 정책 중 하나로 동작한다.

- **fail-open**: Valkey 장애 시 워커별 fallback (가용성 우선 — 성능/감사 dedup 등)
- **fail-closed**: Valkey 장애 시 캐시 비움 / 보수 default (보안 우선 — 정책 조회 등)

분류는 환경변수 ``VALKEY_FAIL_OPEN_FEATURES`` / ``VALKEY_FAIL_CLOSED_FEATURES`` 로
override 가능. 둘 다 지정된 feature 는 **fail-closed 우선** (보안 보수).

미등록 feature 는 default = fail-closed (보수 default — 알 수 없는 feature 는 거부).

함수도서관: ``docs/함수도서관/backend.md`` §1.11-fg71.
"""
from __future__ import annotations

from enum import Enum
from typing import FrozenSet

from app.config import settings

__all__ = [
    "FailPolicy",
    "policy_for",
    "is_fail_open",
    "is_fail_closed",
]


class FailPolicy(str, Enum):
    """Valkey feature 장애 정책."""

    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


# 기본 분류 — Phase 7 개발계획서 §1.2 / §3.3 정합.
_DEFAULT_POLICY: dict[str, FailPolicy] = {
    "viewed_throttle": FailPolicy.FAIL_OPEN,   # 감사 dedup — 가용성 우선
    "rate_limit": FailPolicy.FAIL_OPEN,        # rate-limit — 가용성 우선
    "scope_policy": FailPolicy.FAIL_CLOSED,    # 정책 게이트 — 보안 우선
    "response_cache": FailPolicy.FAIL_OPEN,    # 응답 캐시 — 성능
    "admin_settings": FailPolicy.FAIL_OPEN,    # 설정 캐시 — 운영자 PATCH 후 다음 호출 자동 재로드
}


def _parse_csv(value: str) -> FrozenSet[str]:
    if not value:
        return frozenset()
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _fail_open_overrides() -> FrozenSet[str]:
    return _parse_csv(settings.valkey_fail_open_features)


def _fail_closed_overrides() -> FrozenSet[str]:
    return _parse_csv(settings.valkey_fail_closed_features)


def policy_for(feature: str) -> FailPolicy:
    """feature 의 fail 정책을 반환한다.

    우선순위:
        1. 환경변수 ``VALKEY_FAIL_CLOSED_FEATURES`` 포함 → FAIL_CLOSED (보안 우선)
        2. 환경변수 ``VALKEY_FAIL_OPEN_FEATURES`` 포함 → FAIL_OPEN
        3. _DEFAULT_POLICY 에 등록 → 등록값
        4. 미등록 → FAIL_CLOSED (보수 default)
    """
    closed_override = _fail_closed_overrides()
    if feature in closed_override:
        return FailPolicy.FAIL_CLOSED
    open_override = _fail_open_overrides()
    if feature in open_override:
        return FailPolicy.FAIL_OPEN
    return _DEFAULT_POLICY.get(feature, FailPolicy.FAIL_CLOSED)


def is_fail_open(feature: str) -> bool:
    return policy_for(feature) is FailPolicy.FAIL_OPEN


def is_fail_closed(feature: str) -> bool:
    return policy_for(feature) is FailPolicy.FAIL_CLOSED
