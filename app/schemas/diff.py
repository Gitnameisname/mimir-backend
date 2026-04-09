"""
Diff API 요청/응답 Pydantic 스키마.

Phase 9: 변경 비교 및 이력 가시화 기능 구축

엔티티:
  - InlineDiffToken  : 텍스트 수준 인라인 diff 토큰
  - MoveInfo         : 노드 이동 정보
  - NodeSnapshot     : diff 비교에 사용하는 노드 스냅샷
  - NodeDiff         : 단일 노드 변경 결과
  - ChangedSection   : 변경된 최상위 섹션 요약
  - DiffSummary      : 전체 변경 요약
  - DiffResult       : 버전 비교 전체 응답
  - DiffSummaryResponse: 경량 변경 요약 응답
"""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ChangeType(str, Enum):
    ADDED = "ADDED"
    DELETED = "DELETED"
    MODIFIED = "MODIFIED"
    MOVED = "MOVED"
    UNCHANGED = "UNCHANGED"


class MoveType(str, Enum):
    HIERARCHY_CHANGE = "HIERARCHY_CHANGE"  # parent_id 변경
    REORDER = "REORDER"                    # 동일 parent 내 순서 변경


class DiffSeverity(str, Enum):
    MAJOR = "MAJOR"      # 30% 이상 변경
    MINOR = "MINOR"      # 10~30% 변경
    TRIVIAL = "TRIVIAL"  # 10% 미만 변경


# ---------------------------------------------------------------------------
# 토큰 / 스냅샷
# ---------------------------------------------------------------------------


class InlineDiffToken(BaseModel):
    """텍스트 수준 인라인 diff 토큰."""
    type: str  # "added" | "deleted" | "unchanged"
    text: str


class MoveInfo(BaseModel):
    """노드 이동 정보."""
    old_parent_id: Optional[str] = None
    new_parent_id: Optional[str] = None
    old_order: int
    new_order: int
    move_type: MoveType


class NodeSnapshot(BaseModel):
    """diff 결과에 포함할 노드 상태 스냅샷."""
    node_id: str
    node_type: str
    title: Optional[str] = None
    content: Optional[str] = None
    parent_id: Optional[str] = None
    order: int
    metadata: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# NodeDiff
# ---------------------------------------------------------------------------


class NodeDiff(BaseModel):
    """단일 노드 변경 결과."""
    node_id: str
    change_type: ChangeType
    before: Optional[NodeSnapshot] = None
    after: Optional[NodeSnapshot] = None
    inline_diff: Optional[list[InlineDiffToken]] = None
    inline_diff_skipped: bool = False
    move_info: Optional[MoveInfo] = None


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


class ChangedSection(BaseModel):
    """변경된 최상위 섹션 요약."""
    node_id: str
    title: Optional[str] = None
    change_type: ChangeType
    sub_changes: int = 0


class DiffSummary(BaseModel):
    """전체 변경 요약."""
    total_added: int = 0
    total_deleted: int = 0
    total_modified: int = 0
    total_moved: int = 0
    total_unchanged: int = 0
    changed_characters: int = 0
    description: str = "변경 사항 없음"
    severity: Optional[DiffSeverity] = None
    changed_sections: list[ChangedSection] = []


# ---------------------------------------------------------------------------
# 버전 참조
# ---------------------------------------------------------------------------


class VersionRef(BaseModel):
    """버전 참조 정보 (diff 응답에 포함)."""
    id: str
    version_number: int
    status: str
    created_at: str
    created_by: Optional[str] = None
    label: Optional[str] = None
    change_summary: Optional[str] = None


# ---------------------------------------------------------------------------
# DiffResult (전체 응답)
# ---------------------------------------------------------------------------


class DiffResult(BaseModel):
    """두 버전 간 diff 전체 응답."""
    document_id: str
    version_a: VersionRef
    version_b: VersionRef
    summary: DiffSummary
    nodes: list[NodeDiff]
    has_data_issue: bool = False


# ---------------------------------------------------------------------------
# DiffSummaryResponse (경량 응답 — /diff/summary 엔드포인트)
# ---------------------------------------------------------------------------


class DiffSummaryResponse(BaseModel):
    """변경 요약만 포함하는 경량 응답."""
    document_id: str
    version_a: VersionRef
    version_b: VersionRef
    summary: DiffSummary
