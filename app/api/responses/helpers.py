"""
공통 성공 응답 helper 함수.

라우터에서 응답 envelope를 직접 조립하지 않고,
이 helper를 통해 일관된 계약으로 응답을 반환하도록 한다.

사용 예:
    return success_response(data=payload, request_id=req_id)
    return list_response(data=items, total=100, page=1, page_size=20)
    return accepted_response(operation_id="op_123", resource="documents")
"""

from typing import Any, Optional

from app.api.query import ParsedListQuery
from app.api.responses.models import (
    AcceptedData,
    AcceptedResponse,
    ListMeta,
    PaginationMeta,
    ResponseMeta,
    SuccessResponse,
)


def success_response(
    data: Any,
    *,
    request_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> SuccessResponse:
    """단건 또는 일반 성공 응답을 만든다.

    Args:
        data: 응답 payload (단건 리소스, dict, 또는 Pydantic 모델)
        request_id: 요청 추적 ID (Task I-4에서 request context로 자동 주입 예정)
        trace_id: 분산 트레이싱 ID (Task I-4에서 자동 주입 예정)

    Returns:
        { "data": ..., "meta": { "request_id": ..., "trace_id": ... } }
    """
    meta = ResponseMeta(request_id=request_id, trace_id=trace_id)
    return SuccessResponse(data=data, meta=meta)


def list_response(
    data: list,
    *,
    request_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    total: Optional[int] = None,
    has_next: Optional[bool] = None,
    next_cursor: Optional[str] = None,
) -> SuccessResponse:
    """목록 응답을 만든다.

    page와 page_size가 제공되면 meta.pagination이 포함된다.
    total / has_next / next_cursor는 선택적으로 추가할 수 있다.

    Args:
        data: 응답 항목 배열
        request_id: 요청 추적 ID
        trace_id: 분산 트레이싱 ID
        page: 현재 페이지 번호
        page_size: 페이지당 항목 수
        total: 전체 항목 수 (optional — 비용이 높을 경우 생략 가능)
        has_next: 다음 페이지 존재 여부 (optional)
        next_cursor: 다음 cursor 값 (cursor 기반 확장 슬롯, optional)

    Returns:
        { "data": [...], "meta": { ..., "pagination": { ... } } }
    """
    pagination: Optional[PaginationMeta] = None
    if page is not None and page_size is not None:
        pagination = PaginationMeta(
            page=page,
            page_size=page_size,
            total=total,
            has_next=has_next,
            next_cursor=next_cursor,
        )

    meta = ListMeta(request_id=request_id, trace_id=trace_id, pagination=pagination)
    return SuccessResponse(data=data, meta=meta)


def paginated_list_response(
    data: list,
    *,
    query: ParsedListQuery,
    total: int,
    request_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> SuccessResponse:
    """ParsedListQuery 기반 목록 응답을 만든다.

    라우터에서 반복되는 page/page_size 계산 + list_response 조합을 캡슐화한다.

    Args:
        data: 응답 항목 배열
        query: make_list_query_dependency()가 반환한 ParsedListQuery
        total: 전체 항목 수
        request_id: 요청 추적 ID
        trace_id: 분산 트레이싱 ID
    """
    page = query.page or 1
    page_size = query.page_size or 20
    has_next = (page * page_size) < total
    return list_response(
        data=data,
        request_id=request_id,
        trace_id=trace_id,
        page=page,
        page_size=page_size,
        total=total,
        has_next=has_next,
    )


def accepted_response(
    *,
    operation_id: Optional[str] = None,
    resource: Optional[str] = None,
    request_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> AcceptedResponse:
    """202 Accepted 비동기 응답을 만든다.

    비동기 작업 시작 시 일관된 수락 응답을 반환하기 위한 helper 초안.
    실제 operation 리소스 모델은 Task I-11에서 확장 예정.

    Args:
        operation_id: 생성된 비동기 작업 ID (아직 미구현이면 None)
        resource: 대상 리소스 타입 (예: "documents")
        request_id: 요청 추적 ID
        trace_id: 분산 트레이싱 ID

    Returns:
        { "data": { "status": "accepted", ... }, "meta": { ... } }
    """
    data = AcceptedData(operation_id=operation_id, resource=resource)
    meta = ResponseMeta(request_id=request_id, trace_id=trace_id)
    return AcceptedResponse(data=data, meta=meta)
