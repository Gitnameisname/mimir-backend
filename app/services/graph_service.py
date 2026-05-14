"""
Graph service — S3 Phase 2 FG 2-4.

문서 그래프 뷰 (`GET /api/v1/documents/graph`) 의 노드/엣지 데이터를 조립한다.

데이터 소스 (FG 2-4 Pre-flight 갱신 §2.3):
  - 노드 document : `documents` (FG 2-0 scope_profile_id 필터 강제)
  - 엣지 backlink : `document_links` (FG 2-3, resolved 만)
  - 엣지 tagged + 노드 tag : `document_tags` + `tags` (FG 2-2, ?include_tag_nodes)
  - 엣지 in_collection + 노드 collection : `collection_documents` + `collections` (FG 2-1, ?include_collection_nodes)

ACL
---
documents 의 viewer scope IN 필터를 정본으로 한다. tags / collections 메타노드는
"viewer 가 볼 수 있는 documents 가 가진 메타" 로만 자연 필터됨 (S3 ⑥ 뷰 ≠ 권한).

상한
----
- nodes (document 만 카운트): default 500, max 2000
- 메타노드 (tag / collection) 는 별 상한 없음 — documents 가 bounded 면 자연 bounded
- truncated 플래그: documents 상한에 걸렸을 때 true
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional, Sequence

import psycopg2.extensions

from app.db.cursor_helpers import fetch_many_as

logger = logging.getLogger(__name__)


# 상수 단일 정본 (S2 ⑥ 하드코딩 금지 — 서비스 / 라우터 / 스키마 / 테스트 모두 본 모듈 import)
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000

NodeType = Literal["document", "tag", "collection"]
EdgeType = Literal["backlink", "tagged", "in_collection"]


@dataclass
class GraphNode:
    id: str  # node id (uuid 또는 prefix:slug)
    type: NodeType
    title: str
    document_type: Optional[str] = None  # type=document 일 때만


@dataclass
class GraphEdge:
    source: str
    target: str
    type: EdgeType


@dataclass
class GraphResponse:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool
    total_documents: int  # viewer scope 안 documents 전체 수 (truncated 검증용)


def build_graph(
    conn: psycopg2.extensions.connection,
    *,
    viewer_scope_profile_ids: Optional[Sequence[str]],
    limit: int = DEFAULT_LIMIT,
    collection_id: Optional[str] = None,
    folder_id: Optional[str] = None,
    tag_name_normalized: Optional[str] = None,
    include_tag_nodes: bool = False,
    include_collection_nodes: bool = False,
) -> GraphResponse:
    """viewer Scope 안 documents 와 그 메타데이터를 그래프 elements 로 조립.

    Args:
        conn: DB connection
        viewer_scope_profile_ids:
            ``None``: 필터 skip (admin / 내부 호출)
            ``[]``: 결과 없음
            ``[ids]``: documents.scope_profile_id IN (ids)
        limit: documents 노드 상한 (1 ~ MAX_LIMIT)
        collection_id: 특정 컬렉션의 documents 만 (선택)
        folder_id: 특정 폴더의 documents 만 (선택)
        tag_name_normalized: 특정 태그를 가진 documents 만 (선택)
        include_tag_nodes: 태그를 메타노드로 포함 (기본 false)
        include_collection_nodes: 컬렉션을 메타노드로 포함 (기본 false)

    Returns:
        GraphResponse — nodes / edges / truncated / total_documents
    """
    limit = max(1, min(limit, MAX_LIMIT))

    # 1. documents 조회 (viewer scope 필터 + 옵션 필터)
    docs = _fetch_documents(
        conn,
        viewer_scope_profile_ids=viewer_scope_profile_ids,
        limit=limit + 1,  # truncated 검증용 +1
        collection_id=collection_id,
        folder_id=folder_id,
        tag_name_normalized=tag_name_normalized,
    )
    truncated = len(docs) > limit
    docs = docs[:limit]
    doc_ids = [d["id"] for d in docs]

    # total_documents (페이지네이션 없는 전체 카운트) — truncated 일 때만 정확 보고
    total_documents = _count_documents(
        conn,
        viewer_scope_profile_ids=viewer_scope_profile_ids,
        collection_id=collection_id,
        folder_id=folder_id,
        tag_name_normalized=tag_name_normalized,
    ) if truncated else len(docs)

    nodes: list[GraphNode] = [
        GraphNode(
            id=str(d["id"]),
            type="document",
            title=d["title"] or "(제목 없음)",
            document_type=d.get("document_type"),
        )
        for d in docs
    ]
    edges: list[GraphEdge] = []

    if not doc_ids:
        return GraphResponse(nodes=nodes, edges=edges, truncated=truncated, total_documents=total_documents)

    # 2. backlinks (document_links) — from/to 모두 visible documents 안에 있는 것만
    edges.extend(_fetch_backlink_edges(conn, doc_ids=doc_ids))

    # 3. tags (옵션)
    if include_tag_nodes:
        tag_nodes, tag_edges = _fetch_tag_meta(conn, doc_ids=doc_ids)
        nodes.extend(tag_nodes)
        edges.extend(tag_edges)

    # 4. collections (옵션)
    if include_collection_nodes:
        col_nodes, col_edges = _fetch_collection_meta(conn, doc_ids=doc_ids)
        nodes.extend(col_nodes)
        edges.extend(col_edges)

    logger.info(
        "build_graph — limit=%d, docs=%d, edges=%d, truncated=%s, "
        "include_tags=%s, include_collections=%s",
        limit, len(nodes), len(edges), truncated,
        include_tag_nodes, include_collection_nodes,
    )
    return GraphResponse(
        nodes=nodes, edges=edges, truncated=truncated, total_documents=total_documents,
    )


# ---------------------------------------------------------------------------
# 내부 — documents
# ---------------------------------------------------------------------------

def _fetch_documents(
    conn: psycopg2.extensions.connection,
    *,
    viewer_scope_profile_ids: Optional[Sequence[str]],
    limit: int,
    collection_id: Optional[str],
    folder_id: Optional[str],
    tag_name_normalized: Optional[str],
) -> list[dict[str, Any]]:
    # documents 테이블은 soft-delete 컬럼 없음 — status='archived'/'deprecated' 만 그래프에서 제외.
    # search_service 의 visible_statuses 패턴과 정합 (deprecated 는 표시 안 함).
    where_parts: list[str] = ["d.status != 'archived'", "d.status != 'deprecated'"]
    params: list[Any] = []

    if viewer_scope_profile_ids is not None:
        ids = list(viewer_scope_profile_ids)
        if not ids:
            where_parts.append("1 = 0")
        else:
            placeholders = ", ".join(["%s"] * len(ids))
            where_parts.append(f"d.scope_profile_id IN ({placeholders})")
            params.extend(ids)

    joins: list[str] = []
    if collection_id is not None:
        joins.append(
            "JOIN collection_documents cd ON cd.document_id = d.id AND cd.collection_id = %s"
        )
        params.append(collection_id)
    if folder_id is not None:
        joins.append(
            "JOIN document_folder df ON df.document_id = d.id AND df.folder_id = %s"
        )
        params.append(folder_id)
    if tag_name_normalized is not None:
        joins.append(
            "JOIN document_tags dt ON dt.document_id = d.id "
            "JOIN tags t ON t.id = dt.tag_id AND t.name_normalized = %s"
        )
        params.append(tag_name_normalized)

    join_sql = "\n            ".join(joins)
    where_sql = " AND ".join(where_parts)

    sql = f"""
        SELECT d.id, d.title, d.document_type
        FROM documents d
            {join_sql}
        WHERE {where_sql}
        ORDER BY d.updated_at DESC, d.id ASC
        LIMIT %s
    """
    params.append(limit)
    return fetch_many_as(
        conn, sql, params, lambda r: {
            "id": str(r["id"]),
            "title": r["title"],
            "document_type": r.get("document_type"),
        },
    )


def _count_documents(
    conn: psycopg2.extensions.connection,
    *,
    viewer_scope_profile_ids: Optional[Sequence[str]],
    collection_id: Optional[str],
    folder_id: Optional[str],
    tag_name_normalized: Optional[str],
) -> int:
    """truncated 시 전체 documents 수 보고용. _fetch_documents 와 동일 필터."""
    # documents 테이블은 soft-delete 컬럼 없음 — status='archived'/'deprecated' 만 그래프에서 제외.
    # search_service 의 visible_statuses 패턴과 정합 (deprecated 는 표시 안 함).
    where_parts: list[str] = ["d.status != 'archived'", "d.status != 'deprecated'"]
    params: list[Any] = []

    if viewer_scope_profile_ids is not None:
        ids = list(viewer_scope_profile_ids)
        if not ids:
            where_parts.append("1 = 0")
        else:
            placeholders = ", ".join(["%s"] * len(ids))
            where_parts.append(f"d.scope_profile_id IN ({placeholders})")
            params.extend(ids)

    joins: list[str] = []
    if collection_id is not None:
        joins.append("JOIN collection_documents cd ON cd.document_id = d.id AND cd.collection_id = %s")
        params.append(collection_id)
    if folder_id is not None:
        joins.append("JOIN document_folder df ON df.document_id = d.id AND df.folder_id = %s")
        params.append(folder_id)
    if tag_name_normalized is not None:
        joins.append(
            "JOIN document_tags dt ON dt.document_id = d.id "
            "JOIN tags t ON t.id = dt.tag_id AND t.name_normalized = %s"
        )
        params.append(tag_name_normalized)

    join_sql = "\n            ".join(joins)
    where_sql = " AND ".join(where_parts)

    sql = f"""
        SELECT COUNT(*)::INT AS cnt
        FROM documents d
            {join_sql}
        WHERE {where_sql}
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return int(row["cnt"]) if row else 0


