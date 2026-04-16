"""
백그라운드 배치 스케줄러 — Phase 3 S2.

S2 원칙 ⑦ (폐쇄망 호환):
  - 외부 의존 없음 — stdlib threading + cron_util 사용
  - APScheduler 미설치 환경에서도 정상 동작

설계:
  - threading.Thread 기반 데몬 스레드
  - cron_util.next_run() 으로 다음 실행 시각 계산
  - 실행 실패 시 로그만 기록, 다음 주기에 재시도
  - stop() 으로 graceful shutdown (이벤트 방식)
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional
from uuid import uuid4

from app.services.cron_util import next_run as cron_next_run

logger = logging.getLogger(__name__)

# 기본 배치 스케줄: 매일 자정 0시
_DEFAULT_EXPIRATION_SCHEDULE = "0 0 * * *"


class BatchScheduler:
    """cron 기반 배치 스케줄러.

    사용법::

        scheduler = BatchScheduler()
        scheduler.start()
        # ... 앱 실행 중 ...
        scheduler.stop()
    """

    def __init__(
        self,
        *,
        schedule: str = _DEFAULT_EXPIRATION_SCHEDULE,
        job_fn: Optional[Callable] = None,
    ) -> None:
        """
        Args:
            schedule : cron 표현식 (5-field)
            job_fn   : 실행할 콜백 (없으면 기본 expiration batch 사용)
        """
        self._schedule = schedule
        self._job_fn = job_fn or _default_expiration_job
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------

    def start(self) -> None:
        """데몬 스레드로 스케줄러 시작."""
        if self._thread and self._thread.is_alive():
            logger.warning("BatchScheduler already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="expiration-batch-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("BatchScheduler started schedule=%s", self._schedule)

    def stop(self, timeout: float = 10.0) -> None:
        """스케줄러 중지 (graceful)."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("BatchScheduler stopped")

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------
    # 내부 루프
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """다음 실행 시각까지 대기 → 실행 반복."""
        while not self._stop_event.is_set():
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            next_dt = cron_next_run(self._schedule, now=now)
            if next_dt is None:
                logger.error("BatchScheduler: cannot compute next_run, stopping")
                break

            wait_seconds = (next_dt - now).total_seconds()
            logger.debug(
                "BatchScheduler next_run=%s wait_seconds=%.0f",
                next_dt.isoformat(), wait_seconds,
            )

            # stop_event 를 기다리되, 최대 wait_seconds 초 후 깨어남
            if self._stop_event.wait(timeout=max(wait_seconds, 0)):
                break  # stop 신호

            if self._stop_event.is_set():
                break

            self._execute()

    def _execute(self) -> None:
        """배치 함수 실행 — 예외는 로그로만 처리."""
        run_id = str(uuid4())
        logger.info("BatchScheduler executing job run_id=%s", run_id)
        try:
            self._job_fn(request_id=run_id)
        except Exception as exc:
            logger.error("BatchScheduler job failed run_id=%s error=%s", run_id, exc)


# ---------------------------------------------------------------------------
# 기본 배치 함수 (ExpirationBatchJob 래핑)
# ---------------------------------------------------------------------------

def _default_expiration_job(*, request_id: Optional[str] = None) -> None:
    """ExpirationBatchJob 을 새 DB 연결로 실행."""
    try:
        from app.db import get_db
        from app.services.expiration_batch import ExpirationBatchJob

        with get_db() as conn:
            job = ExpirationBatchJob(conn)
            result = job.run(dry_run=False, request_id=request_id)
        logger.info("expiration_job_result %s", result)
    except Exception as exc:
        logger.error("expiration_job_error error=%s", exc)


# ---------------------------------------------------------------------------
# 모듈 수준 싱글턴 (FastAPI startup/shutdown 에서 사용)
# ---------------------------------------------------------------------------

_scheduler: Optional[BatchScheduler] = None


def get_scheduler() -> BatchScheduler:
    """모듈 수준 스케줄러 싱글턴."""
    global _scheduler
    if _scheduler is None:
        import os
        schedule = os.getenv("EXPIRATION_BATCH_SCHEDULE", _DEFAULT_EXPIRATION_SCHEDULE)
        _scheduler = BatchScheduler(schedule=schedule)
    return _scheduler


def start_scheduler() -> None:
    """FastAPI startup 이벤트에서 호출."""
    import os
    if os.getenv("AUTO_EXPIRATION_ENABLED", "true").lower() == "false":
        logger.info("AUTO_EXPIRATION_ENABLED=false → BatchScheduler 비활성")
        return
    get_scheduler().start()


def stop_scheduler() -> None:
    """FastAPI shutdown 이벤트에서 호출."""
    global _scheduler
    if _scheduler and _scheduler.is_running:
        _scheduler.stop()
