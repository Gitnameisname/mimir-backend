"""
Folders API request/response 스키마 — S3 Phase 2 FG 2-1.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class FolderCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    parent_id: Optional[str] = Field(
        default=None,
        description="부모 폴더 UUID. 루트는 null",
    )


class FolderRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class FolderMoveRequest(BaseModel):
    new_parent_id: Optional[str] = Field(
        default=None,
        description="새 부모 폴더 UUID. 루트로 이동하려면 null",
    )


class FolderResponse(BaseModel):
    id: str
    owner_id: str
    parent_id: Optional[str] = None
    name: str
    path: str = Field(description="materialized path (예: /work/projects/)")
    depth: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SetDocumentFolderRequest(BaseModel):
    folder_id: Optional[str] = Field(
        default=None,
        description="지정할 폴더 UUID. null 이면 폴더 해제",
    )
