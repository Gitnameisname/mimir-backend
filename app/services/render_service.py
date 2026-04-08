"""
RenderService — 문서 렌더링 파이프라인 (Task 4-7).

책임:
  저장된 content_snapshot을 렌더링 ViewModel로 변환한다.

파이프라인 단계:
  1. 원본 조회 (호출자가 Version을 전달)
  2. content_snapshot 로드
  3. 구조 정규화 (unknown type 감지, 필수 필드 보완)
  4. ViewModel 변환 (블록 트리, TOC 파생)
  5. 최종 RenderDocument 조립

설계 원칙:
  - Draft / Published / 특정 버전 모두 동일 파이프라인 통과.
  - 렌더링 실패 = 전체 실패가 아닌 fallback block + warning 수집.
  - TOC는 저장 구조에 없고 렌더링 단계에서 heading 블록으로 파생 생성.
  - content_snapshot이 None이면 빈 문서를 반환한다 (에러 아님).
"""

import logging
from typing import Any, Optional

from app.models.version import Version
from app.schemas.render import RenderBlock, RenderDocument, RenderWarning, StatusBadge, TocItem

logger = logging.getLogger(__name__)

# 지원하는 블록 타입 집합
_SUPPORTED_TYPES = {
    "document", "section", "heading", "paragraph",
    "list", "list_item", "table", "quote", "appendix",
}


# ---------------------------------------------------------------------------
# ViewModel 데이터 구조 (dict 기반, Pydantic 미사용 — 성능)
# ---------------------------------------------------------------------------

def _render_mode(version: Version, current_draft_id: Optional[str], current_published_id: Optional[str]) -> str:
    if version.id == current_draft_id:
        return "draft"
    if version.id == current_published_id:
        return "published"
    return "version"


def _status_badge(render_mode: str, version_number: int) -> dict[str, str]:
    badges = {
        "published": {"label": "현재 공식", "type": "current_published"},
        "draft": {"label": "작업 중", "type": "current_draft"},
        "version": {"label": f"v{version_number}", "type": "past_version"},
    }
    return badges.get(render_mode, {"label": render_mode, "type": render_mode})


# ---------------------------------------------------------------------------
# 정규화 + 블록 변환
# ---------------------------------------------------------------------------

def _normalize_block(
    node: Any,
    warnings: list[dict],
) -> dict[str, Any]:
    """단일 블록을 정규화된 RenderBlock dict로 변환한다."""
    if not isinstance(node, dict):
        warnings.append({"level": "warn", "message": f"Non-dict block skipped: {type(node)}"})
        return {"block_type": "error", "content": "[invalid block structure]", "warnings": []}

    raw_type = node.get("type", "")
    block_id = node.get("id", "")

    if raw_type not in _SUPPORTED_TYPES:
        warnings.append({"level": "warn", "message": f"Unsupported block type: {raw_type!r}"})
        return {
            "block_type": "unsupported",
            "original_type": raw_type,
            "block_id": block_id,
            "warnings": [{"level": "warn", "message": f"Unsupported block type: {raw_type!r}"}],
        }

    block: dict[str, Any] = {
        "block_type": raw_type,
        "block_id": block_id,
        "warnings": [],
    }

    if raw_type == "heading":
        level = node.get("level", 2)
        if not isinstance(level, int) or not (1 <= level <= 6):
            block["warnings"].append({"level": "warn", "message": f"Invalid heading level {level!r}, defaulting to 2"})
            level = 2
        text = node.get("text", "")
        if not text:
            block["warnings"].append({"level": "warn", "message": "Empty heading text"})
            text = "[제목 없음]"
        block["heading_level"] = level
        block["content"] = text
        block["anchor"] = block_id or f"h{level}"

    elif raw_type == "paragraph":
        text = node.get("text", "")
        block["content"] = text
        block["annotations"] = node.get("annotations", [])

    elif raw_type == "section":
        block["children"] = _normalize_children(node.get("children", []), warnings)
        block["content"] = node.get("title", "")

    elif raw_type in ("list", "list_item"):
        block["ordered"] = node.get("metadata", {}).get("ordered", False)
        block["content"] = node.get("text", "") or node.get("content", "")
        block["children"] = _normalize_children(node.get("children", []), warnings)

    elif raw_type == "table":
        rows = node.get("metadata", {}).get("rows") or node.get("rows")
        if not rows or not isinstance(rows, list):
            block["block_type"] = "error"
            block["content"] = "[표 구조 손상]"
            block["original_type"] = "table"
            block["warnings"].append({"level": "error", "message": "Table rows missing or invalid"})
            warnings.append({"level": "error", "message": "Table rendering failed: rows missing"})
        else:
            headers = rows[0] if rows else []
            data_rows = rows[1:] if len(rows) > 1 else []
            block["table_model"] = {"headers": headers, "rows": data_rows}

    elif raw_type == "quote":
        block["content"] = node.get("text", "") or node.get("content", "")

    elif raw_type == "appendix":
        block["content"] = node.get("title", "")
        block["children"] = _normalize_children(node.get("children", []), warnings)

    elif raw_type == "document":
        block["children"] = _normalize_children(node.get("children", []), warnings)

    return block


