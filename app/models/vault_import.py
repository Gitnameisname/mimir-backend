"""
VaultImport 도메인 모델 — S3 Phase 2 FG 2-6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional


VaultImportStatus = Literal[
    "pending", "running", "succeeded", "failed", "cancelled",
]


@dataclass
class VaultImport:
    id: str
    owner_id: str
    uploaded_filename: str
    bytes_original: int
    bytes_extracted: int
    file_count: int
    status: VaultImportStatus
    scope_profile_id: str
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    report: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
