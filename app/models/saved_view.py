"""
SavedView 도메인 모델 — S3 Phase 2 FG 2-5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


SavedViewLayout = Literal["list", "tree", "cards", "graph"]


@dataclass
class SavedView:
    """사용자 저장 뷰 1건.

    Attributes:
        id                : UUID 문자열
        owner_id          : 작성한 사용자 id
        name              : 사용자가 입력한 이름 (1~200 chars, owner 당 UNIQUE)
        filter            : SavedViewFilter Pydantic 모델의 dict
        sort              : SavedViewSort 배열의 dict list
        layout            : "list" | "tree" | "cards" | "graph"
        include_tag_nodes : 그래프 layout 의 메타노드 포함 여부
        created_at        : 생성 시각
        updated_at        : 마지막 수정 시각
    """

    id: str
    owner_id: str
    name: str
    filter: dict[str, Any] = field(default_factory=dict)
    sort: list[dict[str, Any]] = field(default_factory=list)
    layout: SavedViewLayout = "list"
    include_tag_nodes: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
