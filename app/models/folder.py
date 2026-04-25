"""
Folder 도메인 모델 — S3 Phase 2 FG 2-1.

계층 폴더 (self-referencing). 하나의 문서는 **최대 하나의 폴더** 에 속한다 (N:1).
materialized path (`/root/child/` 형식) 를 저장해 prefix 매칭으로 하위 탐색을
빠르게 한다.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Folder:
    """폴더 도메인 모델.

    Attributes:
        id          : UUID (str)
        owner_id    : 소유자 (users.id). 폴더는 owner 범위 격리
        parent_id   : 부모 폴더 UUID. 루트이면 None
        name        : 폴더 이름
        path        : materialized path (예: '/work/projects/'). 항상 '/' 로
                      시작/종료. 루트도 '/<name>/' 형식
        depth       : 0 이상. 루트=0, 자식=parent.depth+1. 상한 10
        created_at, updated_at
    """

    id: str
    owner_id: str
    parent_id: Optional[str]
    name: str
    path: str
    depth: int
    created_at: datetime
    updated_at: datetime


@dataclass
class DocumentFolder:
    """문서 ↔ 폴더 연결 레코드 (N:1).

    Attributes:
        document_id : 문서 UUID (PK — 한 문서는 한 폴더에만)
        folder_id   : 폴더 UUID
        assigned_at : 배치 시각
    """

    document_id: str
    folder_id: str
    assigned_at: datetime
