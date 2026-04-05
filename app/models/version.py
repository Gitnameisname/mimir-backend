"""
Version 도메인 모델 (순수 Python dataclass).

document의 특정 시점 스냅샷을 표현한다.
repository가 DB row → Version으로 변환해 반환한다.

책임 분리:
  document = 리소스 정체성/상태/메타
  version  = 특정 시점 구조 스냅샷
  node     = 실제 구조 단위
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Version:
    """문서 버전 도메인 모델.

    Attributes:
        id             : UUID (문자열 표현)
        document_id    : 소속 문서 UUID
        version_number : 문서 내 순차 증가 정수 (1-based)
        label          : 사람이 읽기 좋은 버전 레이블 (optional)
        status         : 버전 상태 (draft / published / archived)
        change_summary : 변경 요약 메시지 (optional)
        source         : 버전 생성 원인 (manual / system / import)
        metadata       : 확장 key-value 구조 (JSONB)
        created_by     : 생성자 actor_id
        created_at     : 생성 시각
    """

    id: str
    document_id: str
    version_number: int
    status: str
    source: str
    metadata: dict[str, Any]
    created_at: datetime
    label: Optional[str] = None
    change_summary: Optional[str] = None
    created_by: Optional[str] = None
