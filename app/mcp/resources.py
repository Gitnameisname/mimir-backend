"""
MCP Resource URI 해석 — Phase 4 FG4.1 / FG 4-1 갱신.

URI 4 패턴 표준 (FG 4-1 §2.1.2):
  - mimir://documents/{document_id}
  - mimir://documents/{document_id}/versions/{version_id}
  - mimir://documents/{document_id}/versions/{version_id}/nodes/{node_id}
  - mimir://documents/{document_id}/versions/{version_id}/render

본 모듈의 ``MimirResource`` / ``parse_resource_uri`` 는 **node URI 전용 백워드 호환**
표면이다 (mcp_router 의 `/mcp/resources/read?uri=...` 엔드포인트가 사용). 4 패턴
일반 파싱은 ``app.mcp.uri_builder.parse_uri`` / 빌더는 ``app.mcp.uri_builder``.
"""
from __future__ import annotations

from typing import Optional

from app.mcp.uri_builder import build_node_uri, parse_uri


class MimirResource:
    """node-단위 URI 의 백워드 호환 래퍼 (FG 4-1 이전 인터페이스)."""

    def __init__(self, document_id: str, version_id: str, node_id: str) -> None:
        self.document_id = document_id
        self.version_id = version_id
        self.node_id = node_id

    @property
    def uri(self) -> str:
        return build_node_uri(self.document_id, self.version_id, self.node_id)

    @property
    def mime_type(self) -> str:
        return "text/plain"

    @property
    def description(self) -> str:
        return f"Document {self.document_id} / Node {self.node_id}"


def parse_resource_uri(uri: str) -> Optional[MimirResource]:
    """mimir:// node URI 를 파싱하여 MimirResource 를 반환한다 (백워드 호환).

    4 패턴 중 ``node`` 만 매칭 — 다른 패턴 (document / version / render) 은 None 반환.
    범용 파싱은 ``app.mcp.uri_builder.parse_uri`` 사용.

    Returns:
        MimirResource (node URI) 또는 None (URI 가 node 패턴 아니거나 잘못된 형식).
    """
    parts = parse_uri(uri)
    if parts is None or parts.kind != "node":
        return None
    assert parts.version_id is not None  # parse_uri 의 node 분기 보장
    assert parts.node_id is not None
    return MimirResource(
        document_id=parts.document_id,
        version_id=parts.version_id,
        node_id=parts.node_id,
    )
