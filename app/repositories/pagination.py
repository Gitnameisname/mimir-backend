"""페이지네이션 가드 유틸 — limit/offset 정규화.

본 모듈은 ``docs/함수도서관/backend.md`` §1.9 BE-G5 에 등록된 공통 유틸이다.

제공 함수:
    - :func:`clamp_pagination` — ``limit``/``offset`` 을 안전한 범위로 clamp.

도입 배경:
    - 라우터·리포지토리에 산재한 ``max(1, min(limit, MAX))`` 패턴.
    - ``None`` 입력 + 음수 보호 + 상한 cap 을 단일 진입점에서 일관되게.

비대상 (intentional non-goals):
    - **page → offset 변환** (``offset = (page - 1) * page_size``) — 별 helper
      후보 (`paginate_page`) 로 후속. 본 라운드는 limit/offset 정규화만.
    - 상한이 도메인별로 다양하므로 (50/100/200/300 등) 호출자가 ``max_limit``
      를 명시해 도메인 정책을 유지.
"""
from __future__ import annotations

__all__ = ["clamp_pagination", "paginate_page"]


def clamp_pagination(
    limit: int | None,
    offset: int | None,
    *,
    max_limit: int = 200,
    default_limit: int = 50,
) -> tuple[int, int]:
    """``limit`` / ``offset`` 을 안전한 범위로 clamp 한다.

    규칙:
        - ``limit is None`` → ``default_limit``
        - ``limit < 1`` → ``1`` (음수 / 0 보호 — 빈 결과 의도가 아닌 한)
        - ``limit > max_limit`` → ``max_limit`` (DoS 방어)
        - ``offset is None`` → ``0``
        - ``offset < 0`` → ``0``

    :param limit: 클라이언트 요청 limit. ``None`` 가능.
    :param offset: 클라이언트 요청 offset. ``None`` 가능.
    :param max_limit: 상한값 (도메인별 정책). 기본 200.
    :param default_limit: ``limit is None`` 일 때 fallback. 기본 50.
    :returns: ``(clamped_limit, clamped_offset)``.

    >>> clamp_pagination(None, None)
    (50, 0)
    >>> clamp_pagination(0, -5)
    (1, 0)
    >>> clamp_pagination(99999, 100, max_limit=200)
    (200, 100)
    >>> clamp_pagination(50, 0)
    (50, 0)
    >>> clamp_pagination(50, 0, max_limit=100, default_limit=20)
    (50, 0)
    """
    eff_limit = limit if limit is not None else default_limit
    if eff_limit < 1:
        eff_limit = 1
    if eff_limit > max_limit:
        eff_limit = max_limit

    eff_offset = offset if offset is not None else 0
    if eff_offset < 0:
        eff_offset = 0

    return eff_limit, eff_offset


def paginate_page(
    page: int | None,
    page_size: int | None,
    *,
    max_page_size: int = 200,
    default_page_size: int = 50,
) -> tuple[int, int, int]:
    """``page`` (1-base) + ``page_size`` 를 안전한 범위로 clamp 하고 ``offset`` 도 계산.

    `clamp_pagination` 위에서 동작 — page → offset 변환 (`(page - 1) * page_size`) 을
    흡수해 라우터·리포지토리에 산재한 동일 계산 (10+ 사이트) 을 단일 호출로.

    규칙:
        - ``page is None`` → ``1``
        - ``page < 1`` → ``1``
        - ``page_size`` clamp 는 `clamp_pagination` 위임 (음수→1, 초과→max, None→default).
        - ``offset = (page - 1) * page_size``

    :param page: 1-base 페이지 번호 (None 가능).
    :param page_size: 페이지당 항목 수 (None 가능).
    :param max_page_size: 페이지당 상한 (도메인별 정책). 기본 200.
    :param default_page_size: ``page_size is None`` 일 때 fallback. 기본 50.
    :returns: ``(page, page_size, offset)`` — 모두 정수, 비음수.

    >>> paginate_page(1, 50)
    (1, 50, 0)
    >>> paginate_page(3, 20)
    (3, 20, 40)
    >>> paginate_page(None, None)
    (1, 50, 0)
    >>> paginate_page(0, -10)
    (1, 1, 0)
    >>> paginate_page(5, 99999, max_page_size=100)
    (5, 100, 400)
    """
    eff_page_size, _ = clamp_pagination(
        page_size, 0, max_limit=max_page_size, default_limit=default_page_size
    )
    eff_page = page if page is not None else 1
    if eff_page < 1:
        eff_page = 1
    offset = (eff_page - 1) * eff_page_size
    return eff_page, eff_page_size, offset
