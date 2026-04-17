"""
배치 재추출 백그라운드 서비스 — Phase 8 Task 8-7.

FastAPI BackgroundTasks에서 호출되는 동기 진입점 + asyncio 내부 워커.

설계:
  - CHUNK_SIZE=10 단위 처리
  - Rate limiting: 청크당 CHUNK_SIZE / RATE_LIMIT_PER_SEC 초 대기
  - 지수 백오프: [1s, 2s, 4s]
  - 부분 실패 허용: 개별 문서 실패 시 skip & log, 다음 문서 계속
  - 취소 플래그: 청크 경계마다 확인
  - S2 원칙 ⑦: LLM 실패 시 MockLLMProvider fallback (ExtractionPipelineService 내부 처리)
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.db.connection import get_db
from app.models.batch_extraction import BatchJobStatus
from app.repositories.batch_extraction_repository import (
    BatchExtractionJobRepository,
    ExtractionRetryLogRepository,
)
from app.repositories.approved_extraction_repository import ApprovedExtractionRepository
from app.services.extraction.extraction_pipeline_service import ExtractionPipelineService

logger = logging.getLogger(__name__)

CHUNK_SIZE = 10
RATE_LIMIT_PER_SEC = 5
MAX_RETRIES = 3
RETRY_DELAYS = [1, 2, 4]


# ---------------------------------------------------------------------------
# 동기 진입점 (FastAPI BackgroundTasks 에서 호출)
# ---------------------------------------------------------------------------

def run_batch_extraction_background(
    *,
    job_id: str,
    extraction_schema_id: str,
    extraction_schema_version: int,
    scope_profile_id: Optional[str],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
    sample_count: Optional[int],
    comparison_mode: bool,
    llm_provider: Any,
) -> None:
    """동기 래퍼 — asyncio.run()으로 비동기 워커를 실행한다."""
    try:
        asyncio.run(
            _execute_batch(
                job_id=UUID(job_id),
                extraction_schema_id=extraction_schema_id,
                extraction_schema_version=extraction_schema_version,
                scope_profile_id=UUID(scope_profile_id) if scope_profile_id else None,
                date_from=date_from,
                date_to=date_to,
                sample_count=sample_count,
                comparison_mode=comparison_mode,
                llm_provider=llm_provider,
            )
        )
    except Exception as exc:
        logger.error("Batch job %s background error: %s", job_id, exc, exc_info=True)
        # best-effort 상태 업데이트
        try:
            with get_db() as conn:
                repo = BatchExtractionJobRepository(conn)
                repo.update_status(UUID(job_id), BatchJobStatus.FAILED, error_summary=str(exc))
                conn.commit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 비동기 워커
# ---------------------------------------------------------------------------

async def _execute_batch(
    *,
    job_id: UUID,
    extraction_schema_id: str,
    extraction_schema_version: int,
    scope_profile_id: Optional[UUID],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
    sample_count: Optional[int],
    comparison_mode: bool,
    llm_provider: Any,
) -> None:
    logger.info("Batch job %s starting", job_id)

    # 1. RUNNING 으로 전이
    with get_db() as conn:
        repo = BatchExtractionJobRepository(conn)
        job = repo.update_status(job_id, BatchJobStatus.RUNNING)
        conn.commit()

    if not job:
        logger.error("Batch job %s not found", job_id)
        return

    try:
        # 2. 대상 문서 목록 조회
        target_doc_ids = await _fetch_target_documents(
            extraction_schema_id=extraction_schema_id,
            scope_profile_id=scope_profile_id,
            date_from=date_from,
            date_to=date_to,
            sample_count=sample_count,
        )

        total = len(target_doc_ids)
        logger.info("Batch job %s: %d documents to re-extract", job_id, total)

        # total_count 업데이트
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE batch_extraction_jobs SET total_count = %s WHERE id = %s",
                    (total, str(job_id)),
                )
            conn.commit()

        completed = failed = skipped = 0

        # 3. 청크 단위 처리
        for chunk_start in range(0, total, CHUNK_SIZE):
            # 취소 플래그 확인
            with get_db() as conn:
                repo = BatchExtractionJobRepository(conn)
                if repo.is_cancellation_requested(job_id):
                    logger.info("Batch job %s cancelled", job_id)
                    repo.update_status(job_id, BatchJobStatus.CANCELLED)
                    conn.commit()
                    return

            chunk = target_doc_ids[chunk_start : chunk_start + CHUNK_SIZE]
            chunk_result = await _process_chunk(
                job_id=job_id,
                document_ids=chunk,
                extraction_schema_id=extraction_schema_id,
                extraction_schema_version=extraction_schema_version,
                scope_profile_id=scope_profile_id,
                llm_provider=llm_provider,
            )

            completed += chunk_result["completed"]
            failed += chunk_result["failed"]
            skipped += chunk_result["skipped"]

            # 진행률 업데이트
            with get_db() as conn:
                repo = BatchExtractionJobRepository(conn)
                repo.update_progress(job_id, completed, failed, skipped, total)
                conn.commit()

            logger.info(
                "Batch job %s chunk %d/%d: ok=%d fail=%d skip=%d",
                job_id,
                chunk_start // CHUNK_SIZE + 1,
                -(-total // CHUNK_SIZE),
                completed, failed, skipped,
            )

            # Rate limiting
            await asyncio.sleep(CHUNK_SIZE / RATE_LIMIT_PER_SEC)

        # 4. 최종 상태
        final_status = BatchJobStatus.COMPLETED if failed == 0 else BatchJobStatus.FAILED
        error_summary = f"{failed}/{total} documents failed" if failed > 0 else None
        with get_db() as conn:
            repo = BatchExtractionJobRepository(conn)
            repo.update_status(job_id, final_status, error_summary=error_summary)
            conn.commit()

        logger.info(
            "Batch job %s finished: status=%s ok=%d fail=%d",
            job_id, final_status.value, completed, failed,
        )

    except Exception as exc:
        logger.error("Batch job %s failed: %s", job_id, exc, exc_info=True)
        with get_db() as conn:
            repo = BatchExtractionJobRepository(conn)
            repo.update_status(job_id, BatchJobStatus.FAILED, error_summary=str(exc))
            conn.commit()


async def _fetch_target_documents(
    *,
    extraction_schema_id: str,
    scope_profile_id: Optional[UUID],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
    sample_count: Optional[int],
) -> List[UUID]:
    """approved_extractions 에서 재추출 대상 문서 ID 목록을 조회한다."""
    import random

    conditions = [
        "extraction_schema_id = %s",
        "is_soft_deleted = FALSE",
    ]
    params: list = [extraction_schema_id]

    if scope_profile_id:
        conditions.append("scope_profile_id = %s")
        params.append(str(scope_profile_id))
    if date_from:
        conditions.append("approved_at >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("approved_at <= %s")
        params.append(date_to)

    sql = f"""
        SELECT DISTINCT document_id
        FROM approved_extractions
        WHERE {' AND '.join(conditions)}
        ORDER BY document_id
    """

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    doc_ids = [UUID(str(row[0])) for row in rows]

    if sample_count and sample_count < len(doc_ids):
        doc_ids = random.sample(doc_ids, sample_count)

    return doc_ids


async def _process_chunk(
    *,
    job_id: UUID,
    document_ids: List[UUID],
    extraction_schema_id: str,
    extraction_schema_version: int,
    scope_profile_id: Optional[UUID],
    llm_provider: Any,
) -> Dict[str, int]:
    """한 청크(최대 CHUNK_SIZE 개)를 처리한다."""
    result = {"completed": 0, "failed": 0, "skipped": 0}

    for doc_id in document_ids:
        success = False
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            t0 = time.monotonic()
            try:
                await _re_extract_document(
                    document_id=doc_id,
                    extraction_schema_id=extraction_schema_id,
                    extraction_schema_version=extraction_schema_version,
                    scope_profile_id=scope_profile_id,
                    llm_provider=llm_provider,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)
                _log_retry(job_id, doc_id, attempt, "success", None, latency_ms)
                result["completed"] += 1
                success = True
                break

            except Exception as exc:
                latency_ms = int((time.monotonic() - t0) * 1000)
                last_error = exc
                logger.warning(
                    "doc %s attempt %d/%d failed: %s", doc_id, attempt, MAX_RETRIES, exc
                )
                _log_retry(job_id, doc_id, attempt, "failed", str(exc), latency_ms)

                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAYS[attempt - 1])

        if not success:
            result["failed"] += 1
            _mark_failed_document(job_id, doc_id)

    return result


async def _re_extract_document(
    *,
    document_id: UUID,
    extraction_schema_id: str,
    extraction_schema_version: int,
    scope_profile_id: Optional[UUID],
    llm_provider: Any,
) -> None:
    """단일 문서에 대해 ExtractionPipelineService.run()을 호출한다."""
    # 문서 텍스트와 스키마 필드를 DB에서 조회
    with get_db() as conn:
        doc_text, schema_fields = _fetch_doc_and_schema(
            conn, document_id, extraction_schema_id, extraction_schema_version
        )

    if not doc_text:
        raise ValueError(f"Document {document_id} not found or has no content")

    svc = ExtractionPipelineService(llm_provider=llm_provider)
    with get_db() as conn:
        await svc.run(
            document_id=document_id,
            document_version=1,
            document_text=doc_text,
            doc_type_code=extraction_schema_id,
            schema_fields=schema_fields,
            schema_version=extraction_schema_version,
            scope_profile_id=scope_profile_id,
            conn=conn,
        )
        conn.commit()


def _fetch_doc_and_schema(conn, document_id: UUID, schema_id: str, schema_version: int):
    """문서 본문 텍스트와 추출 스키마 필드를 조회한다."""
    # 문서 본문: nodes 테이블에서 content 결합
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT string_agg(n.content, E'\n' ORDER BY n.position)
            FROM nodes n
            JOIN versions v ON n.version_id = v.id
            JOIN documents d ON v.document_id = d.id
            WHERE d.id = %s
              AND v.status = 'published'
            LIMIT 1
            """,
            (str(document_id),),
        )
        row = cur.fetchone()
        doc_text = row[0] if row and row[0] else ""

    # 스키마 필드
    with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
        cur.execute(
            """
            SELECT fields
            FROM extraction_schema_versions
            WHERE extraction_schema_id = %s AND version = %s
            LIMIT 1
            """,
            (schema_id, schema_version),
        )
        row = cur.fetchone()
        schema_fields = dict(row["fields"]) if row and row.get("fields") else {}

    return doc_text, schema_fields


def _log_retry(
    job_id: UUID,
    document_id: UUID,
    attempt: int,
    status: str,
    error_reason: Optional[str],
    latency_ms: int,
) -> None:
    """재시도 이력을 DB에 기록한다 (best-effort)."""
    try:
        with get_db() as conn:
            repo = ExtractionRetryLogRepository(conn)
            repo.create(
                job_id=job_id,
                document_id=document_id,
                attempt_number=attempt,
                status=status,
                error_reason=error_reason,
                latency_ms=latency_ms,
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to log retry: %s", exc)


def _mark_failed_document(job_id: UUID, document_id: UUID) -> None:
    """실패 문서 ID를 배치 작업 기록에 추가한다 (best-effort)."""
    try:
        with get_db() as conn:
            repo = BatchExtractionJobRepository(conn)
            repo.append_failed_document(job_id, document_id)
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to mark failed document: %s", exc)
