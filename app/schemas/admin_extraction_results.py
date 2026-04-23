"""
Admin Extraction Results — Pydantic schemas (Phase 8 FG8.2/8.3, scope B).

본 스키마는 `/api/v1/admin/extraction-results` 라우터 전용 외부 계약이다.

프론트엔드(Q1 에서 정의된 계약) ↔ 내부 `extraction_candidates` 테이블 사이의
경계 어댑터 역할을 한다. 내부 상태 어휘(`pending|approved|rejected|modified`)
와 외부 어휘(`pending_review|approved|rejected`)를 명시적으로 분리해 양쪽이
독립적으로 진화할 수 있게 한다.

S2 원칙 준수:
  ① DocumentType 하드코딩 금지 — document_type_code 는 서버 응답값이며
                                 Pydantic 스키마 자체는 특정 코드를 알지 못함
  ⑤ actor_type — 라우터에서 ActorContext 로부터 채움
  ⑥ scope_profile_id — request/response 에 포함, 라우터에서 필터링
  ⑦ 폐쇄망 — 외부 의존 없음 (Pydantic v2)

상태 매핑:
  외부 `pending_review` ↔ 내부 `pending`
  외부 `approved`       ↔ 내부 `approved` | `modified`
  외부 `rejected`       ↔ 내부 `rejected`

`modified`(수정 후 승인) 내부 상태는 외부로 내려갈 때 `approved` 로 normalize
하며, 구분이 필요한 상세 플래그는 별도 필드(`was_modified`) 로 노출한다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Status 어휘 (외부 계약)
# ---------------------------------------------------------------------------

# 외부(프론트/OpenAPI) 에 노출되는 상태 값.
# - `pending_review` 를 `pending` 과 다르게 쓰는 이유: "관리자 검토 대기" 라는
#   도메인 의도를 UI 레이어에서 더 명확히 드러내기 위함.
# - `modified` 는 외부에서 `approved` 와 동일하게 취급 (수정 여부는
#   별도 `was_modified` 플래그로 노출).
ExtractionResultStatusExternal = Literal["pending_review", "approved", "rejected"]


def map_status_to_external(internal: str) -> ExtractionResultStatusExternal:
    """내부 상태(pending|approved|rejected|modified) → 외부 어휘."""
    if internal == "pending":
        return "pending_review"
    if internal in ("approved", "modified"):
        return "approved"
    if internal == "rejected":
        return "rejected"
    # 알 수 없는 값은 안전 기본값으로 취급(500 회피). 감사 로그 측에는
    # 내부 원본 값이 남아 있으므로 데이터 추적 가능.
    return "pending_review"


def map_status_to_internal(external: str) -> str:
    """외부 어휘 → 내부 상태 집합 필터 값.

    반환값은 `WHERE status IN (...)` 에 사용 가능한 단일 내부값.
    `approved` 필터는 내부적으로 `approved` 와 `modified` 둘 다 포함해야
    하므로 이 함수만으로는 완전히 표현할 수 없다. 호출부에서 `approved`
    케이스에 한해 별도 경로(IN 절)로 확장해야 한다.
    """
    if external == "pending_review":
        return "pending"
    if external == "approved":
        return "approved"
    if external == "rejected":
        return "rejected"
    raise ValueError(f"알 수 없는 외부 상태: {external!r}")


# ---------------------------------------------------------------------------
# Response DTOs
# ---------------------------------------------------------------------------


class ExtractionResultSummary(BaseModel):
    """목록(GET /) 응답의 단일 레코드.

    프론트 `ExtractionResult` 인터페이스와 일치.
    """

    id: UUID
    document_id: UUID
    document_title: str = Field(
        ..., description="documents.title (join). 문서 삭제 시 '(삭제됨)' 으로 대체."
    )
    document_type_code: str = Field(
        ...,
        description=(
            "UPPER-SNAKE 형식의 문서 타입 코드. extraction_candidates."
            "extraction_schema_id 와 동일 (doc_type_code 참조)."
        ),
    )
    extracted_at: datetime = Field(..., description="추출 완료 시각 = candidates.created_at")
    status: ExtractionResultStatusExternal
    reviewer_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    # `modified` 내부 상태를 UI 쪽에서 배지로 보여줄 수 있도록 보조 플래그.
    was_modified: bool = False


class ExtractionResultDetail(ExtractionResultSummary):
    """상세(GET /{id}) 응답.

    프론트 `ExtractionResultDetail` 인터페이스와 일치.
    """

    original_content_preview: str = Field(
        default="",
        description=(
            "documents.summary 기반 프리뷰 (최대 2000 자). 요약이 없으면 빈 문자열."
        ),
    )
    extracted_fields: Dict[str, Any] = Field(default_factory=dict)
    field_spans: Dict[str, Optional[str]] = Field(
        default_factory=dict,
        description=(
            "필드별 소스 스팬 앵커(있다면). B 스코프에서는 아직 미지원 — 항상 빈 dict."
        ),
    )


# ---------------------------------------------------------------------------
# Request DTOs
# ---------------------------------------------------------------------------


class AdminApproveExtractionRequest(BaseModel):
    """POST /{id}/approve

    overrides 필드는 선택 사항:
      - 비어 있거나 없으면 전체 원본 extracted_fields 를 그대로 승인.
      - 채워져 있으면 해당 필드들만 덮어쓴 뒤 승인(내부적으로 `modify` 경로).
    """

    approval_comment: Optional[str] = Field(default=None, max_length=1024)
    overrides: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "일부 필드만 덮어쓸 때 사용. None 이거나 빈 dict 면 수정 없이 승인."
        ),
    )

    @field_validator("overrides")
    @classmethod
    def _limit_overrides_size(cls, v: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        # DoS 방어용 필드 개수 상한 (CWE-400).
        if v is not None and len(v) > 200:
            raise ValueError("overrides 필드 수는 최대 200 개")
        return v


class AdminRejectExtractionRequest(BaseModel):
    """POST /{id}/reject"""

    reason: Optional[str] = Field(
        default=None,
        max_length=1024,
        description="반려 사유(선택). 빈 문자열이면 None 으로 정규화.",
    )

    @field_validator("reason", mode="before")
    @classmethod
    def _empty_reason_to_none(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        return v
