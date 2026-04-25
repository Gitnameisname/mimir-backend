"""시간 유틸 — timezone-aware UTC 타임스탬프 표준 진입점.

본 모듈은 `docs/함수도서관/backend.md` §1.1 B1 에 등록된 공통 유틸이다.

제공 함수:
    - :func:`utcnow` — timezone-aware UTC `datetime` 반환
    - :func:`utcnow_iso` — ISO 8601 UTC 문자열 반환

도입 배경:
    - Python 3.12+ 에서 ``datetime.utcnow()`` 는 deprecated (naive datetime 반환).
    - 프로젝트 전반에서 ``datetime.now(timezone.utc)`` 와 ``datetime.utcnow()`` 가 혼재.
    - 단일 진입점으로 묶어 테스트에서 mock 주입·freeze 가 쉬워진다.
    - `CONSTITUTION.md` 제8조(Single Responsibility), 제10조(Docstring as Agent Contract) 준수.

주의:
    - 두 함수 모두 **timezone-aware** 값을 반환한다 (naive datetime 을 돌려주지 않는다).
    - 이 모듈은 외부 I/O·로깅·감사 기록 등 어떠한 부작용도 갖지 않는다(제9조).
"""
from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utcnow", "utcnow_iso"]


def utcnow() -> datetime:
    """현재 시각을 timezone-aware UTC `datetime` 으로 반환한다.

    Returns:
        datetime: ``tzinfo=timezone.utc`` 가 설정된 aware datetime.

    Examples:
        >>> ts = utcnow()
        >>> ts.tzinfo is not None
        True
        >>> ts.tzinfo == timezone.utc
        True

    Notes:
        - Python 3.12+ 에서 deprecated 인 ``datetime.utcnow()`` 의 대체 진입점.
        - 테스트에서 시간을 고정하고 싶으면 ``freezegun.freeze_time`` 또는
          ``unittest.mock.patch('app.utils.time.utcnow')`` 을 사용한다.
    """
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """현재 UTC 시각을 ISO 8601 문자열로 반환한다.

    Returns:
        str: 예) ``"2026-04-25T09:41:00.123456+00:00"``.

    Examples:
        >>> s = utcnow_iso()
        >>> "+00:00" in s or s.endswith("Z")
        True

    Notes:
        - 내부적으로 :func:`utcnow` 를 호출하므로 timezone-aware 보장 동일.
        - 감사 로그·응답 직렬화에서 사용한다.
    """
    return utcnow().isoformat()
