"""
공통 list query 모델.

설계 원칙:
  - ParsedListQuery  : parser 결과 — 서비스 레이어에 전달하는 정규화된 query
  - SortOrder        : 정렬 필드 + 방향 단위
  - FilterFieldSpec  : resource별 허용 filter 필드 명세
  - ListQuerySpec    : resource별 허용 sort/filter spec — router에서 주입

pagination 모드 정책:
  - page 기반 (기본): page + page_size
  - cursor 기반 (확장 슬롯): cursor + limit
  - 두 모드를 동시에 보내면 validation_error
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class SortOrder(BaseModel):
    """단일 정렬 조건 — field + direction."""

    field: str
    direction: Literal["asc", "desc"]


class FilterFieldSpec(BaseModel):
    """resource별 허용 filter 필드 명세.

    Attributes:
        name           : query param 이름 (예: "status")
        type           : 값 타입 힌트 (str / int / bool / uuid). 기본 str.
        allowed_values : 허용 값 목록. None이면 모든 값 허용.
    """

    name: str
    type: Literal["str", "int", "bool", "uuid"] = "str"
    allowed_values: Optional[list[str]] = None


class ListQuerySpec(BaseModel):
    """resource별 list query 허용 명세.

    router에서 생성해 make_list_query_dependency에 주입한다.

    Attributes:
        allowed_sort_fields   : 허용 sort 필드 목록
        allowed_filter_fields : 허용 filter 필드 명세 목록
        default_page_size     : page_size 미지정 시 기본값 (기본 20)
        max_page_size         : page_size 최대 허용값 (기본 100)
        max_limit             : cursor 기반 limit 최대 허용값 (기본 100)
        allow_multi_sort      : 다중 sort 허용 여부 (기본 True)
    """

    allowed_sort_fields: list[str] = Field(default_factory=list)
    allowed_filter_fields: list[FilterFieldSpec] = Field(default_factory=list)
    default_page_size: int = 20
    max_page_size: int = 100
    max_limit: int = 100
    allow_multi_sort: bool = True


class ParsedListQuery(BaseModel):
    """parser 결과 — 서비스 레이어에 전달하는 정규화된 list query.

    pagination_mode:
      - "page"   : page + page_size 기반 (현재 기본)
      - "cursor" : cursor + limit 기반 (확장 슬롯 — 현재 미구현)
    """

    pagination_mode: Literal["page", "cursor"] = "page"

    # page 기반
    page: int = 1
    page_size: int = 20

    # cursor 기반 (확장 슬롯)
    cursor: Optional[str] = None
    limit: Optional[int] = None

    # 정렬
    sort_orders: list[SortOrder] = Field(default_factory=list)

    # 필터 (key → parsed value)
    filters: dict[str, Any] = Field(default_factory=dict)
