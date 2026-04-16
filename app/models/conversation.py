"""
Conversation 도메인 모델 — Phase 3 S2.

멀티턴 대화를 1급 도메인 객체로 정의한다.
Document와 동일한 dataclass 패턴 사용.

계층 구조:
  Conversation (1)
    └── Turn (N)          : 사용자 질문 + AI 응답 쌍
          └── Message (M) : 시스템/사용자/AI 메시지 (세분화)

ACL 원칙 (S2 원칙 ⑥):
  - owner_id, organization_id, access_level 필드로 Document와 동일한 ACL 적용
  - 접근 범위는 Scope Profile(관리자 설정)으로 관리 — 코드에 scope 문자열 하드코딩 금지

감사 원칙 (S2 원칙 ⑥):
  - 모든 생성/수정/삭제 이벤트에 actor_type (user/agent) 기록 의무
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Conversation (대화 세션)
# ---------------------------------------------------------------------------

@dataclass
class Conversation:
    """사용자의 멀티턴 대화 세션.

    Attributes:
        id              : UUID (문자열)
        owner_id        : 소유자 사용자 ID
        organization_id : 소속 조직 ID
        title           : 대화 제목 (사용자 입력 또는 첫 질문 자동 생성)
        status          : active | archived | expired | deleted
        metadata        : 확장 key-value (카테고리, 태그, 언어, 도메인 등)
        retention_days  : 보존 기간(일), 기본 90일
        expires_at      : created_at + retention_days 자동 계산
        deleted_at      : soft delete 타임스탬프 (None이면 활성)
        access_level    : private | organization | public  (ACL)
        created_at      : 생성 시각
        updated_at      : 최종 수정 시각
    """

    id: str
    owner_id: str
    organization_id: str
    title: str
    status: str                         # active | archived | expired | deleted
    metadata: dict[str, Any]
    retention_days: int
    access_level: str                   # private | organization | public
    created_at: datetime
    updated_at: datetime
    expires_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Turn (대화 내 하나의 질문-응답 쌍)
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    """대화 내 하나의 턴 (사용자 질문 + AI 응답 쌍).

    Attributes:
        id                  : UUID (문자열)
        conversation_id     : 소속 Conversation ID
        turn_number         : 대화 내 순서 (1부터 시작)
        user_message        : 사용자의 질의
        assistant_response  : AI의 응답
        retrieval_metadata  : 검색 관련 메타데이터 (JSONB)
            - citations         : [{document_id, version_id, node_id, span_offset, content_hash}, ...]
            - query_original    : 원문 쿼리
            - query_rewritten   : 재작성된 쿼리 (Phase 2 통합)
            - context_window_turns : [turn_id1, ...] (이 턴에 포함된 이전 턴들)
            - retrieval_time_ms : 검색 소요 시간 (ms)
        created_at          : 턴 생성 시각
    """

    id: str
    conversation_id: str
    turn_number: int
    user_message: str
    assistant_response: str
    retrieval_metadata: dict[str, Any]
    created_at: datetime


# ---------------------------------------------------------------------------
# Message (턴 내 세분화된 메시지)
# ---------------------------------------------------------------------------

@dataclass
class Message:
    """턴 내 세분화된 메시지 (시스템 프롬프트, 사용자 질문, AI 응답).

    Attributes:
        id          : UUID (문자열)
        turn_id     : 소속 Turn ID
        role        : user | assistant | system
        content     : 메시지 내용
        metadata    : 추가 메타데이터
            - token_count : 이 메시지의 토큰 수
            - model       : 사용된 모델명 (assistant만 해당)
            - cost        : 생성 비용 (폐쇄망 환경: 0)
        created_at  : 메시지 생성 시각
    """

    id: str
    turn_id: str
    role: str           # user | assistant | system
    content: str
    metadata: dict[str, Any]
    created_at: datetime
