"""
Nodes 서비스 계층 (조회 전용 — read model).

책임:
  - version 존재 여부 검증
  - node-version 관계 검증
  - flat structured node 목록/단건 반환

설계 원칙:
  - 현재 단계에서 node 수정 기능은 범위 밖.
  - node는 version의 하위 리소스로만 접근한다.
  - 이후 tree projection / AI RAG citation-friendly 조회 확장 가능성 유지.
"""

import logging
from typing import Optional

import psycopg2.extensions

from app.api.errors.exceptions import ApiNotFoundError
from app.models.node import Node
from app.repositories.nodes_repository import nodes_repository
from app.repositories.versions_repository import versions_repository
from app.schemas.nodes import NodeResponse

logger = logging.getLogger(__name__)


def _to_response(node: Node) -> NodeResponse:
    """Node 도메인 모델 → NodeResponse DTO 변환."""
    return NodeResponse(
        id=node.id,
        version_id=node.version_id,
        parent_id=node.parent_id,
        node_type=node.node_type,
        order_index=node.order_index,
        title=node.title,
        content=node.content,
        metadata=node.metadata,
        created_at=node.created_at,
    )


class NodesService:
    """Nodes 조회 서비스."""

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    def list_nodes(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
    ) -> list[NodeResponse]:
        """버전에 속한 노드 목록을 flat list로 반환한다.

        version 존재 여부를 먼저 검증한다.
        """
        version = versions_repository.get_by_id(conn, version_id)
        if version is None:
            raise ApiNotFoundError(f"Version '{version_id}' not found")

        nodes = nodes_repository.list_by_version_id(conn, version_id)
        return [_to_response(node) for node in nodes]

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    def get_node(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
        node_id: str,
    ) -> NodeResponse:
        """특정 버전의 특정 노드를 조회한다.

        - version 존재 검증
        - node-version 관계 검증 (다른 version의 node 접근 방지)
        """
        version = versions_repository.get_by_id(conn, version_id)
        if version is None:
            raise ApiNotFoundError(f"Version '{version_id}' not found")

        node = nodes_repository.get_by_id_and_version_id(conn, node_id, version_id)
        if node is None:
            raise ApiNotFoundError(
                f"Node '{node_id}' not found in version '{version_id}'"
            )
        return _to_response(node)


# 모듈 수준 싱글턴
nodes_service = NodesService()
