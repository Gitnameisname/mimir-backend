"""
Wikilink (`[[문서명]]`) 파싱 규약 — S3 Phase 2 FG 2-3.

`docs/개발문서/S3/phase2/작업지시서/task2-3.md` §2.1 (1)~(2) 의 정규식 / 규약을
코드로 고정한다. ``content_snapshot`` (ProseMirror doc) 을 순회하며 본문에 등장한
``[[제목]]`` 또는 ``[[제목|표시이름]]`` 토큰을 ``(raw_text, node_id)`` 튜플로 수집한다.

규약
----
* 정규식: ``\\[\\[([^\\]\\n|]{1,200})(?:\\|([^\\]\\n]{1,200}))?\\]\\]``
* group 1 = 타겟 제목 (raw_text). group 2 = 표시 이름 (있을 수도 없을 수도)
* 파서는 **타겟 제목만** 추출한다. 표시 이름은 본 함수의 책임이 아니다 (필요 시 호출자가 별도 추출)
* 코드블록 (``codeBlock``) 의 text 는 수집 대상 아님
* text run 에 ``link`` mark 가 걸려 있으면 그 text 는 무시 (외부 링크 텍스트 안 wikilink 오탐 방지)
* group 1 안에 ``[[`` 가 포함되면 거부 (중첩 ``[[[[foo]]]]`` 거부 — task2-3.md Step 1)
* 빈 토큰 ``[[]]`` 는 ``{1,200}`` 길이 제약으로 자연 거부
* 줄바꿈 포함 토큰 ``[[foo\\nbar]]`` 은 ``[^\\]\\n|]`` 로 자연 거부

**raw_text 의 NFC 정규화는 본 함수에서 하지 않는다.** resolver 단계에서 양쪽 NFC 정규화 후 비교
(`docs/개발문서/S3/phase2/산출물/FG2-3_Pre-flight_갱신.md` §2.6).

node_id attribution
-------------------
가장 가까운 attrs.node_id 가 있는 조상 block 의 node_id 를 사용한다. NodeId TipTap extension
(``frontend/src/features/editor/tiptap/extensions/NodeId.ts``) 이 ``heading`` / ``paragraph`` /
``bulletList`` / ``orderedList`` / ``codeBlock`` 에 attrs.node_id 를 부여하므로, 본 파서는
재귀 순회 시 가장 최근 만난 attrs.node_id 를 carry 한다. node_id 가 없는 토큰은 결과에서 제외 —
DB UNIQUE (from_doc_id, node_id, raw_text) 제약과 정합.

서버측 안전 상수
---------------
* ``WIKILINK_RAW_TEXT_MAX_LEN = 200`` — DB ``raw_text VARCHAR(500)`` 한도보다 더 보수적인
  서버 검증 한도 (정규식과 일치).
* ``WIKILINK_PATTERN`` — 미리 compile 된 정규식. ``re.UNICODE`` 포함.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

WIKILINK_RAW_TEXT_MAX_LEN = 200

# task2-3.md §2.1 (2) 의 정규식 정본
WIKILINK_PATTERN = re.compile(
    r"\[\[([^\]\n|]{1,%d})(?:\|([^\]\n]{1,%d}))?\]\]"
    % (WIKILINK_RAW_TEXT_MAX_LEN, WIKILINK_RAW_TEXT_MAX_LEN),
    flags=re.UNICODE,
)


def extract_wikilinks_from_snapshot(
    snapshot: Optional[dict],
) -> list[tuple[str, str]]:
    """content_snapshot 에서 ``[[...]]`` 토큰을 ``(raw_text, node_id)`` 로 수집한다.

    같은 노드 안에서 같은 raw_text 가 여러 번 등장하면 첫 항목만 보존한다 (DB UNIQUE
    (from_doc_id, node_id, raw_text) 제약 정합).

    Args:
        snapshot: ProseMirror doc JSONB. ``None`` 또는 부적합 형식이면 빈 리스트 반환.

    Returns:
        ``[(raw_text, node_id), ...]`` 튜플 리스트. 입력 순서 보존.
    """
    if not isinstance(snapshot, dict):
        return []
    content = snapshot.get("content")
    if not isinstance(content, list):
        return []

    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()  # (node_id, raw_text) 중복 흡수
    _collect(content, current_node_id=None, out=result, seen=seen)
    return result


def _collect(
    pm_nodes: Iterable[Any],
    current_node_id: Optional[str],
    out: list[tuple[str, str]],
    seen: set[tuple[str, str]],
) -> None:
    """ProseMirror block 노드 리스트를 순회하며 wikilink 토큰을 수집."""
    for node in pm_nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type == "codeBlock":
            continue  # 코드블록은 스킵 (text run 자체를 보지 않음)

        # block 의 attrs.node_id 가 있으면 carry 갱신
        attrs = node.get("attrs") or {}
        block_node_id = attrs.get("node_id") if isinstance(attrs, dict) else None
        carry = block_node_id or current_node_id

        children = node.get("content") or []
        if node_type in (
            "doc",
            "section",
            "bulletList",
            "orderedList",
            "listItem",
            "blockquote",
        ):
            # 컨테이너 — 재귀
            _collect(children, current_node_id=carry, out=out, seen=seen)
            continue

        # leaf block (heading / paragraph 등) — text run 순회
        _scan_text_children(children, carry, out, seen)


def _scan_text_children(
    children: Iterable[Any],
    node_id: Optional[str],
    out: list[tuple[str, str]],
    seen: set[tuple[str, str]],
) -> None:
    """leaf block 의 content 배열에서 text run 을 꺼내 wikilink 매칭."""
    for child in children:
        if not isinstance(child, dict):
            continue
        if child.get("type") == "text":
            # link mark 가 걸린 텍스트는 외부 링크 → 스킵
            marks = child.get("marks") or []
            if any(isinstance(m, dict) and m.get("type") == "link" for m in marks):
                continue
            text = child.get("text") or ""
            if not text:
                continue
            for match in WIKILINK_PATTERN.finditer(text):
                raw = match.group(1)
                # 중첩 [[[[foo]]]] 거부 — group 1 에 `[[` 포함되면 무효
                if "[[" in raw:
                    continue
                raw_stripped = raw.strip()
                if not raw_stripped:
                    continue
                if not node_id:
                    # node_id 미부여 텍스트의 wikilink 는 anchor 불가 — 스킵
                    continue
                key = (node_id, raw_stripped)
                if key in seen:
                    continue
                seen.add(key)
                out.append((raw_stripped, node_id))
        elif child.get("content"):
            # 중첩 컨테이너 (예: link mark 가 아닌 다른 inline node) — 재귀
            _scan_text_children(child.get("content") or [], node_id, out, seen)


__all__ = [
    "WIKILINK_PATTERN",
    "WIKILINK_RAW_TEXT_MAX_LEN",
    "extract_wikilinks_from_snapshot",
]
