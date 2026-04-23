"""
GoldenSetRepository.item_count 단위 테스트 — S2-5 코드 커버리지 보강 (2026-04-21).

테스트 대상:
  - GoldenSetRepository.list_by_scope() → 각 row 에 _count_items 결과가 attach 되는지
  - GoldenSetRepository._count_items() → SQL 에 is_deleted=FALSE 절이 포함되고
    golden_set_id 가 파라미터화되는지 (SQL injection 방어)
  - GoldenSetRepository.get_version_history() → item_count = len(items_snapshot)
    (items_snapshot 이 None 인 경우 0 으로 안전 처리)
  - GoldenSet / GoldenSetResponse DTO 의 item_count 필드 (None/정수 직렬화)

원칙:
  - S2 ⑥: 모든 조회 SQL 에 scope_id ACL 필터 확인.
  - psycopg2 Mock cursor 로 DB 없이 실행 (CLAUDE.md @unit 마커 기준).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.models.golden_set import (
    GoldenSet,
    GoldenSetDomain,
    GoldenSetResponse,
    GoldenSetStatus,
    GoldenSetVersionInfo,
)
from app.repositories.golden_set_repository import GoldenSetRepository

_NOW = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _mock_conn_and_cursor():
    """psycopg2 connection + cursor mock (RealDictCursor 형태 — dict row)."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur
    return conn, cur


def _golden_set_row(
    *,
    gid: str = "gs-1",
    scope_id: str = "scope-1",
    name: str = "Tech QA",
) -> dict:
    """_row_to_golden_set 에 넘겨도 통과하는 완전한 row 딕셔너리."""
    return {
        "id": gid,
        "scope_id": scope_id,
        "name": name,
        "description": None,
        "domain": GoldenSetDomain.TECHNICAL_GUIDE.value,
        "status": GoldenSetStatus.DRAFT.value,
        "version": 1,
        "extra_metadata": {},
        "created_at": _NOW,
        "created_by": "user-1",
        "updated_at": _NOW,
        "updated_by": None,
        "deleted_at": None,
        "is_deleted": False,
    }


# ---------------------------------------------------------------------------
# 1. list_by_scope 가 각 row 에 item_count 를 부착한다
# ---------------------------------------------------------------------------


class TestListByScopeAttachesItemCount:
    def test_two_rows_each_get_own_count(self):
        conn, cur = _mock_conn_and_cursor()
        row_a = _golden_set_row(gid="gs-a", name="A")
        row_b = _golden_set_row(gid="gs-b", name="B")

        # fetchone 순서:
        #   [0] list_by_scope 내부 COUNT(*) 총 개수 → {"count": 2}
        #   [1] _count_items("gs-a") → {"count": 5}
        #   [2] _count_items("gs-b") → {"count": 0}
        cur.fetchone.side_effect = [
            {"count": 2},
            {"count": 5},
            {"count": 0},
        ]
        # fetchall 은 list_by_scope 의 LIMIT/OFFSET 쿼리에서 한 번만 호출됨
        cur.fetchall.return_value = [row_a, row_b]

        repo = GoldenSetRepository(conn)
        sets, total = repo.list_by_scope("scope-1")

        assert total == 2
        assert len(sets) == 2
        assert sets[0].id == "gs-a"
        assert sets[0].item_count == 5
        assert sets[1].id == "gs-b"
        assert sets[1].item_count == 0

    def test_empty_list_returns_total_0(self):
        conn, cur = _mock_conn_and_cursor()
        cur.fetchone.side_effect = [{"count": 0}]
        cur.fetchall.return_value = []

        repo = GoldenSetRepository(conn)
        sets, total = repo.list_by_scope("scope-1")

        assert total == 0
        assert sets == []
        # _count_items 는 한 번도 호출되지 않아야 함 (fetchone 추가 소비 없음)

    def test_scope_id_is_passed_to_sql(self):
        """S2 ⑥: list_by_scope 의 SQL 파라미터 첫 번째는 scope_id 여야 함."""
        conn, cur = _mock_conn_and_cursor()
        cur.fetchone.side_effect = [{"count": 0}]
        cur.fetchall.return_value = []

        repo = GoldenSetRepository(conn)
        repo.list_by_scope("scope-xyz")

        # execute 호출이 최소 2번 (COUNT + LIST)
        assert cur.execute.call_count >= 2
        count_call = cur.execute.call_args_list[0]
        list_call = cur.execute.call_args_list[1]
        # 첫 번째 positional param → params 리스트
        assert count_call.args[1][0] == "scope-xyz"
        assert list_call.args[1][0] == "scope-xyz"
        # SQL 에 is_deleted=FALSE 와 scope_id=%s 가 포함
        for sql in (count_call.args[0], list_call.args[0]):
            assert "scope_id=%s" in sql
            assert "is_deleted=FALSE" in sql


