"""S3 Phase 6 FG 6-2: retention_job 단위 회귀.

회귀 시나리오:
  R1. dry-run 모드 — DB 변경 없음, candidate 카운트 노출.
  R2. archive-first — INSERT INTO ... SELECT + DELETE 트랜잭션. 직접 DELETE 0.
  R3. 환경변수 (RETENTION_DOCUMENT_VIEWED_DAYS / RETENTION_RESOLVED_ANNOTATION_DAYS)
      override 가능.
  R4. _retention_schedule() 가 RETENTION_CRON_HOUR 환경변수를 cron 으로 변환.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from app.scheduler import _retention_schedule
from app.services.retention_job import (
    DEFAULT_RESOLVED_ANNOTATION_DAYS,
    DEFAULT_VIEWED_DAYS,
    RetentionJob,
    _env_bool,
    _env_int,
)


def _make_conn(*, candidates_audit: int, candidates_ann: int) -> MagicMock:
    """SQL 호출 순서별 fetchone / fetchall 응답을 시뮬레이션."""
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)

    # 시퀀스: COUNT(audit), [COUNT(ann)] — dry-run 모드는 count 만.
    cursor.fetchone.side_effect = [
        {"c": candidates_audit},
        {"c": candidates_ann},
    ]
    cursor.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


def test_dry_run_no_mutation():
    conn = _make_conn(candidates_audit=3, candidates_ann=2)
    job = RetentionJob(
        conn,
        viewed_days=7,
        resolved_annotation_days=90,
        batch_limit=100,
        dry_run=True,
    )
    result = job.run(request_id="t-1")
    assert result["status"] == "success"
    assert result["dry_run"] is True
    assert result["document_viewed"]["candidates"] == 3
    assert result["document_viewed"]["archived"] == 0
    assert result["resolved_annotations"]["candidates"] == 2
    assert result["resolved_annotations"]["archived"] == 0
    # commit 호출 0 (dry-run).
    conn.commit.assert_not_called()


def test_actual_run_invokes_commit():
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    # 시퀀스 (Codex 2차 P1-1 시정 후):
    #   1) audit count → 2
    #   2) audit INSERT/DELETE RETURNING → [a1, a2]
    #   3) ann count → 1
    #   4) ann INSERT/DELETE RETURNING → [n1]
    #   5) ann archive 무결성 verify COUNT(*) → 1 (deleted 와 일치 → OK)
    cursor.fetchone.side_effect = [
        {"c": 2},   # audit candidates
        {"c": 1},   # annotations candidates
        {"c": 1},   # annotations archive 무결성 verify
    ]
    cursor.fetchall.side_effect = [
        [{"id": "a1"}, {"id": "a2"}],
        [{"id": "n1"}],
    ]
    conn = MagicMock()
    conn.cursor.return_value = cursor

    job = RetentionJob(
        conn, viewed_days=7, resolved_annotation_days=90,
        batch_limit=100, dry_run=False,
    )
    result = job.run(request_id="t-2")
    assert result["status"] == "success"
    assert result["document_viewed"]["candidates"] == 2
    assert result["document_viewed"]["archived"] == 2
    assert result["resolved_annotations"]["candidates"] == 1
    assert result["resolved_annotations"]["archived"] == 1
    assert conn.commit.call_count == 2  # audit + annotations 각 1회.


# Codex 2차 P1-1 — archive-first 무결성 회귀 ----------------------------------

def test_archive_first_violation_rolls_back():
    """annotation archive 후 verify count 가 deleted 와 불일치 → rollback + error."""
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchone.side_effect = [
        {"c": 0},   # audit candidates 0 (skip)
        {"c": 2},   # annotations candidates
        {"c": 1},   # archive verify — deleted=2 인데 archive 에 1 만 → 위반!
    ]
    cursor.fetchall.side_effect = [
        [{"id": "n1"}, {"id": "n2"}],   # DELETE RETURNING — 2 row
    ]
    conn = MagicMock()
    conn.cursor.return_value = cursor

    job = RetentionJob(
        conn, viewed_days=7, resolved_annotation_days=90,
        batch_limit=100, dry_run=False,
    )
    result = job.run(request_id="t-violation")
    # error 가 errors list 에 잡힘, status = partial.
    assert result["status"] == "partial"
    assert any("archive-first" in e for e in result["errors"])
    conn.rollback.assert_called()
    # archive 부분만 rollback — audit 은 commit 시도 없음 (candidates=0).
    assert conn.commit.call_count == 0


def test_annotation_retention_uses_recursive_cte_sql():
    """nested reply (depth>1) 가 archive 누락 없이 처리되려면 SQL 에 RECURSIVE
    CTE 가 있어야 한다 — Codex P1-1.
    """
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchone.side_effect = [
        {"c": 0},   # audit skip
        {"c": 1},   # ann candidates
        {"c": 1},   # archive verify OK
    ]
    cursor.fetchall.side_effect = [[{"id": "n1"}]]
    conn = MagicMock()
    conn.cursor.return_value = cursor

    job = RetentionJob(
        conn, viewed_days=7, resolved_annotation_days=90,
        batch_limit=100, dry_run=False,
    )
    job.run(request_id="t-recursive")

    executed_sql = [
        (call.args[0] if call.args else "")
        for call in cursor.execute.call_args_list
    ]
    assert any("RECURSIVE" in s and "descendants" in s for s in executed_sql), (
        "annotation retention SQL must use recursive CTE for nested replies"
    )


def test_audit_retention_uses_deletable_union():
    """audit retention DELETE 기준이 inserted ∪ already_archived UNION 임을 보장."""
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchone.side_effect = [
        {"c": 1},   # audit candidates
        {"c": 0},   # ann skip
    ]
    cursor.fetchall.side_effect = [[{"id": "a1"}]]
    conn = MagicMock()
    conn.cursor.return_value = cursor

    job = RetentionJob(
        conn, viewed_days=7, resolved_annotation_days=90,
        batch_limit=100, dry_run=False,
    )
    job.run(request_id="t-audit-deletable")

    executed_sql = [
        (call.args[0] if call.args else "")
        for call in cursor.execute.call_args_list
    ]
    assert any(
        "deletable" in s and "already_archived" in s and "UNION" in s
        for s in executed_sql
    ), "audit retention DELETE must target deletable = inserted UNION already_archived"


def test_env_int_fallback_on_bad_value():
    with patch.dict(os.environ, {"RETENTION_DOCUMENT_VIEWED_DAYS": "not-a-number"}):
        assert _env_int("RETENTION_DOCUMENT_VIEWED_DAYS", DEFAULT_VIEWED_DAYS) == DEFAULT_VIEWED_DAYS


def test_env_int_override():
    with patch.dict(os.environ, {"RETENTION_DOCUMENT_VIEWED_DAYS": "30"}):
        assert _env_int("RETENTION_DOCUMENT_VIEWED_DAYS", DEFAULT_VIEWED_DAYS) == 30


def test_env_bool_accepts_truthy_strings():
    with patch.dict(os.environ, {"RETENTION_DRY_RUN": "1"}):
        assert _env_bool("RETENTION_DRY_RUN") is True
    with patch.dict(os.environ, {"RETENTION_DRY_RUN": "false"}):
        assert _env_bool("RETENTION_DRY_RUN") is False


def test_retention_schedule_default():
    # 환경 변수 미설정 — 02:00 cron.
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("RETENTION_CRON_HOUR", None)
        assert _retention_schedule() == "0 2 * * *"


def test_retention_schedule_override():
    with patch.dict(os.environ, {"RETENTION_CRON_HOUR": "5"}):
        assert _retention_schedule() == "0 5 * * *"


def test_retention_schedule_invalid_uses_default():
    with patch.dict(os.environ, {"RETENTION_CRON_HOUR": "99"}):
        # 99 는 0~23 범위 외 — 기본 02 으로 fallback.
        assert _retention_schedule() == "0 2 * * *"


def test_defaults_match_phase_spec():
    # Phase 6 §1.4 + §3.3: 7일 / 90일.
    assert DEFAULT_VIEWED_DAYS == 7
    assert DEFAULT_RESOLVED_ANNOTATION_DAYS == 90