def _normalize_children(
    children: Any,
    warnings: list[dict],
) -> list[dict[str, Any]]:
    if not isinstance(children, list):
        return []
    return [_normalize_block(child, warnings) for child in children]


def _extract_toc(blocks: list[dict[str, Any]]) -> list[dict]:
    """blocks 트리에서 heading 블록을 순회해 TOC를 파생 생성한다."""
    toc = []

    def _walk(items: list[dict]) -> None:
        for item in items:
            if item.get("block_type") == "heading":
                level = item.get("heading_level", 2)
                if level <= 2:
                    toc.append({
                        "block_id": item.get("block_id", ""),
                        "level": level,
                        "text": item.get("content", ""),
                        "anchor": item.get("anchor", ""),
                    })
            children = item.get("children", [])
            if children:
                _walk(children)

    _walk(blocks)
    return toc


def _split_appendix(blocks: list[dict]) -> tuple[list[dict], list[dict]]:
    """blocks에서 appendix를 분리해 (main_blocks, appendix_blocks)로 반환한다."""
    main, appendix = [], []
    for b in blocks:
        if b.get("block_type") == "appendix":
            appendix.append(b)
        else:
            main.append(b)
    return main, appendix


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class RenderService:
    """문서 렌더링 파이프라인 서비스."""

    def render_version(
        self,
        version: Version,
        *,
        current_draft_id: Optional[str] = None,
        current_published_id: Optional[str] = None,
        include_toc: bool = True,
    ) -> RenderDocument:
        """Version → RenderDocument 변환.

        content_snapshot이 None이면 빈 문서를 반환한다.
        """
        mode = _render_mode(version, current_draft_id, current_published_id)
        badge_dict = _status_badge(mode, version.version_number)
        badge = StatusBadge(**badge_dict)
        warnings: list[dict] = []
        unsupported_types: list[str] = []

        snapshot = version.content_snapshot
        if not snapshot:
            return RenderDocument(
                source_document_id=version.document_id,
                source_version_id=version.id,
                source_version_number=version.version_number,
                render_mode=mode,
                title=version.title_snapshot or "",
                summary=version.summary_snapshot,
                status_badge=badge,
            )

        # 루트가 document 타입인지 확인
        if not isinstance(snapshot, dict):
            warnings.append({"level": "error", "message": "content_snapshot is not a JSON object"})
            raw_blocks = []
        elif snapshot.get("type") == "document":
            raw_blocks = snapshot.get("children", [])
        else:
            # 루트가 document가 아닌 경우 — 전체를 children으로 처리
            raw_blocks = [snapshot]

        blocks = _normalize_children(raw_blocks, warnings)

        # unsupported 타입 수집
        def _collect_unsupported(items: list[dict]) -> None:
            for item in items:
                if item.get("block_type") == "unsupported":
                    ot = item.get("original_type", "unknown")
                    if ot not in unsupported_types:
                        unsupported_types.append(ot)
                for child in item.get("children", []):
                    _collect_unsupported([child])

        _collect_unsupported(blocks)

        main_blocks, appendix_blocks = _split_appendix(blocks)
        toc_dicts = _extract_toc(main_blocks) if include_toc else []

        return RenderDocument(
            source_document_id=version.document_id,
            source_version_id=version.id,
            source_version_number=version.version_number,
            render_mode=mode,
            title=version.title_snapshot or "",
            summary=version.summary_snapshot,
            status_badge=badge,
            toc=[TocItem(**t) for t in toc_dicts],
            blocks=[RenderBlock(**b) for b in main_blocks],
            appendix_blocks=[RenderBlock(**b) for b in appendix_blocks],
            warnings=[RenderWarning(**w) for w in warnings],
            unsupported_blocks=unsupported_types,
        )


# 모듈 수준 싱글턴
render_service = RenderService()