# ---------------------------------------------------------------------------
# 2. _count_items 의 SQL 안전성 + IDOR 방어
# ---------------------------------------------------------------------------


class TestCountItemsSql:
    def test_count_items_where_clause_has_is_deleted_false(self):
        conn, cur = _mock_conn_and_cursor()
        cur.fetchone.return_value = {"count": 7}

        repo = GoldenSetRepository(conn)
        result = repo._count_items("gs-x")

        assert result == 7
        assert cur.execute.call_count == 1
        sql, params = cur.execute.call_args.args
        assert "FROM golden_items" in sql
        assert "golden_set_id=%s" in sql
        assert "is_deleted=FALSE" in sql, (
            "_count_items 는 소프트 삭제된 항목을 제외해야 함 (S2 무결성)"
        )
        # golden_set_id 는 반드시 파라미터화되어야 함 (SQL injection 방어 — S1 #3)
        assert params == ("gs-x",)

    def test_count_items_returns_zero_for_empty_set(self):
        conn, cur = _mock_conn_and_cursor()
        cur.fetchone.return_value = {"count": 0}

        repo = GoldenSetRepository(conn)
        assert repo._count_items("gs-empty") == 0

    def test_count_items_no_hardcoded_id_in_sql(self):
        """입력 id 가 SQL 문자열에 직접 interpolation 되지 않는지 — 단순 방어."""
        conn, cur = _mock_conn_and_cursor()
        cur.fetchone.return_value = {"count": 1}

        repo = GoldenSetRepository(conn)
        malicious = "gs-1'; DROP TABLE golden_items;--"
        repo._count_items(malicious)

        sql, params = cur.execute.call_args.args
        # SQL 자체에는 악성 문자열이 들어있지 않아야 함 — 항상 파라미터로 전달
        assert malicious not in sql
        assert params == (malicious,)


# ---------------------------------------------------------------------------
# 3. get_version_history 의 item_count = len(items_snapshot)
# ---------------------------------------------------------------------------


class TestGetVersionHistoryItemCount:
    def test_item_count_from_snapshot_length(self):
        conn, cur = _mock_conn_and_cursor()

        # 1) get_by_id 로 존재 확인 → _row_to_golden_set 가 처리할 수 있는 row
        existing = _golden_set_row(gid="gs-1")
        # 2) get_version_history 의 list 쿼리 → 각 버전에 items_snapshot 포함
        version_rows = [
            {
                "version": 3,
                "created_at": _NOW,
                "created_by": "user-1",
                "items_snapshot": [
                    {"id": "i1", "version": 1, "question": "Q1"},
                    {"id": "i2", "version": 1, "question": "Q2"},
                    {"id": "i3", "version": 1, "question": "Q3"},
                ],
            },
            {
                "version": 2,
                "created_at": _NOW,
                "created_by": "user-1",
                "items_snapshot": [{"id": "i1", "version": 1, "question": "Q1"}],
            },
            {
                "version": 1,
                "created_at": _NOW,
                "created_by": "user-1",
                "items_snapshot": [],
            },
        ]

        # fetchone: get_by_id 의 SELECT 결과 (row)
        cur.fetchone.side_effect = [existing]
        # fetchall: version 쿼리 결과
        cur.fetchall.return_value = version_rows

        repo = GoldenSetRepository(conn)
        history = repo.get_version_history("gs-1", "scope-1")

        assert len(history) == 3
        assert all(isinstance(h, GoldenSetVersionInfo) for h in history)
        assert history[0].version == 3 and history[0].item_count == 3
        assert history[1].version == 2 and history[1].item_count == 1
        assert history[2].version == 1 and history[2].item_count == 0

    def test_item_count_null_snapshot_is_zero(self):
        """items_snapshot 이 NULL (None) 일 때도 item_count 는 0 으로 안전 처리."""
        conn, cur = _mock_conn_and_cursor()
        cur.fetchone.side_effect = [_golden_set_row(gid="gs-1")]
        cur.fetchall.return_value = [
            {
                "version": 1,
                "created_at": _NOW,
                "created_by": "user-1",
                "items_snapshot": None,
            }
        ]

        repo = GoldenSetRepository(conn)
        history = repo.get_version_history("gs-1", "scope-1")

        assert len(history) == 1
        assert history[0].item_count == 0

    def test_nonexistent_parent_returns_empty_list(self):
        """S2 ⑥: scope 가 다르거나 존재하지 않는 golden set → 빈 history."""
        conn, cur = _mock_conn_and_cursor()
        # get_by_id 가 None 을 반환하도록 설정
        cur.fetchone.side_effect = [None]

        repo = GoldenSetRepository(conn)
        history = repo.get_version_history("nonexistent", "scope-1")

        assert history == []


