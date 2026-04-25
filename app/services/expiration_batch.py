"""
대화 자동 만료 배치 작업 — Phase 3 S2.

책임:
  - expires_at < NOW() 인 활성 대화를 'expired' 상태로 전환
  - dry-run 모드: 변경 없이 대상 목록만 반환
  - 감사 로그: 모든 만료 처리를 audit_emitter 로 기록 (actor_type=system)

S2 원칙 ⑦ (폐쇄망 호환):
  - 외부 의존 없음 — PostgreSQL + 로컬 cron 계산만 사용

설계 원칙:
  - 개별 대화 만료 오류는 로그로 기록하되 배치를 중단하지 않는다.
  - 모든 변경은 단일 트랜잭션으로 커밋 (실패 시 rollback).
  - 배치 자체가 실패해도 애플리케이션 서비스에 영향 없음 (non-blocking).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from app.audit.emitter import audit_emitter
from app.repositories.conversation_repository import ConversationRepository
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# 배치 작업 "시스템" actor UUID — 실제 사용자가 아닌 배치 시스템 식별자
_SYSTEM_ACTOR_ID = "00000000-0000-0000-0000-000000000000"


class ExpirationBatchJob:
    """대화 자동 만료 배치.

    psycopg2 연결을 직접 받아 ConversationRepository를 통해 조작한다.
    연결 획득/해제는 호출자(scheduler) 책임.
    """

    def __init__(self, conn) -> None:
        """
        Args:
            conn: psycopg2 connection (with RealDictCursor factory 설정된)
        """
        self._conn = conn
        self._repo = ConversationRepository(conn)

    # ------------------------------------------------------------------
    # 배치 실행
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        dry_run: bool = False,
        batch_limit: int = 200,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """배치 실행.

        Args:
            dry_run      : True 이면 DB 변경 없이 대상 목록만 반환
            batch_limit  : 한 번에 처리할 최대 대화 수
            request_id   : 감사 로그 request_id (스케줄러 실행 시 자동 생성)

        Returns::

            {
              "status": "success" | "error",
              "expired_count": int,
              "failed_count": int,
              "dry_run": bool,
              "message": str,
              "errors": [str, ...],
            }
        """
        started_at = utcnow()
        logger.info(
            "expiration_batch_start dry_run=%s limit=%s request_id=%s",
            dry_run, batch_limit, request_id,
        )

        try:
            expired_conversations = self._repo.list_expired(limit=batch_limit)
            total = len(expired_conversations)

            logger.info("expiration_batch_candidates count=%s dry_run=%s", total, dry_run)

            if dry_run:
                return {
                    "status": "success",
                    "expired_count": total,
                    "failed_count": 0,
                    "dry_run": True,
                    "message": f"[DRY RUN] 만료 대상: {total}건",
                    "errors": [],
                }

            expired_count = 0
            failed_count = 0
            errors: list[str] = []

            for conv in expired_conversations:
                try:
                    ok = self._repo.mark_expired(conv.id)
                    if ok:
                        expired_count += 1
                        audit_emitter.emit(
                            event_type="conversation.expired",
                            action="conversation.expire",
                            actor_id=_SYSTEM_ACTOR_ID,
                            actor_type="system",
                            resource_type="conversation",
                            resource_id=conv.id,
                            result="success",
                            request_id=request_id,
                            metadata={
                                "reason": "auto_expiration",
                                "expires_at": conv.expires_at.isoformat() if conv.expires_at else None,
                                "expired_at": started_at.isoformat(),
                            },
                        )
                    else:
                        logger.debug(
                            "expiration_batch_skip conv_id=%s (already expired or deleted)",
                            conv.id,
                        )
                except Exception as exc:
                    failed_count += 1
                    errors.append(f"conv_id={conv.id}: {exc}")
                    logger.error(
                        "expiration_batch_item_failed conv_id=%s error=%s",
                        conv.id, exc,
                    )

            self._conn.commit()

            elapsed = (utcnow() - started_at).total_seconds()
            logger.info(
                "expiration_batch_complete expired=%s failed=%s elapsed_s=%.2f",
                expired_count, failed_count, elapsed,
            )

            return {
                "status": "success",
                "expired_count": expired_count,
                "failed_count": failed_count,
                "dry_run": False,
                "message": f"배치 완료. 만료: {expired_count}건, 실패: {failed_count}건",
                "errors": errors,
            }

        except Exception as exc:
            logger.error("expiration_batch_failed error=%s", exc)
            try:
                self._conn.rollback()
            except Exception as rollback_exc:
                logger.warning("expiration_batch rollback 실패: %s", rollback_exc)
            return {
                "status": "error",
                "expired_count": 0,
                "failed_count": 0,
                "dry_run": dry_run,
                "message": f"배치 실패: {exc}",
                "errors": [str(exc)],
            }
