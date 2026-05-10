"""
DocumentLink 도메인 모델 — S3 Phase 2 FG 2-3.

본문 ``[[문서명]]`` 토큰의 양방향 그래프 에지 1건을 표현한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional


ResolvedStatus = Literal["resolved", "ambiguous", "missing"]


@dataclass
class DocumentLink:
    """문서 간 wikilink 에지.

    Attributes:
        id               : UUID 문자열
        from_document_id : 출발 문서 (NOT NULL)
        to_document_id   : 도착 문서 — resolved 일 때만 채워짐. ambiguous / missing 은 None
        node_id          : 출발 문서의 block node_id (앵커)
        raw_text         : 본문 ``[[원문]]`` 의 원문 (alias 제외, NFC 미적용)
        resolved_status  : ``'resolved' | 'ambiguous' | 'missing'``
        created_at       : 생성 시각
    """

    id: str
    from_document_id: str
    to_document_id: Optional[str]
    node_id: str
    raw_text: str
    resolved_status: ResolvedStatus
    created_at: datetime
