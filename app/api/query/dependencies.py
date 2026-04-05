"""
List query FastAPI dependency factory.

사용 예:
    from app.api.query import make_list_query_dependency, ListQuerySpec, FilterFieldSpec

    DOCUMENTS_SPEC = ListQuerySpec(
        allowed_sort_fields=["created_at", "updated_at", "title", "status"],
        allowed_filter_fields=[
            FilterFieldSpec(name="status", allowed_values=["draft", "published", "archived"]),
            FilterFieldSpec(name="document_type"),
            FilterFieldSpec(name="owner_id", type="uuid"),
        ],
    )

    @router.get("")
    def list_documents(
        query: ParsedListQuery = Depends(make_list_query_dependency(DOCUMENTS_SPEC)),
    ):
        ...

설계:
  - make_list_query_dependency(spec)는 FastAPI dependency 함수를 반환한다.
  - pagination / sort 파라미터는 FastAPI Query로 명시 선언 → OpenAPI 문서에 노출.
  - filter 파라미터는 Request.query_params에서 추출 → resource별 허용 필드로 검증.
  - 모든 validation 오류는 ApiValidationError → global handler → 400 validation_error.
"""

from typing import Callable, Optional

from fastapi import Query, Request

from app.api.query.filtering import parse_filters
from app.api.query.models import ListQuerySpec, ParsedListQuery
from app.api.query.pagination import parse_pagination
from app.api.query.sorting import parse_sort


def make_list_query_dependency(spec: ListQuerySpec) -> Callable[..., ParsedListQuery]:
    """ListQuerySpec을 주입받아 FastAPI dependency 함수를 반환하는 factory.

    반환된 함수는 FastAPI Depends()로 사용한다.
    pagination, sort 파라미터가 OpenAPI 문서에 자동으로 노출된다.

    Args:
        spec: resource별 ListQuerySpec

    Returns:
        FastAPI dependency callable → ParsedListQuery
    """

    async def _parse_list_query(
        request: Request,
        page: Optional[int] = Query(
            None,
            description="Page number (1-based). Cannot be combined with cursor.",
            examples=[1],
        ),
        page_size: Optional[int] = Query(
            None,
            description=(
                f"Number of items per page. "
                f"Default: {spec.default_page_size}. "
                f"Max: {spec.max_page_size}."
            ),
            examples=[spec.default_page_size],
        ),
        cursor: Optional[str] = Query(
            None,
            description=(
                "Opaque cursor for cursor-based pagination. "
                "Cannot be combined with page/page_size. "
                "[Reserved for future use]"
            ),
        ),
        limit: Optional[int] = Query(
            None,
            description=(
                f"Number of items per cursor page. "
                f"Max: {spec.max_limit}. "
                "[Reserved for future use]"
            ),
        ),
        sort: Optional[str] = Query(
            None,
            description=(
                "Comma-separated sort fields. "
                "Prefix with '-' for descending. "
                f"Allowed: {spec.allowed_sort_fields or 'any'}. "
                "Example: 'created_at,-updated_at'."
            ),
            examples=["created_at"] if spec.allowed_sort_fields else None,
        ),
    ) -> ParsedListQuery:
        """공통 list query 파싱 및 검증 dependency."""
        # 1. pagination
        pagination = parse_pagination(page, page_size, cursor, limit, spec)

        # 2. sort
        sort_orders = parse_sort(sort, spec) if sort else []

        # 3. filters (Request.query_params에서 허용 필드만 추출)
        filters = parse_filters(dict(request.query_params), spec)

        return ParsedListQuery(
            pagination_mode=pagination.mode,
            page=pagination.page,
            page_size=pagination.page_size,
            cursor=pagination.cursor,
            limit=pagination.limit,
            sort_orders=sort_orders,
            filters=filters,
        )

    return _parse_list_query
