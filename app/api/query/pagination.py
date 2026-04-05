"""
Pagination parser / validator.

정책:
  - 기본 모드: page + page_size
  - 확장 슬롯: cursor + limit
  - page/page_size와 cursor/limit 동시 사용 금지
  - page  : 기본값 1, 최솟값 1
  - page_size: 기본값 spec.default_page_size, 최댓값 spec.max_page_size
  - limit : 최솟값 1, 최댓값 spec.max_limit
  - invalid 입력 → ApiValidationError (validation_error)
"""

from dataclasses import dataclass
from typing import Literal, Optional

from app.api.errors.exceptions import ApiValidationError
from app.api.query.models import ListQuerySpec


@dataclass
class PaginationResult:
    mode: Literal["page", "cursor"]
    page: int
    page_size: int
    cursor: Optional[str]
    limit: Optional[int]


def parse_pagination(
    page: Optional[int],
    page_size: Optional[int],
    cursor: Optional[str],
    limit: Optional[int],
    spec: ListQuerySpec,
) -> PaginationResult:
    """pagination 파라미터를 검증하고 정규화한다.

    Args:
        page      : 요청된 페이지 번호 (page 기반)
        page_size : 요청된 페이지 크기 (page 기반)
        cursor    : 요청된 cursor 문자열 (cursor 기반)
        limit     : 요청된 항목 수 (cursor 기반)
        spec      : resource별 ListQuerySpec

    Returns:
        PaginationResult

    Raises:
        ApiValidationError: invalid 또는 혼합 입력
    """
    _has_page_params = page is not None or page_size is not None
    _has_cursor_params = cursor is not None or limit is not None

    # 혼합 금지
    if _has_page_params and _has_cursor_params:
        raise ApiValidationError(
            "Cannot mix page/page_size and cursor/limit pagination",
            details=[
                {
                    "field": "pagination",
                    "reason": "page/page_size and cursor/limit are mutually exclusive",
                    "category": "invalid_pagination",
                }
            ],
        )

    if _has_cursor_params:
        return _parse_cursor_pagination(cursor, limit, spec)

    return _parse_page_pagination(page, page_size, spec)


def _parse_page_pagination(
    page: Optional[int],
    page_size: Optional[int],
    spec: ListQuerySpec,
) -> PaginationResult:
    """page 기반 pagination 검증."""
    errors = []

    resolved_page = page if page is not None else 1
    resolved_page_size = page_size if page_size is not None else spec.default_page_size

    if resolved_page < 1:
        errors.append(
            {
                "field": "page",
                "reason": "must be >= 1",
                "category": "invalid_pagination",
            }
        )

    if resolved_page_size < 1:
        errors.append(
            {
                "field": "page_size",
                "reason": "must be >= 1",
                "category": "invalid_pagination",
            }
        )
    elif resolved_page_size > spec.max_page_size:
        errors.append(
            {
                "field": "page_size",
                "reason": f"must be <= {spec.max_page_size}",
                "category": "invalid_pagination",
            }
        )

    if errors:
        raise ApiValidationError("Invalid pagination parameters", details=errors)

    return PaginationResult(
        mode="page",
        page=resolved_page,
        page_size=resolved_page_size,
        cursor=None,
        limit=None,
    )


def _parse_cursor_pagination(
    cursor: Optional[str],
    limit: Optional[int],
    spec: ListQuerySpec,
) -> PaginationResult:
    """cursor 기반 pagination 검증 (확장 슬롯 — 현재 decode 미구현).

    cursor 자체 해석은 하지 않지만, 입력 contract와 limit 범위를 검증한다.
    TODO: cursor decode/encode 실제 구현 예정
    """
    errors = []

    if cursor is not None:
        _validate_cursor_format(cursor, errors)

    resolved_limit = limit if limit is not None else spec.default_page_size

    if resolved_limit < 1:
        errors.append(
            {
                "field": "limit",
                "reason": "must be >= 1",
                "category": "invalid_pagination",
            }
        )
    elif resolved_limit > spec.max_limit:
        errors.append(
            {
                "field": "limit",
                "reason": f"must be <= {spec.max_limit}",
                "category": "invalid_pagination",
            }
        )

    if errors:
        raise ApiValidationError("Invalid cursor pagination parameters", details=errors)

    return PaginationResult(
        mode="cursor",
        page=1,
        page_size=resolved_limit,
        cursor=cursor,
        limit=resolved_limit,
    )


def _validate_cursor_format(cursor: str, errors: list) -> None:
    """cursor 문자열 기본 형식 검증.

    현재는 빈 문자열만 거부한다.
    TODO: 실제 cursor 인코딩 방식 결정 후 검증 로직 확장 예정
    """
    if not cursor or not cursor.strip():
        errors.append(
            {
                "field": "cursor",
                "reason": "cursor must not be empty",
                "category": "invalid_pagination",
            }
        )
