"""
Document 도메인 모델 (순수 Python dataclass).

ORM에 의존하지 않으며, repository가 DB row → Document로 변환해 반환한다.
service와 router는 이 모델을 통해 문서 데이터를 다룬다.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Document:
    """문서 도메인 모델.

    Attributes:
        id            : UUID (문자열 표현)
        title         : 문서 제목
        document_type : 문서 유형 (예: policy, guide, regulation)
        status        : 문서 상태 (draft / published / archived / deprecated)
        metadata      : 확장 key-value 구조 (JSONB)
        summary       : 문서 요약 (optional)
        created_by    : 생성자 actor_id (optional — 감사 로그 확장 슬롯)
        updated_by    : 최종 수정자 actor_id (optional)
        created_at    : 생성 시각 (TIMESTAMPTZ)
        updated_at    : 최종 수정 시각 (TIMESTAMPTZ)
    """

    id: str
    title: str
    document_type: str
    status: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    summary: Optional[str] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    # Phase 4: 현재 활성 버전 포인터
    current_draft_version_id: Optional[str] = None
    current_published_version_id: Optional[str] = None
    # S3 Phase 2 FG 2-0 (2026-04-24): Scope Profile 바인딩
    # 값이 채워지는 경로는 Alembic revision s3_p2_documents_scope_profile,
    # 이후 생성부터는 DocumentsService.create_document 가 ActorContext 기반으로 주입.
    scope_profile_id: Optional[str] = None
    # S3 Phase 2 FG 2-1 UX 2차 (2026-04-24): 현재 배치된 폴더 + 본인 컬렉션 포함 목록.
    # Repository 는 채우지 않고, Service 가 문서 상세 응답을 조립할 때만 set. 리스트
    # 조회 경로는 비용 고려해 None/빈 리스트로 남긴다.
    folder_id: Optional[str] = None
    in_collection_ids: list[str] = field(default_factory=list)
