"""
MCP Resource URI 해석 — Phase 4 FG4.1.

URI 스킴: mimir://documents/{document_id}/versions/{version_id}/nodes/{node_id}
"""
from __future__ import annotations

import re
from typing import Optional

_RESOURCE_PATTERN = re.compile(
    r"^mimir://documents/([^/]+)/versions/([^/]+)/nodes/([^/]+)$"
)


class MimirResource:
    def __init__(self, document_id: str, version_id: str, node_id: str) -> None:
        self.document_id = document_id
        self.version_id = version_id
        self.node_id = node_id

    @property
    def uri(self) -> str:
        return f"mimir://documents/{self.document_id}/versions/{self.version_id}/nodes/{self.node_id}"

    @property
    def mime_type(self) -> str:
        return "text/plain"

    @property
    def description(self) -> str:
        return f"Document {self.document_id} / Node {self.node_id}"


def parse_resource_uri(uri: str) -> Optional[MimirResource]:
    """mimir:// URI를 파싱하여 MimirResource를 반환한다.

    Returns:
        MimirResource 또는 URI가 올바르지 않으면 None
    """
    m = _RESOURCE_PATTERN.match(uri.strip())
    if not m:
        return None
    return MimirResource(
        document_id=m.group(1),
        version_id=m.group(2),
        node_id=m.group(3),
    )
