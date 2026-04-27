"""snapshot_sync_service — content_snapshot(ProseMirror doc) ↔ nodes 양방향 변환·동기화.

Phase 1 FG 1-1 에서 도입.

목적
----
현재 Mimir 는 두 개의 저장 포맷이 공존한다:

  * ``versions.content_snapshot`` (JSONB, ProseMirror doc)
  * ``nodes`` 테이블 (flat list, parent_id 로 트리 표현)

Phase 1 FG 1-1 (D1) 결정에 따라 ``content_snapshot`` 을 **단일 정본** 으로 확정하고,
``nodes`` 는 서버 사이드에서 자동 파생되는 **동기화 테이블** 로 재정의한다.

본 모듈은 두 포맷 간 양방향 변환기를 제공한다. 기존
``vectorization_service._parse_nodes_from_snapshot`` 의 핵심 로직을 정규 경로로
승격하고, 중첩 section / list 등 트리 구조까지 다룬다.

공개 API
--------
* :func:`prosemirror_from_nodes` — flat nodes → ProseMirror doc
* :func:`nodes_from_prosemirror`  — ProseMirror doc → flat node dicts
* :func:`rebuild_nodes_from_snapshot` — snapshot 으로부터 nodes 테이블 재작성
* :func:`prosemirror_from_text` — 순수 텍스트 → 최소 유효 ProseMirror doc
* :func:`is_valid_prosemirror_doc` — schemas/versions.py validator 용

원칙
----
* DocumentType 별 분기 로직을 이 모듈에 두지 않는다 (S1 ① hardcoding 금지).
* node_id 는 **최대한 보존** — 없을 때만 ``uuid4()`` 를 생성한다.
* list 는 flat nodes 포맷에서 ``content`` 의 줄 단위로 저장되지만, ProseMirror
  에서는 ``bulletList / listItem / paragraph`` 트리로 표현한다. 이 왕복 변환 시
  listItem 수준에는 별도 node_id 를 부여하지 않고 bulletList 에만 node_id 를
  유지한다 — 인라인 주석 anchor 가 list 단위로 걸리도록 한다.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional
from uuid import UUID, uuid4
from app.utils.converters import uuid_str_or_none

logger = logging.getLogger(__name__)

# content_snapshot schema version — 마이그레이션 시 인식용
CONTENT_SNAPSHOT_SCHEMA_VERSION = 1

# nodes.node_type 어휘 (S1 ① 하드코딩 금지 원칙 상 DocumentType schema 에서
# 허용 타입을 가져오는 것이 이상적이지만, Phase 1 에서는 현행 에디터가 쓰는
# 5개 타입을 ProseMirror 표준 노드와 매핑하는 고정 테이블만 제공한다.
# Phase 2 이후 DocumentType schema 플러그인으로 확장 가능.)

# flat node_type → ProseMirror node type
_NODE_TO_PM: dict[str, str] = {
    "section": "section",       # 커스텀 블록 (FG 1-2 에서 TipTap 커스텀 NodeView 로 구현)
    "heading": "heading",
    "paragraph": "paragraph",
    "list": "bulletList",
    "code_block": "codeBlock",
}

# ProseMirror node type → flat node_type (역 매핑)
_PM_TO_NODE: dict[str, str] = {
    "section": "section",
    "heading": "heading",
    "paragraph": "paragraph",
    "bulletList": "list",
    "orderedList": "list",
    "codeBlock": "code_block",
}


# ---------------------------------------------------------------------------
# 유효성 검증
# ---------------------------------------------------------------------------

def is_valid_prosemirror_doc(snapshot: Any) -> bool:
    """content_snapshot 이 ProseMirror doc 최소 규격을 만족하는지 검사.

    규격:
      * dict 여야 함
      * ``type == "doc"``
      * ``content`` 필드가 list (빈 리스트 허용 — 빈 문서)

    ``schema_version`` 은 필수 아님. 없으면 레거시 포맷으로 간주한다.
    """
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("type") != "doc":
        return False
    content = snapshot.get("content")
    if not isinstance(content, list):
        return False
    return True


# ---------------------------------------------------------------------------
# flat nodes → ProseMirror doc
# ---------------------------------------------------------------------------

def _ensure_node_id(raw_id: Any) -> str:
    """UUID 문자열을 반환. 입력이 비었거나 유효하지 않으면 새로 생성한다."""
    if raw_id is None:
        return str(uuid4())
    if isinstance(raw_id, UUID):
        return str(raw_id)
    if isinstance(raw_id, str) and raw_id:
        try:
            return str(UUID(raw_id))
        except (ValueError, AttributeError):
            pass
    return str(uuid4())


def _text_run(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _node_text_children(text: Optional[str]) -> list[dict[str, Any]]:
    """텍스트가 있으면 ``[{type:"text", text}]``, 없으면 빈 리스트."""
    if text is None or text == "":
        return []
    return [_text_run(text)]


def _flat_to_pm_block(node: dict[str, Any], children: list[dict[str, Any]]) -> dict[str, Any]:
    """단일 flat node + 이미 변환된 children 을 ProseMirror block 으로 만든다."""
    node_type = node.get("node_type") or "paragraph"
    node_id = _ensure_node_id(node.get("id"))
    title = node.get("title")
    content = node.get("content")
    metadata = node.get("metadata") or {}

    pm_type = _NODE_TO_PM.get(node_type, "paragraph")

    if pm_type == "section":
        # section: title 은 attrs 에 담고 children 을 content 로
        return {
            "type": "section",
            "attrs": {
                "node_id": node_id,
                "title": title or "",
                **{k: v for k, v in metadata.items() if k not in {"node_id", "title"}},
            },
            "content": children,
        }

    if pm_type == "heading":
        # heading: flat 의 content 또는 title 중 우선순위로 텍스트 추출
        text = content if content is not None else title
        level = 2
        if isinstance(metadata, dict) and isinstance(metadata.get("level"), int):
            level = metadata["level"]
        return {
            "type": "heading",
            "attrs": {"node_id": node_id, "level": level},
            "content": _node_text_children(text),
        }

    if pm_type == "bulletList":
        # list: content 를 줄 단위로 분해해 listItem(paragraph) 로 싼다
        lines = (content or "").splitlines() if content else []
        lines = [ln.strip() for ln in lines if ln.strip()]
        list_items: list[dict[str, Any]] = []
        for ln in lines:
            list_items.append({
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": _node_text_children(ln),
                }],
            })
        if not list_items:
            # 빈 리스트도 최소 1개 listItem 이 있어야 ProseMirror schema 가 통과
            list_items.append({
                "type": "listItem",
                "content": [{"type": "paragraph", "content": []}],
            })
        return {
            "type": "bulletList",
            "attrs": {"node_id": node_id},
            "content": list_items,
        }

    if pm_type == "codeBlock":
        language = None
        if isinstance(metadata, dict):
            language = metadata.get("language")
        return {
            "type": "codeBlock",
            "attrs": {"node_id": node_id, "language": language},
            "content": _node_text_children(content or ""),
        }

    # 기본: paragraph
    return {
        "type": "paragraph",
        "attrs": {"node_id": node_id},
        "content": _node_text_children(content or ""),
    }


def prosemirror_from_nodes(nodes: Iterable[Any]) -> dict[str, Any]:
    """flat nodes (dict 또는 pydantic model) 를 ProseMirror doc 으로 변환한다.

    * ``id``, ``parent_id``, ``node_type``, ``order`` 또는 ``order_index``,
      ``title``, ``content``, ``metadata`` 키를 인식한다.
    * parent_id 로 트리를 재구성하고, 각 층을 order(또는 order_index) ASC 로 정렬한다.
    * 루트 노드들이 ``doc.content`` 가 된다.
    """
    node_dicts: list[dict[str, Any]] = []
    for raw in nodes:
        if raw is None:
            continue
        if hasattr(raw, "model_dump"):
            node_dicts.append(raw.model_dump())
        elif isinstance(raw, dict):
            node_dicts.append(dict(raw))
        else:
            node_dicts.append({
                "id": getattr(raw, "id", None),
                "parent_id": getattr(raw, "parent_id", None),
                "node_type": getattr(raw, "node_type", "paragraph"),
                "order_index": getattr(raw, "order_index", getattr(raw, "order", 0)),
                "title": getattr(raw, "title", None),
                "content": getattr(raw, "content", None),
                "metadata": getattr(raw, "metadata", {}),
            })

    # 정규화: 각 노드에 id / order_index 보정
    for n in node_dicts:
        n["id"] = _ensure_node_id(n.get("id"))
        # 프런트는 ``order`` 로 보낼 수도 있음
        if "order_index" not in n:
            n["order_index"] = n.get("order", 0)

    # parent_id 별로 children 모으기 (string key 로 통일)
    children_by_parent: dict[Optional[str], list[dict[str, Any]]] = {}
    for n in node_dicts:
        pid_raw = n.get("parent_id")
        pid = uuid_str_or_none(pid_raw)
        children_by_parent.setdefault(pid, []).append(n)

    def _build(parent_id: Optional[str]) -> list[dict[str, Any]]:
        group = children_by_parent.get(parent_id, [])
        group_sorted = sorted(group, key=lambda n: (n.get("order_index", 0), n.get("id", "")))
        result: list[dict[str, Any]] = []
        for node in group_sorted:
            child_blocks = _build(node["id"])
            result.append(_flat_to_pm_block(node, child_blocks))
        return result

    doc_content = _build(None)
    return {
        "type": "doc",
        "schema_version": CONTENT_SNAPSHOT_SCHEMA_VERSION,
        "content": doc_content,
    }


# ---------------------------------------------------------------------------
# ProseMirror doc → flat nodes
# ---------------------------------------------------------------------------

def _collect_text(pm_node: dict[str, Any]) -> str:
    """ProseMirror 노드의 text 자식을 문자열로 이어붙인다."""
    if not isinstance(pm_node, dict):
        return ""
    parts: list[str] = []
    for child in pm_node.get("content") or []:
        if not isinstance(child, dict):
            continue
        if child.get("type") == "text":
            parts.append(child.get("text") or "")
        else:
            parts.append(_collect_text(child))
    return "".join(parts)


def _pm_block_to_flat(
    pm_node: dict[str, Any],
    parent_id: Optional[str],
    order_index: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """단일 ProseMirror block → (flat_node, child_pm_nodes_to_recurse).

    child_pm_nodes_to_recurse 는 "이 노드의 children 중 추가 재귀가 필요한 것"
    이다. paragraph/heading/codeBlock 처럼 text 컨테이너 성격 노드는 children 을
    flat 로 분해하지 않는다. section / list 는 하위 블록을 계속 재귀한다.
    """
    pm_type = pm_node.get("type", "paragraph")
    flat_type = _PM_TO_NODE.get(pm_type, "paragraph")
    attrs = pm_node.get("attrs") or {}
    node_id = _ensure_node_id(attrs.get("node_id"))

    # metadata: attrs 중 node_id/title/level/language 는 테이블 전용 필드 이므로
    # metadata 에 넣지 않고, 나머지만 보관한다.
    _reserved = {"node_id", "title", "level", "language"}
    metadata: dict[str, Any] = {
        k: v for k, v in attrs.items() if k not in _reserved
    }

    title: Optional[str] = None
    content: Optional[str] = None
    recurse_children: list[dict[str, Any]] = []

    if pm_type == "section":
        title = attrs.get("title") or None
        # children 그대로 재귀
        for child in pm_node.get("content") or []:
            if isinstance(child, dict):
                recurse_children.append(child)
    elif pm_type == "heading":
        content = _collect_text(pm_node) or None
        if isinstance(attrs.get("level"), int):
            metadata["level"] = attrs["level"]
    elif pm_type in {"bulletList", "orderedList"}:
        # listItem > paragraph 구조에서 텍스트만 줄 단위로 추출
        lines: list[str] = []
        for item in pm_node.get("content") or []:
            if not isinstance(item, dict):
                continue
            # listItem 안에 paragraph 들이 있을 수 있음 → 모두 합쳐 한 줄
            text = _collect_text(item).strip()
            if text:
                lines.append(text)
        content = "\n".join(lines) if lines else None
    elif pm_type == "codeBlock":
        content = _collect_text(pm_node) or None
        if attrs.get("language") is not None:
            metadata["language"] = attrs["language"]
    else:
        # paragraph 및 기타
        content = _collect_text(pm_node) or None

    flat = {
        "id": node_id,
        "parent_id": parent_id,
        "node_type": flat_type,
        "order_index": order_index,
        "title": title,
        "content": content,
        "metadata": metadata,
    }
    return flat, recurse_children


def nodes_from_prosemirror(snapshot: Any) -> list[dict[str, Any]]:
    """ProseMirror doc 을 flat nodes list 로 변환한다.

    * 각 node 는 ``id``, ``parent_id``, ``node_type``, ``order_index``,
      ``title``, ``content``, ``metadata`` 키를 갖는다.
    * 입력이 유효하지 않으면 빈 리스트를 반환한다.
    """
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (ValueError, TypeError):
            return []

    if not is_valid_prosemirror_doc(snapshot):
        return []

    result: list[dict[str, Any]] = []

    def _walk(pm_nodes: list[Any], parent_id: Optional[str]) -> None:
        order = 0
        for pm in pm_nodes:
            if not isinstance(pm, dict):
                continue
            flat, children = _pm_block_to_flat(pm, parent_id, order)
            result.append(flat)
            if children:
                _walk(children, flat["id"])
            order += 1

    _walk(snapshot.get("content") or [], None)
    return result


# ---------------------------------------------------------------------------
# 동기화 (쓰기)
# ---------------------------------------------------------------------------

def rebuild_nodes_from_snapshot(
    conn: Any,
    version_id: str,
    snapshot: Any,
) -> list[dict[str, Any]]:
    """주어진 snapshot 을 flat nodes 로 변환해 nodes 테이블을 replace 한다.

    반환값은 삽입된 flat node dict list. snapshot 이 유효하지 않으면 nodes
    테이블을 비우고 빈 리스트를 반환한다.

    부작용:
      * ``nodes_repository.replace_for_version`` 호출로 기존 노드는 모두 삭제된다.
      * 호출자 측 트랜잭션 (``with get_db() as conn``) 안에서 실행되어야 한다.
    """
    # 순환 import 방지: 함수 내부에서 import
    from app.repositories.nodes_repository import nodes_repository

    flat_nodes = nodes_from_prosemirror(snapshot)
    nodes_repository.replace_for_version(conn, version_id, flat_nodes)
    logger.info(
        "content_snapshot → nodes 동기화 완료 (version_id=%s, node_count=%d)",
        version_id, len(flat_nodes),
    )
    return flat_nodes


def rebuild_tags_for_document(
    conn: Any,
    document_id: str,
    snapshot: Any,
    metadata: Optional[dict[str, Any]],
) -> list[tuple[str, str]]:
    """content_snapshot + documents.metadata 에서 태그를 추출해
    ``document_tags`` 테이블을 **replace** 한다 (S3 Phase 2 FG 2-2).

    블로커1 결정서 §3.2 "서버 파서가 정본" 규약에 따라 쓰기 경로
    (``save_draft`` / ``publish_version`` / agent 승인) 에서
    ``rebuild_nodes_from_snapshot`` 직후 호출된다.

    작업지시서 상호검증 §7 트랜잭션 규약:
      1) ``rebuild_nodes_from_snapshot``  (FG 1-1)
      2) ``rebuild_tags_for_document``     (FG 2-2, **이 함수**)
      3) ``rebuild_wikilinks_for_document`` (FG 2-3, 후속)
    세 함수 모두 동일 ``with get_db() as conn:`` 블록에서 호출되어야 한다.

    Returns:
        실제 연결된 ``[(name_normalized, source), ...]`` 목록.
    """
    # 순환 import 방지
    from app.repositories.tags_repository import tags_repository
    from app.services.tag_rules import extract_tags_from_snapshot

    extracted = extract_tags_from_snapshot(snapshot, metadata)
    if not extracted:
        tags_repository.replace_for_document(
            conn, document_id=document_id, assignments=[],
        )
        logger.info(
            "rebuild_tags_for_document — document_id=%s, tags=0 (cleared)",
            document_id,
        )
        return []

    names = [name for name, _src in extracted]
    name_to_id = tags_repository.upsert_many(conn, names)

    assignments: list[tuple[str, str]] = []
    for name, source in extracted:
        tag_id = name_to_id.get(name)
        if tag_id is None:
            continue
        assignments.append((tag_id, source))

    tags_repository.replace_for_document(
        conn, document_id=document_id, assignments=assignments,
    )
    logger.info(
        "rebuild_tags_for_document — document_id=%s, tags=%d",
        document_id, len(assignments),
    )
    return extracted


def rebuild_annotation_anchoring(
    conn: Any,
    document_id: str,
    snapshot: Any,
) -> tuple[int, int]:
    """S3 Phase 3 FG 3-3 — annotation 의 node_id anchoring 재계산.

    snapshot 에서 살아있는 node_id 집합을 추출해
    ``annotations_repository.mark_orphans`` 에 위임:
      - 살아있지 않은 node_id 의 annotation → is_orphan=true
      - 다시 살아난 node_id 의 annotation → orphan 해제 (is_orphan=false)

    호출 시점: ``rebuild_nodes_from_snapshot`` / ``rebuild_tags_for_document`` 직후
    (같은 트랜잭션 안). 별도 시점 호출도 안전 (idempotent).

    Returns:
        (newly_orphaned_count, recovered_count)
    """
    # 순환 import 회피
    from app.repositories.annotations_repository import annotations_repository

    flat_nodes = nodes_from_prosemirror(snapshot)
    live_node_ids: set[str] = set()
    for n in flat_nodes:
        nid = n.get("id") or n.get("node_id")
        if nid:
            live_node_ids.add(str(nid))
    newly_orphaned, recovered = annotations_repository.mark_orphans(
        conn, document_id, live_node_ids,
    )
    if newly_orphaned or recovered:
        logger.info(
            "rebuild_annotation_anchoring — document_id=%s, newly_orphaned=%d, recovered=%d",
            document_id, newly_orphaned, recovered,
        )
    return newly_orphaned, recovered


# ---------------------------------------------------------------------------
# 순수 텍스트 → 최소 doc
# ---------------------------------------------------------------------------

def prosemirror_from_text(text: Optional[str]) -> dict[str, Any]:
    """agent_proposal_service 같이 자유 텍스트 1건을 ProseMirror doc 으로 감싼다.

    빈 문자열/None 은 빈 문단 하나만 포함한 유효 doc 을 반환한다.
    """
    paragraph: dict[str, Any] = {
        "type": "paragraph",
        "attrs": {"node_id": str(uuid4())},
        "content": _node_text_children(text or ""),
    }
    return {
        "type": "doc",
        "schema_version": CONTENT_SNAPSHOT_SCHEMA_VERSION,
        "content": [paragraph],
    }


__all__ = [
    "CONTENT_SNAPSHOT_SCHEMA_VERSION",
    "is_valid_prosemirror_doc",
    "prosemirror_from_nodes",
    "nodes_from_prosemirror",
    "rebuild_nodes_from_snapshot",
    "rebuild_tags_for_document",
    "rebuild_annotation_anchoring",
    "prosemirror_from_text",
]
