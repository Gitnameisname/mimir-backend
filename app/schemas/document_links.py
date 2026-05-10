"""
Schemas for Document Links (Wikilinks) — S3 Phase 2 FG 2-3.

본문 ``[[문서명]]`` 토큰의 양방향 그래프 에지에 대한 응답 스키마.
``app.api.v1.document_links`` 라우터의 응답 모델 정본.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class BacklinkItem(BaseModel):
    """``GET /documents/{id}/backlinks`` 응답 항목 — 이 문서를 참조하는 출발 문서."""

    link_id: str = Field(..., description="document_links.id")
    from_document_id: str = Field(..., description="참조한 출발 문서 id")
    from_document_title: str = Field(..., description="출발 문서의 제목")
    node_id: str = Field(..., description="출발 문서의 anchor block node_id")
    raw_text: str = Field(..., description="본문에 입력된 원문 (alias 제외)")
    created_at: datetime


class OutgoingLinkItem(BaseModel):
    """``GET /documents/{id}/links`` 응답 항목 — 정방향 (admin/디버깅용)."""

    id: str = Field(..., description="document_links.id")
    to_document_id: Optional[str] = Field(
        None, description="resolved 일 때만 채워짐"
    )
    node_id: str
    raw_text: str
    resolved_status: Literal["resolved", "ambiguous", "missing"]
    created_at: datetime


class ResolveItem(BaseModel):
    """``GET /documents/resolve`` 응답 항목 — TipTap WikiLinkMark 자동완성."""

    id: str = Field(..., description="document_id")
    title: str
    updated_at: datetime
