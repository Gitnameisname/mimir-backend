"""S3 Phase 1 FG 1-1 — agent_proposal_service 의 ProseMirror 표준 포맷 검증.

이전 구현은 ``content_snapshot = {"type": "text", "content": content}`` 를
직접 INSERT 했다. FG 1-1 에서 schemas/versions.py validator 가 ``type=="doc"``
을 강제하므로 이 포맷은 거부된다. 본 테스트는:

  * prosemirror_from_text 경유로 표준 doc 이 만들어지는지
  * rebuild_nodes_from_snapshot 이 호출되어 nodes 테이블이 동기화되는지
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def svc_and_mocks(monkeypatch):
    """agent_proposal_service 의 외부 의존을 mock 한다."""
    from app.services import agent_proposal_service as svc_mod

    # versions_repository.get_next_version_number
    versions_repo = MagicMock()
    versions_repo.get_next_version_number.return_value = 1
    monkeypatch.setattr(svc_mod, "versions_repository", versions_repo)

    rebuild = MagicMock(return_value=[])
    import app.services.snapshot_sync_service as snap_mod
    monkeypatch.setattr(snap_mod, "rebuild_nodes_from_snapshot", rebuild)

    # audit_emitter no-op
    audit = MagicMock()
    monkeypatch.setattr(svc_mod, "audit_emitter", audit, raising=False)

    svc = svc_mod.agent_proposal_service

    return SimpleNamespace(svc_mod=svc_mod, svc=svc, rebuild=rebuild, audit=audit)


def _make_conn_with_fetchone(side_effects: list) -> MagicMock:
    """cursor.fetchone() 가 side_effects 리스트를 순차 반환하는 conn mock."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.side_effect = side_effects
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    return conn


class TestAgentProposalContentSnapshotFormat:
    def test_content_snapshot_is_prosemirror_doc(self, svc_and_mocks, monkeypatch):
        """propose_draft 가 INSERT 하는 content_snapshot 이 표준 ProseMirror doc 인지."""
        svc = svc_and_mocks.svc
        mod = svc_and_mocks.svc_mod

        # _assert_agent_active / _assert_document_exists / _create_mcp_task /
        # _record_acting_on_behalf_of 을 모두 no-op 으로 교체.
        # 주의: 실 메서드는 conn 을 positional 로 받으므로 lambda 도 동일 형태로.
        monkeypatch.setattr(svc, "_assert_agent_active", lambda conn, agent_id: None)
        monkeypatch.setattr(svc, "_assert_document_exists", lambda conn, doc_id: None)
        monkeypatch.setattr(svc, "_create_mcp_task", lambda conn, **kw: "mcp-task-id")
        monkeypatch.setattr(
            svc, "_record_acting_on_behalf_of", lambda conn, **kw: None
        )

        # INSERT 된 content_snapshot 을 포획하기 위해 cursor.execute 를 spy
        executed: list[tuple] = []

        conn = MagicMock()
        cur = MagicMock()
        cur.execute.side_effect = lambda sql, params=None: executed.append((sql, params))
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False

        svc.propose_draft(
            conn,
            agent_id="agent-1",
            acting_on_behalf_of=None,
            document_id="doc-existing",
            document_type_id=None,
            title="제안",
            content="자유 텍스트 본문",
            metadata={},
            reason="테스트",
        )

        # versions INSERT 를 찾아 content_snapshot 추출
        insert_calls = [(sql, params) for sql, params in executed if "INSERT INTO versions" in sql]
        assert insert_calls, "versions INSERT 가 발생해야 한다"
        _, params = insert_calls[0]
        # 파라미터 순서: id, doc_id, version_number, label, reason, metadata, title,
        #   content_snapshot(json), created_by, now
        content_snapshot_json = params[7]
        snapshot = json.loads(content_snapshot_json)

        assert snapshot["type"] == "doc"
        assert isinstance(snapshot.get("content"), list)
        assert snapshot["content"][0]["type"] == "paragraph"
        text_run = snapshot["content"][0]["content"][0]
        assert text_run["type"] == "text"
        assert text_run["text"] == "자유 텍스트 본문"
        # node_id 가 attrs 에 부여
        assert "node_id" in snapshot["content"][0]["attrs"]

        # nodes 파생 동기화 호출됨
        assert svc_and_mocks.rebuild.call_count == 1
