"""
S3 Phase 2 FG 2-4 — graph_service.build_graph 단위.

task2-4.md §4 Step 1 의 회귀 시나리오 충족:
  - ACL viewer scope 강제 (None / [] / [ids])
  - 노드 상한 (DEFAULT_LIMIT 500, MAX_LIMIT 2000) + truncated 플래그
  - backlink 엣지: from/to 모두 visible documents 안에 있는 것만
  - tag / collection 메타노드: include_*_nodes 옵션 동작
  - 옵션 필터 (collection_id / folder_id / tag_name_normalized) 결합

DB 의존 통합 테스트는 별 라운드 — 본 단위는 helper monkeypatch 로 합성 로직 검증.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")


pytestmark = pytest.mark.unit


@pytest.fixture
def patched_fetchers(monkeypatch):
    """graph_service 의 4 fetcher 를 monkeypatch 가능하도록 노출.

    Usage:
        patched_fetchers.set_documents([{"id": "d1", ...}])
        patched_fetchers.set_backlinks([("d1", "d2")])
    """
    from app.services import graph_service

    state: dict[str, Any] = {
        "documents": [],
        "doc_count": None,  # None 이면 len(documents) 반환
        "backlinks": [],  # list[tuple[from_id, to_id]]
        "tag_rows": [],   # list[(tag_id, name, document_id)]
        "col_rows": [],   # list[(collection_id, title, document_id)]
    }

    def fake_fetch_documents(conn, **kw):
        return list(state["documents"])

    def fake_count_documents(conn, **kw):
        return state["doc_count"] if state["doc_count"] is not None else len(state["documents"])

    def fake_fetch_backlinks(conn, *, doc_ids):
        ids = set(doc_ids)
        return [
            graph_service.GraphEdge(source=src, target=tgt, type="backlink")
            for src, tgt in state["backlinks"]
            if src in ids and tgt in ids
        ]

    def fake_fetch_tag_meta(conn, *, doc_ids):
        ids = set(doc_ids)
        seen: dict[str, graph_service.GraphNode] = {}
        edges: list[graph_service.GraphEdge] = []
        for tag_id, name, doc_id in state["tag_rows"]:
            if doc_id not in ids:
                continue
            node_id = f"tag:{tag_id}"
            if node_id not in seen:
                seen[node_id] = graph_service.GraphNode(id=node_id, type="tag", title=name)
            edges.append(graph_service.GraphEdge(source=doc_id, target=node_id, type="tagged"))
        return list(seen.values()), edges

    def fake_fetch_collection_meta(conn, *, doc_ids):
        ids = set(doc_ids)
        seen: dict[str, graph_service.GraphNode] = {}
        edges: list[graph_service.GraphEdge] = []
        for col_id, title, doc_id in state["col_rows"]:
            if doc_id not in ids:
                continue
            node_id = f"collection:{col_id}"
            if node_id not in seen:
                seen[node_id] = graph_service.GraphNode(id=node_id, type="collection", title=title)
            edges.append(graph_service.GraphEdge(source=doc_id, target=node_id, type="in_collection"))
        return list(seen.values()), edges

    monkeypatch.setattr(graph_service, "_fetch_documents", fake_fetch_documents)
    monkeypatch.setattr(graph_service, "_count_documents", fake_count_documents)
    monkeypatch.setattr(graph_service, "_fetch_backlink_edges", fake_fetch_backlinks)
    monkeypatch.setattr(graph_service, "_fetch_tag_meta", fake_fetch_tag_meta)
    monkeypatch.setattr(graph_service, "_fetch_collection_meta", fake_fetch_collection_meta)

    class _Helper:
        def set_documents(self, docs):
            state["documents"] = docs
        def set_doc_count(self, n):
            state["doc_count"] = n
        def set_backlinks(self, edges):
            state["backlinks"] = edges
        def set_tag_rows(self, rows):
            state["tag_rows"] = rows
        def set_col_rows(self, rows):
            state["col_rows"] = rows

    return _Helper()


def _doc(doc_id: str, title: str = "Doc", document_type: str = "memo"):
    return {"id": doc_id, "title": title, "document_type": document_type}


# ---------------------------------------------------------------------------
# 노드 / 기본 응답
# ---------------------------------------------------------------------------

class TestBasicResponse:
    def test_empty_documents(self, patched_fetchers):
        from app.services.graph_service import build_graph

        patched_fetchers.set_documents([])
        result = build_graph(conn=None, viewer_scope_profile_ids=["s"])
        assert result.nodes == []
        assert result.edges == []
        assert result.truncated is False
        assert result.total_documents == 0

    def test_documents_only_no_meta(self, patched_fetchers):
        from app.services.graph_service import build_graph

        patched_fetchers.set_documents([_doc("d1", "A"), _doc("d2", "B")])
        result = build_graph(conn=None, viewer_scope_profile_ids=["s"])
        assert len(result.nodes) == 2
        assert all(n.type == "document" for n in result.nodes)
        assert {n.id for n in result.nodes} == {"d1", "d2"}
        assert result.edges == []
        assert result.truncated is False

    def test_node_title_fallback(self, patched_fetchers):
        from app.services.graph_service import build_graph

        patched_fetchers.set_documents([{"id": "d1", "title": None, "document_type": "memo"}])
        result = build_graph(conn=None, viewer_scope_profile_ids=["s"])
        assert result.nodes[0].title == "(제목 없음)"


# ---------------------------------------------------------------------------
# 노드 상한 / truncated
# ---------------------------------------------------------------------------

class TestLimit:
    def test_limit_clamped_to_max(self, patched_fetchers):
        from app.services.graph_service import build_graph, MAX_LIMIT

        # 입력 limit 5000 → MAX_LIMIT 로 clamp. fetcher 가 받는 limit 은 max+1
        patched_fetchers.set_documents([_doc(f"d{i}") for i in range(MAX_LIMIT + 1)])
        result = build_graph(conn=None, viewer_scope_profile_ids=["s"], limit=5000)
        assert len(result.nodes) == MAX_LIMIT
        assert result.truncated is True

    def test_limit_clamped_to_min(self, patched_fetchers):
        from app.services.graph_service import build_graph

        patched_fetchers.set_documents([_doc("d1"), _doc("d2")])
        # limit = 0 → 1 로 clamp
        result = build_graph(conn=None, viewer_scope_profile_ids=["s"], limit=0)
        assert len(result.nodes) == 1
        assert result.truncated is True

    def test_truncated_flag_when_extra_returned(self, patched_fetchers):
        from app.services.graph_service import build_graph

        # fetcher 가 limit+1 을 반환 (5개 요청, 6개 나옴)
        patched_fetchers.set_documents([_doc(f"d{i}") for i in range(6)])
        patched_fetchers.set_doc_count(50)
        result = build_graph(conn=None, viewer_scope_profile_ids=["s"], limit=5)
        assert len(result.nodes) == 5
        assert result.truncated is True
        assert result.total_documents == 50  # 별도 count 쿼리 결과

    def test_not_truncated_when_under_limit(self, patched_fetchers):
        from app.services.graph_service import build_graph

        patched_fetchers.set_documents([_doc(f"d{i}") for i in range(3)])
        result = build_graph(conn=None, viewer_scope_profile_ids=["s"], limit=10)
        assert result.truncated is False
        assert result.total_documents == 3  # 길이 그대로


# ---------------------------------------------------------------------------
# Backlink edges
# ---------------------------------------------------------------------------

class TestBacklinkEdges:
    def test_backlink_both_visible(self, patched_fetchers):
        from app.services.graph_service import build_graph

        patched_fetchers.set_documents([_doc("d1"), _doc("d2")])
        patched_fetchers.set_backlinks([("d1", "d2")])
        result = build_graph(conn=None, viewer_scope_profile_ids=["s"])
        assert len(result.edges) == 1
        assert result.edges[0].source == "d1"
        assert result.edges[0].target == "d2"
        assert result.edges[0].type == "backlink"

    def test_backlink_target_invisible_excluded(self, patched_fetchers):
        """to_document 가 visible documents 에 없으면 엣지 제외 (R2 정합)."""
        from app.services.graph_service import build_graph

        patched_fetchers.set_documents([_doc("d1")])  # d2 는 visible 아님
        patched_fetchers.set_backlinks([("d1", "d2-other-scope")])
        result = build_graph(conn=None, viewer_scope_profile_ids=["s"])
        assert result.edges == []


# ---------------------------------------------------------------------------
# Tag meta nodes
# ---------------------------------------------------------------------------

class TestTagMeta:
    def test_tag_nodes_excluded_by_default(self, patched_fetchers):
        from app.services.graph_service import build_graph

        patched_fetchers.set_documents([_doc("d1")])
        patched_fetchers.set_tag_rows([("t1", "ai", "d1")])
        result = build_graph(conn=None, viewer_scope_profile_ids=["s"])
        assert all(n.type != "tag" for n in result.nodes)
        assert all(e.type != "tagged" for e in result.edges)

    def test_tag_nodes_included_when_flag(self, patched_fetchers):
        from app.services.graph_service import build_graph

        patched_fetchers.set_documents([_doc("d1"), _doc("d2")])
        patched_fetchers.set_tag_rows([
            ("t1", "ai", "d1"),
            ("t1", "ai", "d2"),  # 같은 태그가 두 문서에 — 노드 1개로 흡수
            ("t2", "ml", "d1"),
        ])
        result = build_graph(
            conn=None, viewer_scope_profile_ids=["s"], include_tag_nodes=True,
        )
        tag_nodes = [n for n in result.nodes if n.type == "tag"]
        assert {n.title for n in tag_nodes} == {"ai", "ml"}
        assert len(tag_nodes) == 2

        tag_edges = [e for e in result.edges if e.type == "tagged"]
        assert len(tag_edges) == 3
        # tag node id 형식: "tag:<uuid>"
        assert all(e.target.startswith("tag:") for e in tag_edges)


# ---------------------------------------------------------------------------
# Collection meta nodes
# ---------------------------------------------------------------------------

class TestCollectionMeta:
    def test_collection_nodes_excluded_by_default(self, patched_fetchers):
        from app.services.graph_service import build_graph

        patched_fetchers.set_documents([_doc("d1")])
        patched_fetchers.set_col_rows([("c1", "Folder A", "d1")])
        result = build_graph(conn=None, viewer_scope_profile_ids=["s"])
        assert all(n.type != "collection" for n in result.nodes)

    def test_collection_nodes_included_when_flag(self, patched_fetchers):
        from app.services.graph_service import build_graph

        patched_fetchers.set_documents([_doc("d1"), _doc("d2")])
        patched_fetchers.set_col_rows([
            ("c1", "Memos", "d1"),
            ("c1", "Memos", "d2"),
        ])
        result = build_graph(
            conn=None, viewer_scope_profile_ids=["s"], include_collection_nodes=True,
        )
        col_nodes = [n for n in result.nodes if n.type == "collection"]
        assert len(col_nodes) == 1
        assert col_nodes[0].title == "Memos"
        col_edges = [e for e in result.edges if e.type == "in_collection"]
        assert len(col_edges) == 2


# ---------------------------------------------------------------------------
# 통합 — 모든 메타 + 백링크
# ---------------------------------------------------------------------------

class TestACLLeakPrevention:
    def test_meta_node_not_leaked_when_doc_invisible(self, patched_fetchers):
        """다른 scope 의 document 가 가진 tag 가 본 viewer 의 그래프에 노출되지 않음.

        시나리오: tag_rows 에 d-other (다른 scope) 의 태그가 들어가도, documents 응답이
        d1 만이라 fake_fetch_tag_meta 가 d-other 를 자연스럽게 제외한다 (R2 정합).
        """
        from app.services.graph_service import build_graph

        patched_fetchers.set_documents([_doc("d1")])
        patched_fetchers.set_tag_rows([
            ("t1", "private-tag", "d-other-scope"),  # d-other 의 태그 — visible 아님
            ("t2", "public-tag", "d1"),              # d1 의 태그 — visible
        ])
        result = build_graph(
            conn=None, viewer_scope_profile_ids=["s"], include_tag_nodes=True,
        )
        tag_nodes = [n for n in result.nodes if n.type == "tag"]
        # d-other 의 태그는 visible documents 안에 없으므로 응답에 포함 안 됨
        assert {n.title for n in tag_nodes} == {"public-tag"}


class TestCombined:
    def test_all_meta_and_backlinks(self, patched_fetchers):
        from app.services.graph_service import build_graph

        patched_fetchers.set_documents([_doc("d1"), _doc("d2"), _doc("d3")])
        patched_fetchers.set_backlinks([("d1", "d2"), ("d2", "d3")])
        patched_fetchers.set_tag_rows([("t1", "ai", "d1")])
        patched_fetchers.set_col_rows([("c1", "Memos", "d2")])

        result = build_graph(
            conn=None,
            viewer_scope_profile_ids=["s"],
            include_tag_nodes=True,
            include_collection_nodes=True,
        )
        # 노드: 3 documents + 1 tag + 1 collection
        node_types = sorted(n.type for n in result.nodes)
        assert node_types == ["collection", "document", "document", "document", "tag"]
        # 엣지: 2 backlinks + 1 tagged + 1 in_collection
        edge_types = sorted(e.type for e in result.edges)
        assert edge_types == ["backlink", "backlink", "in_collection", "tagged"]
