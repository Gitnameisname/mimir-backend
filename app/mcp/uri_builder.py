"""
mimir:// URI 빌더 + parser — S3 Phase 4 FG 4-1 §2.1.2.

URI 4 패턴 (확정):

  | 대상         | 패턴                                                                 |
  |--------------|----------------------------------------------------------------------|
  | 문서         | mimir://documents/{document_id}                                      |
  | 특정 버전    | mimir://documents/{document_id}/versions/{version_id}                |
  | 특정 노드    | mimir://documents/{document_id}/versions/{version_id}/nodes/{node_id}|
  | 렌더링 텍스트| mimir://documents/{document_id}/versions/{version_id}/render         |

R3 (pinned 강제): URI 자체에는 ``latest`` 가 들어가지 않는다.
``latest`` 입력은 ``resolve_latest_version`` 에서 published 버전 id 로 변환한 후
URI 를 빌드한다. 외부에 노출되는 URI 는 항상 구체 ``version_id``.

본 모듈은 도구 함수와 라우터의 단일 정본 — 라우터에서 문자열 인라인으로
mimir:// URI 를 만들지 말 것 (§2.3 하드코딩 금지).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_doc_uri(document_id: str) -> str:
    """``mimir://documents/{document_id}``."""
    if not document_id:
        raise ValueError("document_id 가 비어있습니다.")
    return f"mimir://documents/{document_id}"


def build_version_uri(document_id: str, version_id: str) -> str:
    """``mimir://documents/{document_id}/versions/{version_id}``.

    R3: ``version_id`` 가 ``"latest"`` 이면 거부 (먼저 resolve_latest_version 호출 필요).
    """
    if not document_id:
        raise ValueError("document_id 가 비어있습니다.")
    if not version_id:
        raise ValueError("version_id 가 비어있습니다.")
    if version_id == "latest":
        raise ValueError(
            "URI 에 'latest' 직접 사용 금지 (R3 — pinned). resolve_latest_version 으로 먼저 변환하세요."
        )
    return f"mimir://documents/{document_id}/versions/{version_id}"


def build_node_uri(document_id: str, version_id: str, node_id: str) -> str:
    """``mimir://documents/{document_id}/versions/{version_id}/nodes/{node_id}``."""
    if not node_id:
        raise ValueError("node_id 가 비어있습니다.")
    base = build_version_uri(document_id, version_id)
    return f"{base}/nodes/{node_id}"


def build_render_uri(document_id: str, version_id: str) -> str:
    """``mimir://documents/{document_id}/versions/{version_id}/render``."""
    base = build_version_uri(document_id, version_id)
    return f"{base}/render"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MimirUriParts:
    """파싱된 mimir:// URI 의 구성 요소.

    어느 패턴인지에 따라 일부 필드가 None — kind 로 분기.
    """

    kind: str  # "document" | "version" | "node" | "render"
    document_id: str
    version_id: Optional[str] = None
    node_id: Optional[str] = None


# 4 패턴 (구체적인 패턴부터 매칭)
_NODE_RE = re.compile(
    r"^mimir://documents/([^/]+)/versions/([^/]+)/nodes/([^/]+)$"
)
_RENDER_RE = re.compile(
    r"^mimir://documents/([^/]+)/versions/([^/]+)/render$"
)
_VERSION_RE = re.compile(
    r"^mimir://documents/([^/]+)/versions/([^/]+)$"
)
_DOC_RE = re.compile(
    r"^mimir://documents/([^/]+)$"
)


def parse_uri(uri: str) -> Optional[MimirUriParts]:
    """``mimir://`` URI 를 파싱하여 구성 요소를 반환한다.

    URI 가 형식에 맞지 않으면 ``None`` (호출자가 4xx 응답).
    """
    if not uri:
        return None
    s = uri.strip()
    m = _NODE_RE.match(s)
    if m:
        return MimirUriParts(
            kind="node",
            document_id=m.group(1),
            version_id=m.group(2),
            node_id=m.group(3),
        )
    m = _RENDER_RE.match(s)
    if m:
        return MimirUriParts(
            kind="render",
            document_id=m.group(1),
            version_id=m.group(2),
        )
    m = _VERSION_RE.match(s)
    if m:
        return MimirUriParts(
            kind="version",
            document_id=m.group(1),
            version_id=m.group(2),
        )
    m = _DOC_RE.match(s)
    if m:
        return MimirUriParts(
            kind="document",
            document_id=m.group(1),
        )
    return None


# ---------------------------------------------------------------------------
# latest resolve (R3 — pinned)
# ---------------------------------------------------------------------------


def resolve_latest_version(conn, document_id: str) -> Optional[str]:
    """``"latest"`` 를 현재 published 버전 id (UUID 문자열) 로 변환.

    Returns:
        - 구체 version_id (UUID str) 또는 None (published 버전 부재).

    호출자 책임:
        - input 이 ``"latest"`` 일 때만 호출 (그 외 값은 그대로 사용).
        - None 반환 시 적절한 4xx 응답 (예: NOT_FOUND).
    """
    from app.repositories.versions_repository import VersionsRepository  # 지연 import

    repo = VersionsRepository()
    version = repo.get_current_published(conn, document_id)
    if version is None:
        return None
    return str(version.id)


def resolve_version_id(conn, document_id: str, version_id: Optional[str]) -> Optional[str]:
    """``version_id`` 가 ``"latest"`` 또는 None 이면 published 버전으로 resolve, 그 외엔 그대로.

    Phase 4 FG 4-1 R3 게이트: 외부에 저장/응답되는 URI 는 항상 구체 version_id.
    """
    if version_id and version_id != "latest":
        return version_id
    return resolve_latest_version(conn, document_id)
