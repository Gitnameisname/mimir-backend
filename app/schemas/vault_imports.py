"""
Schemas for Vault Imports — S3 Phase 2 FG 2-6.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


VaultImportStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class VaultImportResponse(BaseModel):
    id: str
    uploaded_filename: str
    bytes_original: int
    bytes_extracted: int
    file_count: int
    status: VaultImportStatus
    scope_profile_id: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    report: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
