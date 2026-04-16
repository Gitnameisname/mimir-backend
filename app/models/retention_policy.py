"""
RetentionPolicy 도메인 모델 — Phase 3 S2.

조직별 보존 정책 설정을 저장한다.
기존 codebase 패턴(Python dataclass + psycopg2)을 따른다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class RetentionPolicy:
    """조직별 대화 보존 정책.

    Attributes:
        id                    : 정책 고유 UUID
        organization_id       : 조직 UUID
        default_retention_days: 기본 보존 기간(일) — conversations.retention_days 기본값
        max_retention_days    : 조직 내 최대 허용 보존 기간(일)
        auto_expire_enabled   : 자동 만료 배치 활성화 여부
        batch_schedule        : cron 표현식 (5-field, 예: "0 0 * * *" = 매일 자정)
        created_at            : 정책 생성 시각
        updated_at            : 정책 수정 시각
    """

    id: str
    organization_id: str
    default_retention_days: int
    max_retention_days: int
    auto_expire_enabled: bool
    batch_schedule: str
    created_at: datetime
    updated_at: datetime
