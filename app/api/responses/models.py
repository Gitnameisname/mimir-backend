"""
공통 성공 응답 envelope 모델.

모든 성공 응답의 최상위 계약을 정의한다:
  - SuccessResponse[T]  : 단건 응답
  - ListMeta / SuccessResponse[list[T]] : 목록 응답 (ListMeta 사용)
  - AcceptedResponse    : 202 Accepted 비동기 응답 초안

설계 원칙:
  - 최상위 필드는 data / meta 두 가지만
  - meta는 request_id / trace_id 를 즉시 수용하고, 이후 확장을 위한 공간으로 유지
  - message / status / code 남발 금지
  - Generic[T] 기반으로 OpenAPI 스키마 자동 생성 지원
"""

from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, field_serializer

T = TypeVar("T")


class ResponseMeta(BaseModel):
    """모든 응답에 공통으로 실리는 메타 정보.

    현재 필드:
      - request_id : 요청 추적 식별자 (Task I-4에서 자동 주입 예정)
      - trace_id   : 분산 트레이싱 식별자 (Task I-4에서 자동 주입 예정)

    향후 확장 예정 위치:
      - pagination  (Task I-6)
      - links
      - operation   (Task I-11)
      - warnings
      - idempotency (Task I-9)
    """

    model_config = ConfigDict(extra="allow")

    request_id: Optional[str] = None
    trace_id: Optional[str] = None


class PaginationMeta(BaseModel):
    """목록 응답의 페이지네이션 정보.

    page 기반과 cursor 기반 모두를 수용할 수 있는 구조.

    Attributes:
        page       : 현재 페이지 번호 (page 기반)
        page_size  : 페이지당 항목 수
        total      : 전체 항목 수 (optional — 비용이 높을 수 있으므로 서비스 재량)
        has_next   : 다음 페이지 존재 여부 (optional)
        next_cursor: 다음 cursor 값 (cursor 기반 확장 슬롯 — 현재 미구현)
    """

    page: int
    page_size: int
    total: Optional[int] = None
    has_next: Optional[bool] = None
    next_cursor: Optional[str] = None


class ListMeta(ResponseMeta):
    """목록 응답에 특화된 meta — pagination을 추가로 수용한다."""

    pagination: Optional[PaginationMeta] = None


class SuccessResponse(BaseModel, Generic[T]):
    """단건 성공 응답 envelope.

    사용 예:
        SuccessResponse[DocumentSchema]
        SuccessResponse[dict]

    meta 필드는 ResponseMeta로 선언되어 있지만, ListMeta 등 서브클래스를
    런타임 타입 기준으로 직렬화한다. 따라서 pagination 등 서브클래스 필드가
    응답에 올바르게 포함된다.
    """

    data: T
    meta: ResponseMeta = ResponseMeta()

    @field_serializer("meta", mode="plain")
    def _serialize_meta(self, v: ResponseMeta) -> dict:
        # 런타임 타입(ListMeta 등 서브클래스)의 실제 필드를 모두 직렬화
        return v.model_dump()


class AcceptedData(BaseModel):
    """202 Accepted 응답의 data 페이로드 초안.

    Task I-11에서 operation 리소스 모델로 확장 예정.
    현재는 최소한의 비동기 수락 정보만 포함한다.
    """

    status: str = "accepted"
    operation_id: Optional[str] = None
    resource: Optional[str] = None


class AcceptedResponse(BaseModel):
    """202 Accepted 비동기 응답 envelope 초안."""

    data: AcceptedData
    meta: ResponseMeta = ResponseMeta()
