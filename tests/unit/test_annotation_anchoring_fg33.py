"""S3 Phase 3 FG 3-3 — annotation anchoring (orphan / 복구) 단위 테스트.

대상:
  - app.services.snapshot_sync_service.rebuild_annotation_anchoring
  - app.repositories.annotations_repository.AnnotationsRepository.mark_orphans
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.repositories.annotations_repository import AnnotationsRepository
from app.services import snapshot_sync_service as snap_mod


# ---------------------------------------------------------------------------
# rebuild_annotation_anchoring (snapshot 변환 + repo 호출 통합)
# ---------------------------------------------------------------------------

class TestRebuildAnnotationAnchoring:
    def test_extracts_node_ids_from_snapshot(self, monkeypatch):
        # nodes_from_prosemirror 가 [{id: "n1"}, {id: "n2"}] 반환 → live_node_ids = {n1, n2}
        monkeypatch.setattr(
            snap_mod, "nodes_from_prosemirror",
            lambda snapshot: [{"id": "n1"}, {"id": "n2"}],
        )
        fake_repo = MagicMock()
        fake_repo.mark_orphans.return_value = (0, 0)
        monkeypatch.setattr(
            "app.repositories.annotations_repository.annotations_repository",
            fake_repo,
        )

        result = snap_mod.rebuild_annotation_anchoring(
            conn=MagicMock(), document_id="doc-1", snapshot={},
        )
        assert result == (0, 0)
        fake_repo.mark_orphans.assert_called_once_with(
            mock_arg(), "doc-1", {"n1", "n2"},
        ) if False else None
        # 실제 검증
        call = fake_repo.mark_orphans.call_args
        assert call.args[1] == "doc-1"
        assert call.args[2] == {"n1", "n2"}

    def test_empty_snapshot_marks_all_orphan(self, monkeypatch):
        monkeypatch.setattr(snap_mod, "nodes_from_prosemirror", lambda s: [])
        fake_repo = MagicMock()
        fake_repo.mark_orphans.return_value = (5, 0)
        monkeypatch.setattr(
            "app.repositories.annotations_repository.annotations_repository",
            fake_repo,
        )
        result = snap_mod.rebuild_annotation_anchoring(
            conn=MagicMock(), document_id="doc-1", snapshot={},
        )
        assert result == (5, 0)
        # 빈 set 으로 호출
        assert fake_repo.mark_orphans.call_args.args[2] == set()

    def test_node_id_or_id_field_supported(self, monkeypatch):
        # 일부 노드는 'node_id' 키를 사용 (유연성)
        monkeypatch.setattr(
            snap_mod, "nodes_from_prosemirror",
            lambda s: [{"id": "n1"}, {"node_id": "n2"}, {"id": None}, {}],
        )
        fake_repo = MagicMock()
        fake_repo.mark_orphans.return_value = (0, 0)
        monkeypatch.setattr(
            "app.repositories.annotations_repository.annotations_repository",
            fake_repo,
        )
        snap_mod.rebuild_annotation_anchoring(
            conn=MagicMock(), document_id="doc-1", snapshot={},
        )
        # None 과 missing 키는 제외
        assert fake_repo.mark_orphans.call_args.args[2] == {"n1", "n2"}


# ---------------------------------------------------------------------------
# AnnotationsRepository.mark_orphans (SQL 직접 검증)
# ---------------------------------------------------------------------------

class _CursorStub:
    def __init__(self, rowcounts: list[int]):
        self._rowcounts = list(rowcounts)
        self.execute_calls: list[tuple[str, tuple]] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.execute_calls.append((sql, tuple(params) if params else ()))
        if self._rowcounts:
            self.rowcount = self._rowcounts.pop(0)


def _conn(stub: _CursorStub) -> MagicMock:
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=stub)
    return conn


class TestMarkOrphans:
    def test_normal_path_two_updates(self):
        stub = _CursorStub(rowcounts=[3, 1])  # newly_orphaned=3, recovered=1
        result = AnnotationsRepository().mark_orphans(
            _conn(stub), "doc-1", {"n1", "n2"},
        )
        assert result == (3, 1)
        # 두 UPDATE 호출
        assert len(stub.execute_calls) == 2
        # 1) is_orphan=true 처리 (live 에 없는 것)
        assert "is_orphan = true" in stub.execute_calls[0][0]
        assert "node_id <> ALL" in stub.execute_calls[0][0]
        # 2) is_orphan=false 처리 (다시 등장)
        assert "is_orphan = false" in stub.execute_calls[1][0]
        assert "node_id = ANY" in stub.execute_calls[1][0]

    def test_empty_live_marks_all_orphan(self):
        stub = _CursorStub(rowcounts=[7])  # 두 번째 UPDATE 는 안 일어남 (live 비어있음)
        result = AnnotationsRepository().mark_orphans(
            _conn(stub), "doc-1", set(),
        )
        assert result == (7, 0)
        # 한 번의 UPDATE 만 (모두 orphan)
        assert len(stub.execute_calls) == 1
        assert "is_orphan = true" in stub.execute_calls[0][0]
        assert "node_id <>" not in stub.execute_calls[0][0]


# helper class for placeholder reference
class mock_arg:
    pass
