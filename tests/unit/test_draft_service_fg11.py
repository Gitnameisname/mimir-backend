"""S3 Phase 1 FG 1-1 — draft_service 의 단일 정본 경로 유닛 테스트.

커버 범위:
  * save_draft 가 content_snapshot 저장 후 rebuild_nodes_from_snapshot 을 호출
  * save_draft_nodes (DEPRECATED) 가 prosemirror_from_nodes 로 변환 후
    save_draft 로 위임하는지
  * content_snapshot validator 가 비표준 포맷을 거부하는지
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


DOC_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
VER_DRAFT = "v-draft-fg11-0000-0000-000000000001"


def _doc(**kw):
    base = {
        "id": DOC_ID,
        "title": "FG1-1 문서",
        "document_type": "policy",
        "status": "draft",
        "summary": "요약",
        "metadata": {},
        "current_draft_version_id": VER_DRAFT,
        "current_published_version_id": None,
        "created_by": "author-1",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _version(**kw):
    base = {
        "id": VER_DRAFT,
        "document_id": DOC_ID,
        "version_number": 1,
        "label": None,
        "status": "draft",
        "change_summary": None,
        "source": "manual",
        "metadata": {},
        "created_by": "author-1",
        "created_at": datetime.now(timezone.utc),
        "parent_version_id": None,
        "restored_from_version_id": None,
        "title_snapshot": "t",
        "summary_snapshot": "s",
        "published_by": None,
        "published_at": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _valid_snapshot() -> dict:
    return {
        "type": "doc",
        "schema_version": 1,
        "content": [
            {
                "type": "paragraph",
                "attrs": {"node_id": "11111111-1111-4111-8111-111111111111"},
                "content": [{"type": "text", "text": "본문"}],
            }
        ],
    }


@pytest.fixture
def mocked(monkeypatch):
    from app.services import draft_service as svc_mod

    docs = MagicMock()
    vers = MagicMock()
    wf = MagicMock()
    rebuild = MagicMock(return_value=[])

    docs.get_by_id.return_value = _doc()
    docs.update_version_pointers.return_value = None
    docs.update.return_value = None

    vers.get_by_id.return_value = _version()
    vers.get_next_version_number.return_value = 2
    vers.create.return_value = _version(id="new-draft-id", version_number=2)
    vers.update_content.return_value = _version()

    wf.get_workflow_status.return_value = "draft"

    monkeypatch.setattr(svc_mod, "documents_repository", docs)
    monkeypatch.setattr(svc_mod, "versions_repository", vers)

    import app.repositories.workflow_repository as wfrepo_mod
    monkeypatch.setattr(wfrepo_mod, "workflow_repository", wf, raising=False)

    import app.services.snapshot_sync_service as snap_mod
    monkeypatch.setattr(snap_mod, "rebuild_nodes_from_snapshot", rebuild)

    return SimpleNamespace(docs=docs, vers=vers, wf=wf, rebuild=rebuild, mod=svc_mod)


# --------------------------------------------------------------------------- #
# 1) save_draft 가 nodes 동기화를 호출
# --------------------------------------------------------------------------- #


class TestSaveDraftSyncsNodes:
    def test_new_draft_calls_rebuild_with_snapshot(self, mocked):
        """신규 Draft 생성 경로에서 rebuild_nodes_from_snapshot 호출."""
        from app.services.draft_service import draft_service
        from app.schemas.versions import DraftSaveRequest

        # 현재 Draft 가 없는 상황
        mocked.docs.get_by_id.return_value = _doc(current_draft_version_id=None)

        snapshot = _valid_snapshot()
        req = DraftSaveRequest(title="t", content_snapshot=snapshot)
        conn = MagicMock()
        draft_service.save_draft(conn, DOC_ID, req, actor_id="u1")

        assert mocked.rebuild.call_count == 1
        args = mocked.rebuild.call_args[0]
        assert args[0] is conn
        assert args[1] == "new-draft-id"
        assert args[2] == snapshot

    def test_existing_draft_update_also_calls_rebuild(self, mocked):
        """기존 Draft 교체 경로에서도 rebuild 호출."""
        from app.services.draft_service import draft_service
        from app.schemas.versions import DraftSaveRequest

        snapshot = _valid_snapshot()
        req = DraftSaveRequest(title="t", content_snapshot=snapshot)
        conn = MagicMock()
        draft_service.save_draft(conn, DOC_ID, req, actor_id="u1")

        assert mocked.rebuild.call_count == 1
        assert mocked.rebuild.call_args[0][2] == snapshot


# --------------------------------------------------------------------------- #
# 2) save_draft_nodes (DEPRECATED) — save_draft 로 위임
# --------------------------------------------------------------------------- #


class TestSaveDraftNodesDelegation:
    def test_converts_nodes_to_prosemirror_and_delegates(self, mocked):
        """nodes → ProseMirror 변환 후 save_draft 호출."""
        from app.services.draft_service import draft_service
        from app.schemas.versions import DraftNodeItem, DraftNodeSaveRequest

        req = DraftNodeSaveRequest(
            title="t",
            nodes=[
                DraftNodeItem(
                    id="11111111-1111-4111-8111-111111111111",
                    node_type="paragraph",
                    order=0,
                    content="본문",
                )
            ],
        )
        conn = MagicMock()
        draft_service.save_draft_nodes(conn, DOC_ID, VER_DRAFT, req, actor_id="u1")

        # rebuild 가 save_draft 경로에서 호출됐는지 (위임 확인)
        assert mocked.rebuild.call_count == 1
        # 위임된 content_snapshot 이 표준 ProseMirror doc
        delivered_snapshot = mocked.rebuild.call_args[0][2]
        assert delivered_snapshot["type"] == "doc"
        assert delivered_snapshot["content"][0]["type"] == "paragraph"
        assert delivered_snapshot["content"][0]["attrs"]["node_id"].startswith("11111111")

    def test_rejects_mismatched_draft_version(self, mocked):
        """version_id 가 current_draft 가 아니면 409."""
        from app.services.draft_service import draft_service
        from app.schemas.versions import DraftNodeSaveRequest
        from app.api.errors.exceptions import ApiConflictError

        mocked.docs.get_by_id.return_value = _doc(
            current_draft_version_id="other-draft-id"
        )

        req = DraftNodeSaveRequest(title="t", nodes=[])
        conn = MagicMock()
        with pytest.raises(ApiConflictError):
            draft_service.save_draft_nodes(conn, DOC_ID, VER_DRAFT, req, actor_id="u1")


# --------------------------------------------------------------------------- #
# 3) content_snapshot validator 강화
# --------------------------------------------------------------------------- #


class TestContentSnapshotValidator:
    def test_accepts_standard_doc(self):
        from app.schemas.versions import DraftSaveRequest
        DraftSaveRequest(content_snapshot=_valid_snapshot())  # no raise

    def test_accepts_empty_doc_content(self):
        from app.schemas.versions import DraftSaveRequest
        DraftSaveRequest(content_snapshot={"type": "doc", "content": []})

    def test_rejects_text_root(self):
        from app.schemas.versions import DraftSaveRequest
        with pytest.raises(ValueError):
            DraftSaveRequest(content_snapshot={"type": "text", "content": "x"})

    def test_rejects_document_legacy_root(self):
        """과거 {type:"document"} 레거시 포맷은 거부한다."""
        from app.schemas.versions import DraftSaveRequest
        with pytest.raises(ValueError):
            DraftSaveRequest(
                content_snapshot={"type": "document", "children": []}
            )

    def test_rejects_missing_content_list(self):
        from app.schemas.versions import DraftSaveRequest
        with pytest.raises(ValueError):
            DraftSaveRequest(content_snapshot={"type": "doc"})

    def test_rejects_non_dict(self):
        from app.schemas.versions import DraftSaveRequest
        with pytest.raises(ValueError):
            DraftSaveRequest(content_snapshot="not a dict")  # type: ignore[arg-type]