# ---------------------------------------------------------------------------
# 내부 — edges
# ---------------------------------------------------------------------------

def _fetch_backlink_edges(
    conn: psycopg2.extensions.connection,
    *,
    doc_ids: Sequence[str],
) -> list[GraphEdge]:
    """document_links 중 from/to 양쪽이 visible documents 에 속한 resolved 엣지만."""
    if not doc_ids:
        return []
    placeholders = ", ".join(["%s"] * len(doc_ids))
    sql = f"""
        SELECT dl.from_document_id, dl.to_document_id
        FROM document_links dl
        WHERE dl.resolved_status = 'resolved'
          AND dl.from_document_id IN ({placeholders})
          AND dl.to_document_id IN ({placeholders})
    """
    params = list(doc_ids) + list(doc_ids)
    return fetch_many_as(
        conn, sql, params,
        lambda r: GraphEdge(
            source=str(r["from_document_id"]),
            target=str(r["to_document_id"]),
            type="backlink",
        ),
    )


def _fetch_tag_meta(
    conn: psycopg2.extensions.connection,
    *,
    doc_ids: Sequence[str],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    if not doc_ids:
        return [], []
    placeholders = ", ".join(["%s"] * len(doc_ids))
    sql = f"""
        SELECT t.id AS tag_id, t.name_normalized, dt.document_id
        FROM document_tags dt
        JOIN tags t ON t.id = dt.tag_id
        WHERE dt.document_id IN ({placeholders})
    """
    rows = fetch_many_as(
        conn, sql, list(doc_ids),
        lambda r: {
            "tag_id": str(r["tag_id"]),
            "name": r["name_normalized"],
            "document_id": str(r["document_id"]),
        },
    )
    seen_tags: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for row in rows:
        node_id = f"tag:{row['tag_id']}"
        if node_id not in seen_tags:
            seen_tags[node_id] = GraphNode(
                id=node_id,
                type="tag",
                title=row["name"],
            )
        edges.append(
            GraphEdge(source=row["document_id"], target=node_id, type="tagged"),
        )
    return list(seen_tags.values()), edges


def _fetch_collection_meta(
    conn: psycopg2.extensions.connection,
    *,
    doc_ids: Sequence[str],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    if not doc_ids:
        return [], []
    placeholders = ", ".join(["%s"] * len(doc_ids))
    sql = f"""
        SELECT c.id AS collection_id, c.title, cd.document_id
        FROM collection_documents cd
        JOIN collections c ON c.id = cd.collection_id
        WHERE cd.document_id IN ({placeholders})
    """
    rows = fetch_many_as(
        conn, sql, list(doc_ids),
        lambda r: {
            "collection_id": str(r["collection_id"]),
            "title": r["title"],
            "document_id": str(r["document_id"]),
        },
    )
    seen_cols: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for row in rows:
        node_id = f"collection:{row['collection_id']}"
        if node_id not in seen_cols:
            seen_cols[node_id] = GraphNode(
                id=node_id,
                type="collection",
                title=row["title"] or "(제목 없음)",
            )
        edges.append(
            GraphEdge(source=row["document_id"], target=node_id, type="in_collection"),
        )
    return list(seen_cols.values()), edges


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "NodeType",
    "EdgeType",
    "GraphNode",
    "GraphEdge",
    "GraphResponse",
    "build_graph",
]
