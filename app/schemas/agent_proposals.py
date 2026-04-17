"""
FG5.1 — 에이전트 Draft 제안 / 워크플로 전이 제안 스키마.

포함 스키마:
  - ProposeDraftRequest / ProposeDraftResponse
  - ProposeTransitionRequest / ProposeTransitionResponse
  - ApproveDraftRequest / ApproveDraftResponse
  - RejectDraftRequest / RejectDraftResponse
  - WithdrawProposalRequest / WithdrawProposalResponse
  - McpTaskResponse
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Propose Draft
# ---------------------------------------------------------------------------

class ProposeDraftRequest(BaseModel):
    document_id: Optional[str] = Field(
        None,
        description="기존 문서 UUID. null이면 새 문서 생성.",
    )
    document_type_id: Optional[str] = Field(
        None,
        description="새 문서 생성 시 필수. 기존 문서 수정 시 무시.",
    )
    title: Optional[str] = Field(None, description="문서 제목 (선택)")
    content: str = Field(..., description="Draft 본문 (마크다운 또는 plain text)")
    metadata: dict[str, Any] = Field(default_factory=dict, description="추가 메타데이터")
    reason: str = Field(..., description="제안 사유 (감사 로그에 기록됨)")


class ProposeDraftResponse(BaseModel):
    draft_id: str
    status: str = "proposed"
    created_by_agent: bool = True
    created_at: datetime
    document_id: str
    version_id: str
    proposal_url: Optional[str] = None
    mcp_task_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Propose Transition
# ---------------------------------------------------------------------------

class ProposeTransitionRequest(BaseModel):
    document_id: str = Field(..., description="상태 전이를 제안할 문서 UUID")
    target_state: str = Field(
        ...,
        description="전이 목표 상태 (예: 'review', 'approved', 'published')",
    )
    reason: str = Field(..., description="전이 사유")
    approver_notes: Optional[str] = Field(
        None,
        description="승인자에게 남길 메모",
    )


class ProposeTransitionResponse(BaseModel):
    transition_proposal_id: str
    document_id: str
    current_state: str
    proposed_state: str
    status: str = "pending_approval"
    created_at: datetime
    mcp_task_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Approve Draft (인간 작업)
# ---------------------------------------------------------------------------

class ApproveDraftRequest(BaseModel):
    notes: Optional[str] = Field(None, description="승인 노트")


class ApproveDraftResponse(BaseModel):
    draft_id: str
    document_id: str
    previous_status: str
    new_status: str
    reviewed_by: Optional[str]
    reviewed_at: datetime


# ---------------------------------------------------------------------------
# Reject Draft (인간 작업)
# ---------------------------------------------------------------------------

class RejectDraftRequest(BaseModel):
    reason: str = Field(..., description="반려 사유")


class RejectDraftResponse(BaseModel):
    draft_id: str
    document_id: str
    previous_status: str
    new_status: str
    reviewed_by: Optional[str]
    reviewed_at: datetime
    reason: str


# ---------------------------------------------------------------------------
# Withdraw Proposal (에이전트 회수)
# ---------------------------------------------------------------------------

class WithdrawProposalRequest(BaseModel):
    reason: Optional[str] = Field(None, description="회수 사유")


class WithdrawProposalResponse(BaseModel):
    proposal_id: str
    draft_id: Optional[str]
    previous_status: str
    new_status: str = "withdrawn"
    withdrawn_at: datetime


# ---------------------------------------------------------------------------
# MCP Task
# ---------------------------------------------------------------------------

class McpTaskResponse(BaseModel):
    task_id: str
    title: str
    state: str
    reference_type: Optional[str]
    reference_id: Optional[str]
    created_at: datetime
