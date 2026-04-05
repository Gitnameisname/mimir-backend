"""
Nodes persistence repository.

책임:
  - nodes 테이블에 대한 SQL CRUD 실행
  - DB row(RealDictRow) → Node 도메인 모델 변환
  - version-node 관계 무결성 접근

설계 원칙:
  - 조회 전용 (read model) — 노드 수정은 이번 단계 범위 밖.
  - bulk_create_for_version: 버전 생성과 같은 트랜잭션 내에서 호출.
  - flat list 응답 (parent_id + order_index로 클라이언트에서 트리 재구성).
"""

import json
import logging
from typing import Any, Optional
from uuid import UUID

import psycopg2.extensions

from app.models.node import Node

logger = logging.getLogger(__name__)


def _row_to_node(row: dict[str, Any]) -> Node:
    """DB row(RealDictRow) → Node 도메인 모델 변환."""
    return Node(
        id=str(row["id"]),
        version_id=str(row["version_id"]),
        parent_id=str(row["parent_id"]) if row.get("parent_id") else None,
        node_type=row["node_type"],
        order_index=row["order_index"],
        title=row.get("title"),
        content=row.get("content"),
        metadata=row["metadata"] if row["metadata"] is not None else {},
        created_at=row["created_at"],
    )


class NodesRepository:
    """Nodes 테이블 접근 repository."""

    def bulk_create_for_version(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
        node_items: list[dict[str, Any]],
    ) -> list[Node]:
        """버전에 속할 노드를 일괄 생성한다.

        node_items: list of dicts with keys:
            parent_id (Optional[str]), node_type, order_index,
            title, content, metadata

        트랜잭션 경계: 호출자(서비스)의 get_db() 컨텍스트 내에서 실행.
        """
        if not node_items:
            return []

        created: list[Node] = []
        sql = """
            INSERT INTO nodes
                (version_id, parent_id, node_type, order_index, title, content, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, version_id, parent_id, node_type, order_index,
                      title, content, metadata, created_at
        """
        with conn.cursor() as cur:
            for item in node_items:
                cur.execute(
                    sql,
                    (
                        version_id,
                        item.get("parent_id"),
                        item.get("node_type", "paragraph"),
                        item.get("order_index", 0),
                        item.get("title"),
                        item.get("content"),
                        json.dumps(item.get("metadata", {}), ensure_ascii=False),
                    ),
                )
                row = cur.fetchone()
                created.append(_row_to_node(dict(row)))
        return created

    def list_by_version_id(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
    ) -> list[Node]:
        """버전에 속한 노드 목록을 order_index ASC로 반환한다."""
        sql = """
            SELECT id, version_id, parent_id, node_type, order_index,
                   title, content, metadata, created_at
            FROM nodes
            WHERE version_id = %s
            ORDER BY order_index ASC, id ASC
        """
        with conn.cursor() as cur:
            cur.execute(sql, (version_id,))
            rows = cur.fetchall()
        return [_row_to_node(dict(row)) for row in rows]

    def get_by_id_and_version_id(
        self,
        conn: psycopg2.extensions.connection,
        node_id: str,
        version_id: str,
    ) -> Optional[Node]:
        """node_id와 version_id를 함께 검증해 단건 조회한다.

        node가 다른 version에 속한 경우 None을 반환 → 호출자가 not found로 처리.
        """
        sql = """
            SELECT id, version_id, parent_id, node_type, order_index,
                   title, content, metadata, created_at
            FROM nodes
            WHERE id = %s AND version_id = %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (node_id, version_id))
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_node(dict(row))


# 모듈 수준 싱글턴
nodes_repository = NodesRepository()
