"""Valkey 키 / 채널 네임스페이스 합성 — S3 Phase 7 FG 7-1.

목적:
    - 환경(dev/staging/prod/test) 분리를 코드 contract 수준에서 강제 (R-I4)
    - tenant 격리가 필요한 pub/sub 채널에 prefix 강제 (R-I3)

규약:
    - 키 prefix: ``mimir:<env>:`` (settings.valkey_namespace override 가능)
    - 채널 prefix: 같은 prefix + ``cache:invalidate:`` 또는 ``tenant:<org_id>:cache:invalidate:``

기존 ``app.cache.response_cache`` 의 ``mimir:`` prefix 와 충돌하지 않는다
(env 가 추가되어 더 좁아진다). 신규 호출자만 ``make_key()`` 를 사용한다.

함수도서관: ``docs/함수도서관/backend.md`` §1.11-fg71 (FG 7-1 신설).
"""
from __future__ import annotations

import re
from typing import Optional

from app.config import settings

__all__ = [
    "make_key",
    "make_channel",
    "namespace_prefix",
]


# Valkey 키에 ``:`` 는 segment 구분자. 인자에 포함되면 escape.
_INVALID_KEY_CHARS = re.compile(r"[\s\r\n\x00]")


def _quote_part(value: object) -> str:
    """키 segment 1개를 안전하게 직렬화한다.

    - whitespace / null byte 차단 (R-I4 — 키 조작 방어)
    - 빈 문자열은 명시 placeholder ``_`` 로 대체 (segment 누락 방지)
    """
    s = str(value)
    if _INVALID_KEY_CHARS.search(s):
        # 안전한 대체 — sanitize 후 호출자에게 알릴 수 있도록 명시 marker.
        s = _INVALID_KEY_CHARS.sub("_", s)
    return s if s else "_"


def namespace_prefix() -> str:
    """현재 환경의 키 namespace prefix.

    Returns:
        예: ``mimir:test``, ``mimir:prod``.
    """
    override = settings.valkey_namespace
    if override:
        return override.rstrip(":")
    env = settings.environment or "unknown"
    return f"mimir:{env}"


def make_key(feature: str, *parts: object) -> str:
    """``mimir:<env>:<feature>:<part1>:<part2>...`` 합성.

    Args:
        feature: 기능 식별자 (예: ``viewed``, ``scope_policy``).
        *parts: 키 segment 들. 각 segment 는 문자열로 직렬화된다.

    Returns:
        Valkey 키 문자열. 환경(env)·feature 가 prefix 로 강제된다.

    Raises:
        ValueError: feature 가 비어있거나 부적절한 문자 포함.
    """
    if not feature or _INVALID_KEY_CHARS.search(feature) or ":" in feature:
        raise ValueError(f"invalid feature name: {feature!r}")
    body = ":".join(_quote_part(p) for p in parts)
    if body:
        return f"{namespace_prefix()}:{feature}:{body}"
    return f"{namespace_prefix()}:{feature}"


def make_channel(feature: str, *, org_id: Optional[str] = None) -> str:
    """pub/sub 채널 이름 합성.

    Args:
        feature: 기능 식별자 (예: ``scope_policy``).
        org_id: 테넌트 ID. 지정 시 ``tenant:<org_id>:`` prefix 추가 (R-I3).
            ``None`` 이면 cluster-wide 채널.

    Returns:
        예: ``mimir:prod:cache:invalidate:scope_policy`` (org_id 없음).
        예: ``mimir:prod:tenant:org-123:cache:invalidate:scope_policy``.
    """
    if not feature or _INVALID_KEY_CHARS.search(feature) or ":" in feature:
        raise ValueError(f"invalid feature name: {feature!r}")
    prefix = namespace_prefix()
    if org_id:
        oid = _quote_part(org_id)
        return f"{prefix}:tenant:{oid}:cache:invalidate:{feature}"
    return f"{prefix}:cache:invalidate:{feature}"