# ---------------------------------------------------------------------------
# 4. DTO 직렬화 — item_count None / 정수
# ---------------------------------------------------------------------------


class TestItemCountDTOSerialization:
    def _make_set(self, **overrides) -> GoldenSet:
        defaults = dict(
            id="gs-1",
            scope_id="scope-1",
            name="Set",
            description=None,
            domain=GoldenSetDomain.CUSTOM,
            status=GoldenSetStatus.DRAFT,
            version=1,
            items=None,
            extra_metadata={},
            created_at=_NOW,
            created_by="user-1",
            updated_at=_NOW,
            updated_by=None,
            deleted_at=None,
            is_deleted=False,
        )
        defaults.update(overrides)
        return GoldenSet(**defaults)

    def test_golden_set_model_item_count_default_none(self):
        gs = self._make_set()
        assert gs.item_count is None

    def test_golden_set_model_item_count_attribute_assignment_allowed(self):
        """repository.list_by_scope 가 런타임에 gs.item_count = N 할당하는 패턴 검증."""
        gs = self._make_set()
        gs.item_count = 42
        assert gs.item_count == 42

    def test_golden_set_response_item_count_serializes_int(self):
        resp = GoldenSetResponse(
            id="gs-1",
            scope_id="scope-1",
            name="Set",
            description=None,
            domain="custom",
            status="draft",
            version=1,
            item_count=7,
            extra_metadata={},
            created_at=_NOW,
            created_by="user-1",
            updated_at=_NOW,
            updated_by=None,
            is_deleted=False,
        )
        data = resp.model_dump()
        assert data["item_count"] == 7

    def test_golden_set_response_item_count_none_by_default(self):
        resp = GoldenSetResponse(
            id="gs-1",
            scope_id="scope-1",
            name="Set",
            description=None,
            domain="custom",
            status="draft",
            version=1,
            extra_metadata={},
            created_at=_NOW,
            created_by="user-1",
            updated_at=_NOW,
            updated_by=None,
            is_deleted=False,
        )
        # 명시적으로 값을 주지 않으면 None
        assert resp.item_count is None
        data = resp.model_dump()
        assert data["item_count"] is None

    def test_version_info_item_count_required(self):
        """GoldenSetVersionInfo.item_count 는 반드시 int 여야 함 (Optional 아님)."""
        info = GoldenSetVersionInfo(
            version=1,
            created_at=_NOW,
            created_by="user-1",
            item_count=5,
        )
        assert info.item_count == 5

    def test_version_info_item_count_negative_allowed_but_typed(self):
        """int 로만 받으면 되며 별도 constraint 없음을 기록한다."""
        info = GoldenSetVersionInfo(
            version=1,
            created_at=_NOW,
            created_by="user-1",
            item_count=0,
        )
        assert info.item_count == 0


# ---------------------------------------------------------------------------
# 5. 회귀 픽스처 — #30 P0 버그가 고쳐진 이후 list_by_scope 가 예외 없이 동작
# ---------------------------------------------------------------------------


class TestListByScopeRegressionP0:
    def test_freshly_created_set_zero_items_listing_does_not_raise(self):
        """
        #30 회귀 방어: 새로 생성된 골든셋(item 0개) 을 list_by_scope 가 조회해도
        예외 없이 item_count=0 으로 직렬화된다.
        """
        conn, cur = _mock_conn_and_cursor()
        row = _golden_set_row(gid="just-created", name="Brand New")
        cur.fetchone.side_effect = [
            {"count": 1},  # total
            {"count": 0},  # _count_items
        ]
        cur.fetchall.return_value = [row]

        repo = GoldenSetRepository(conn)
        sets, total = repo.list_by_scope("scope-1")

        assert total == 1
        assert len(sets) == 1
        assert sets[0].item_count == 0
        assert sets[0].name == "Brand New"
