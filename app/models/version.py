"""
Version 도메인 모델 (순수 Python dataclass).

document의 특정 시점 스냅샷을 표현한다.
repository가 DB row → Version으로 변환해 반환한다.

책임 분리:
  document = 리소스 정체성/상태/메타
  version  = 특정 시점 구조 스냅샷
  node     = 실제 구조 단위

Phase 4 확장:
  - status: draft | published | superseded | discarded
  - parent_version_id: 선행 버전 참조 (lineage)
  - restored_from_version_id: 복원 출처 버전
  - *_snapshot: 발행 시점 title/summary/metadata/content 불변 스냅샷
  - published_by / published_at: 발행자/발행 시각
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Version:
    """문서 버전 도메인 모델.

    Attributes:
        id                      : UUID (문자열 표현)
        document_id             : 소속 문서 UUID
        version_number          : 문서 내 순차 증가 정수 (1-based)
        status                  : 버전 상태 (draft/published/superseded/discarded)
        source                  : 버전 생성 원인 (manual/system/restore)
        metadata                : 버전 자체 확장 메타 (JSONB)
        created_at              : 생성 시각
        label                   : 사람이 읽기 좋은 버전 레이블 (optional)
        change_summary          : 변경 요약 메시지 (사용자 작성, optional)
        created_by              : 생성자 actor_id
        parent_version_id       : 선행 버전 UUID (lineage)
        restored_from_version_id: 복원 출처 버전 UUID
        title_snapshot          : Publish 시점 문서 제목 스냅샷
        summary_snapshot        : Publish 시점 문서 요약 스냅샷
        metadata_snapshot       : Publish 시점 문서 메타 스냅샷 (JSONB)
        content_snapshot        : 저장된 본문 구조 트리 (JSONB)
        published_by            : 발행자 actor_id
        published_at            : 발행 시각
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
    # Phase 4 확장 필드
    parent_version_id: Optional[str] = None
    restored_from_version_id: Optional[str] = None
    title_snapshot: Optional[str] = None
    summary_snapshot: Optional[str] = None
    metadata_snapshot: Optional[dict[str, Any]] = None
    content_snapshot: Optional[dict[str, Any]] = None
    published_by: Optional[str] = None
    published_at: Optional[datetime] = None
