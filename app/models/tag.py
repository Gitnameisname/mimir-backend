"""
Tag 도메인 모델 — S3 Phase 2 FG 2-2.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Tag:
    """태그 전역 풀 엔트리.

    Attributes:
        id              : UUID 문자열
        name_normalized : NFKC + lower + [\\w/-]{1,64} 정규화된 이름
        created_at      : 생성 시각
        usage_count     : 이 태그가 붙은 문서 수 (popular API 용, 선택)
    """

    id: str
    name_normalized: str
    created_at: datetime
    usage_count: Optional[int] = None


@dataclass
class DocumentTag:
    """문서 ↔ 태그 연결 레코드.

    source ∈ {'inline', 'frontmatter', 'both'}
    """

    document_id: str
    tag_id: str
    source: str
    created_at: datetime
