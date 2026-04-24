"""
평가 Repository + 모델 단위 테스트 — Phase 7 FG7.2 (task7-7)

psycopg2 cursor를 Mock으로 대체하여 DB 없이 테스트.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

import pytest

from app.repositories.evaluation_repository import (
    EvaluationResultRecordRepository,
    EvaluationRunRepository,
)
from app.services.evaluation.models import (
    EvaluationResult,
    LatencyMetrics,
    ScoreMetrics,
    TokenMetrics,
    CostMetrics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_conn(fetchone_val=None, fetchall_val=None, description=None):
    """psycopg2 connection + cursor mock."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: cur
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur
    if fetchone_val is not None:
        cur.fetchone.return_value = fetchone_val
    if fetchall_val is not None:
        cur.fetchall.return_value = fetchall_val
    if description is not None:
        cur.description = [(col,) for col in description]
    return conn, cur


def _sample_result(item_id: str = "item-1") -> EvaluationResult:
    return EvaluationResult(
        item_id=item_id,
        question="Q?",
        answer="A.",
        contexts=["ctx"],
        scores=ScoreMetrics(faithfulness=0.8, citation_present_rate=1.0),
        latency_metrics=LatencyMetrics(retrieval_ms=10.0, generation_ms=20.0, total_ms=30.0),
        token_metrics=TokenMetrics(query_tokens=10, response_tokens=5, total_tokens=15),
        cost_metrics=CostMetrics(query_cost=0.001, response_cost=0.002, total_cost=0.003),
    )


# ---------------------------------------------------------------------------
# EvaluationRunRepository
# ---------------------------------------------------------------------------

class TestEvaluationRunRepository:
    def test_create_calls_insert(self):
        cols = ["id", "batch_id", "status", "scope_id", "actor_id",
                "actor_type", "total_items", "created_at"]
        run_id = str(uuid4())
        now = datetime.now(timezone.utc)
        conn, cur = _mock_conn(
            fetchone_val=dict(zip(cols, (run_id, "batch1", "queued", "scope1", "user1", "user", 3, now))),
            description=cols,
        )
        repo = EvaluationRunRepository(conn)
        run = repo.create(
            batch_id="batch1",
            scope_id="scope1",
            actor_id="user1",
            actor_type="user",
            total_items=3,
        )
        cur.execute.assert_called_once()
        assert run["status"] == "queued"
        assert run["batch_id"] == "batch1"

    def test_get_by_id_not_found(self):
        conn, cur = _mock_conn(description=[])
        cur.fetchone.return_value = None  # explicit None
        repo = EvaluationRunRepository(conn)
        result = repo.get_by_id("nonexistent", "scope1")
        assert result is None

    def test_get_by_id_found(self):
        cols = ["id", "batch_id", "status", "scope_id", "actor_id", "actor_type",
                "total_items", "successful_items", "failed_items", "overall_score",
                "total_tokens", "total_latency_ms", "total_cost", "duration_seconds",
                "started_at", "completed_at", "created_at"]
        run_id = str(uuid4())
        vals = (run_id, "b", "completed", "s", "u", "user",
                3, 3, 0, 0.85, 0, 0.0, 0.0, 10.0, None, None, datetime.now(timezone.utc))
        conn, cur = _mock_conn(fetchone_val=dict(zip(cols, vals)), description=cols)
        repo = EvaluationRunRepository(conn)
        result = repo.get_by_id(run_id, "s")
        assert result is not None
        assert result["id"] == run_id
        assert result["status"] == "completed"

    def test_list_by_scope(self):
        cols = ["id", "batch_id", "status", "scope_id", "actor_id", "actor_type",
                "total_items", "successful_items", "failed_items", "overall_score",
                "duration_seconds", "created_at", "completed_at"]
        conn, cur = _mock_conn(description=cols)
        # COUNT(*) returns 2, then list returns 2 rows
        run_id1, run_id2 = str(uuid4()), str(uuid4())
        now = datetime.now(timezone.utc)
        cur.fetchone.return_value = {"total": 2}
        cur.fetchall.return_value = [
            dict(zip(cols, (run_id1, "b1", "completed", "s", "u", "user", 3, 3, 0, 0.8, 1.0, now, now))),
            dict(zip(cols, (run_id2, "b2", "queued",    "s", "u", "user", 5, 0, 0, None, None, now, None))),
        ]
        repo = EvaluationRunRepository(conn)
        items, total = repo.list_by_scope("s")
        assert total == 2
        assert len(items) == 2

    def test_update_status(self):
        conn, cur = _mock_conn()
        cur.rowcount = 1
        repo = EvaluationRunRepository(conn)
        ok = repo.update_status("run-1", "scope-1", "running")
        assert ok is True
        cur.execute.assert_called_once()

    def test_finalize(self):
        conn, cur = _mock_conn()
        cur.rowcount = 1
        repo = EvaluationRunRepository(conn)
        ok = repo.finalize(
            run_id="run-1", scope_id="scope-1",
            successful_items=3, failed_items=0, overall_score=0.9,
            total_tokens=100, total_latency_ms=300.0, total_cost=0.01,
            duration_seconds=5.0,
        )
        assert ok is True


# ---------------------------------------------------------------------------
# EvaluationResultRecordRepository
# ---------------------------------------------------------------------------

class TestEvaluationResultRecordRepository:
    def test_batch_insert(self):
        conn, cur = _mock_conn()
        repo = EvaluationResultRecordRepository(conn)
        results = [_sample_result(f"item-{i}") for i in range(3)]
        count = repo.batch_insert("run-1", results)
        assert count == 3
        assert cur.execute.call_count == 3

    def test_batch_insert_empty(self):
        conn, cur = _mock_conn()
        repo = EvaluationResultRecordRepository(conn)
        count = repo.batch_insert("run-1", [])
        assert count == 0

    def test_list_by_run(self):
        cols = ["id", "item_id", "question", "answer",
                "faithfulness", "answer_relevance", "context_precision",
                "context_recall", "citation_present_rate", "hallucination_rate",
                "overall_score", "total_latency_ms", "total_tokens", "estimated_cost",
                "created_at"]
        now = datetime.now(timezone.utc)
        row = dict(zip(cols, (str(uuid4()), "item-1", "Q?", "A.",
               0.8, 0.7, 0.6, 0.5, 1.0, 0.0, 0.75, 30.0, 15, 0.003, now)))
        conn, cur = _mock_conn(fetchall_val=[row], description=cols)
        repo = EvaluationResultRecordRepository(conn)
        results = repo.list_by_run("run-1")
        assert len(results) == 1
        assert results[0]["item_id"] == "item-1"
        assert results[0]["faithfulness"] == 0.8


# ---------------------------------------------------------------------------
# API import smoke test
# ---------------------------------------------------------------------------

def test_evaluations_router_importable():
    from app.api.v1.evaluations import router
    routes = {r.path for r in router.routes}
    assert "/run" in routes
    assert "" in routes or "/" in routes or any("compare" in p for p in routes)
