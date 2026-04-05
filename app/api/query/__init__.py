"""
List query parsing and validation package.

공통 list query parser / validator 계층.
pagination / sort / filter 규약을 공통화하고,
허용 필드는 resource별 ListQuerySpec으로 분리한다.

사용 예:
    from app.api.query import make_list_query_dependency, ListQuerySpec, FilterFieldSpec

    DOCUMENTS_SPEC = ListQuerySpec(
        allowed_sort_fields=["created_at", "updated_at", "title", "status"],
        allowed_filter_fields=[
            FilterFieldSpec(name="status"),
            FilterFieldSpec(name="document_type"),
        ],
    )

    @router.get("")
    def list_documents(
        query: ParsedListQuery = Depends(make_list_query_dependency(DOCUMENTS_SPEC)),
    ): ...
"""

from app.api.query.dependencies import make_list_query_dependency
from app.api.query.models import FilterFieldSpec, ListQuerySpec, ParsedListQuery, SortOrder

__all__ = [
    "FilterFieldSpec",
    "ListQuerySpec",
    "ParsedListQuery",
    "SortOrder",
    "make_list_query_dependency",
]
