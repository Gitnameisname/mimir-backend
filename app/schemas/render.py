"""
Render API 응답 Pydantic 스키마.

render_service.render_version()의 출력 구조를 정의한다.

RenderDocument 구조:
  - source_*      : 원본 버전 참조
  - render_mode   : draft | published | version
  - title / summary
  - status_badge  : 뱃지 레이블 + 타입
  - toc           : heading 블록에서 파생된 목차
  - blocks        : 정규화된 본문 블록 트리
  - appendix_blocks : appendix 블록 (본문에서 분리)
  - warnings      : 렌더링 중 수집된 경고 목록
  - unsupported_blocks : 미지원 block_type 목록
"""

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 공통 서브 모델
# ---------------------------------------------------------------------------


class StatusBadge(BaseModel):
    label: str
    type: str


class TocItem(BaseModel):
    block_id: str
    level: int
    text: str
    anchor: str


class RenderWarning(BaseModel):
    level: Literal["warn", "error"]
    message: str


# ---------------------------------------------------------------------------
# RenderBlock — 재귀 구조 (children 포함)
# ---------------------------------------------------------------------------


class RenderBlock(BaseModel):
    """정규화된 렌더링 블록.

    block_type에 따라 존재하는 필드가 다르다:
      heading   : heading_level, content, anchor
      paragraph : content, annotations
      section   : content (title), children
      list / list_item : ordered, content, children
      table     : table_model
      quote     : content
      appendix  : content (title), children
      document  : children
      unsupported : original_type
      error     : content, original_type(optional)
    """

    block_type: str
    block_id: str = ""
    content: Optional[str] = None
    warnings: list[RenderWarning] = Field(default_factory=list)

    # heading
    heading_level: Optional[int] = None
    anchor: Optional[str] = None

    # paragraph
    annotations: Optional[list[Any]] = None

    # list
    ordered: Optional[bool] = None

    # table
    table_model: Optional[dict[str, Any]] = None

    # unsupported / error
    original_type: Optional[str] = None

    # children — 재귀 참조
    children: Optional[list["RenderBlock"]] = None

    model_config = {"extra": "allow"}


RenderBlock.model_rebuild()


# ---------------------------------------------------------------------------
# RenderDocument — render_version() 최종 출력
# ---------------------------------------------------------------------------


class RenderDocument(BaseModel):
    """render_service.render_version()의 완전한 출력 구조."""

    source_document_id: str
    source_version_id: str
    source_version_number: int
    render_mode: Literal["draft", "published", "version"]
    title: str
    summary: Optional[str] = None
    status_badge: StatusBadge
    toc: list[TocItem] = Field(default_factory=list)
    blocks: list[RenderBlock] = Field(default_factory=list)
    appendix_blocks: list[RenderBlock] = Field(default_factory=list)
    warnings: list[RenderWarning] = Field(default_factory=list)
    unsupported_blocks: list[str] = Field(default_factory=list)
