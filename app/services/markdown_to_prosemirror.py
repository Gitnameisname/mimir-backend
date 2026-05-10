"""
Markdown → ProseMirror 변환 — S3 Phase 2 FG 2-6.

옵시디언 vault 의 markdown 파일을 Mimir 의 ProseMirror doc 으로 변환한다.

지원 요소 (task2-6.md §2.1 (4))
-------------------------------
- YAML frontmatter (`---` ~ `---` 블록, PyYAML safe_load)
- heading: `#` ~ `######` (1~6 레벨)
- paragraph
- bulletList: `- ` / `* ` / `+ `
- orderedList: `1. ` / `2. ` ...
- codeBlock: ``` fence (language 옵션)
- blockquote: `> `
- horizontalRule: `---` / `***`
- inline marks: bold `**`, italic `*` 또는 `_`, inline code `` ` ``, link `[text](url)`
- HashtagMark / WikiLinkMark — FG 2-2 / FG 2-3 정규식 재사용

지원 안 하는 요소 → plain text fallback (저장 가능, 시각적 markdown 는 보존 안 됨):
  - 표 (`|` table)
  - 정의 리스트
  - 각주 (`[^1]`)
  - 이미지 (`![[...]]`) — placeholder text 로 변환 후 report 에 미변환 자산 카운트 (호출자 책임)
  - HTML embedded blocks

NodeId 자동 부여
----------------
모든 block 레벨 노드 (heading / paragraph / bulletList / orderedList / codeBlock /
blockquote / horizontalRule) 의 attrs.node_id 에 UUID 부여. 본 변환 결과가
NodeId TipTap extension (FG 1-2) 과 정합.

보안
----
- PyYAML 은 `safe_load` 만 사용 — 임의 코드 실행 차단 (task2-6.md §8 R-04)
- frontmatter 가 손상된 YAML 이면 frontmatter 만 빈 dict 로 처리하고 본문은 그대로
- 무한 루프 방어: 입력 라인 수 상한 적용 (호출자가 vault_import_config.MAX_FILE_BYTES 로
  사전 차단 가정)
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Optional

import yaml

from app.services.tag_rules import INLINE_HASHTAG_PATTERN, normalize_tag
from app.services.wikilink_rules import WIKILINK_PATTERN

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z",
    re.DOTALL,
)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """`---` 으로 둘러싼 frontmatter 추출. 없거나 손상되면 ({}, text) 반환.

    PyYAML safe_load 사용 — 임의 객체 생성 차단.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    body = m.group(2)
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        logger.warning("frontmatter YAML 파싱 실패 — frontmatter 무시: %s", e)
        return {}, body
    if not isinstance(parsed, dict):
        # YAML 이 단일 값 / list 인 경우 — frontmatter 가 dict 가 아니면 무시
        return {}, body
    return parsed, body


# ---------------------------------------------------------------------------
# 인라인 mark 변환 (text run 단위)
# ---------------------------------------------------------------------------

_INLINE_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_INLINE_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_INLINE_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)|_([^_]+)_")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")


def _make_text_run(text: str, marks: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    run: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        run["marks"] = marks
    return run


def _parse_inline(text: str) -> list[dict[str, Any]]:
    """text run 1줄을 inline marks 배열로 분해.

    우선순위 (왼쪽 → 오른쪽 매칭):
      1. inline code (`...`)
      2. link [text](url)
      3. wikilink [[...]]
      4. hashtag #word
      5. bold **...**
      6. italic *...* / _..._
      7. plain text

    중첩 mark 는 본 단순 파서에서 미지원 (옵시디언 vault 의 일반 케이스 우선).
    """
    if not text:
        return []
    runs: list[dict[str, Any]] = []
    pos = 0
    n = len(text)

    # 처리 순서가 중요 — 가장 구체적 (code, link, wiki) 부터 시도
    # 단순 알고리즘: 모든 매칭을 순회 후 가장 빠른 시작 위치 선택, 처리 후 pos 갱신.
    while pos < n:
        chunk = text[pos:]
        candidates: list[tuple[int, int, list[dict[str, Any]]]] = []
        # (start, end, runs_for_match)

        # 1. inline code
        m = _INLINE_CODE_RE.search(chunk)
        if m:
            candidates.append((
                m.start(), m.end(),
                [_make_text_run(m.group(1), [{"type": "code"}])],
            ))

        # 2. link [text](url)
        m = _INLINE_LINK_RE.search(chunk)
        if m:
            candidates.append((
                m.start(), m.end(),
                [_make_text_run(m.group(1), [{"type": "link", "attrs": {"href": m.group(2)}}])],
            ))

        # 3. wikilink [[...]]
        m = WIKILINK_PATTERN.search(chunk)
        if m:
            target = m.group(1).strip()
            if target and "[[" not in target:
                candidates.append((
                    m.start(), m.end(),
                    [_make_text_run(
                        m.group(0),  # 원문 그대로 (FG 5-1 ADR 의 wikilink mark 직렬화 정합)
                        [{"type": "wikilink", "attrs": {"target": target}}],
                    )],
                ))

        # 4. hashtag #word
        m = INLINE_HASHTAG_PATTERN.search(chunk)
        if m:
            tag = normalize_tag(m.group(1))
            if tag:
                candidates.append((
                    m.start() + (1 if not chunk[m.start()].startswith("#") else 0),
                    m.end(),
                    [_make_text_run(
                        f"#{m.group(1)}",  # 원문 보존
                        [{"type": "hashtag", "attrs": {"tag": tag}}],
                    )],
                ))

        # 5. bold **...**
        m = _INLINE_BOLD_RE.search(chunk)
        if m:
            candidates.append((
                m.start(), m.end(),
                [_make_text_run(m.group(1), [{"type": "bold"}])],
            ))

        # 6. italic *...* or _..._
        m = _INLINE_ITALIC_RE.search(chunk)
        if m:
            italic_text = m.group(1) or m.group(2)
            candidates.append((
                m.start(), m.end(),
                [_make_text_run(italic_text, [{"type": "italic"}])],
            ))

        if not candidates:
            # 더 매칭 없음 — 나머지 plain text
            runs.append(_make_text_run(chunk))
            break

        # 가장 빠른 매칭 선택
        candidates.sort(key=lambda x: x[0])
        start, end, mark_runs = candidates[0]
        if start > 0:
            runs.append(_make_text_run(chunk[:start]))
        runs.extend(mark_runs)
        pos += end

    # 빈 run 제거
    return [r for r in runs if r.get("text") != "" or r.get("marks")]


# ---------------------------------------------------------------------------
# Block 파서
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_OPEN_RE = re.compile(r"^```(\S*)\s*$")
_FENCE_CLOSE_RE = re.compile(r"^```\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+)$")
_ORDERED_RE = re.compile(r"^\s*\d+\.\s+(.+)$")
_BLOCKQUOTE_RE = re.compile(r"^>\s*(.*)$")
_HR_RE = re.compile(r"^(---+|\*\*\*+)\s*$")


def _new_node_id() -> str:
    return str(uuid.uuid4())


def _block(node_type: str, attrs: dict[str, Any], content: list[dict[str, Any]]) -> dict[str, Any]:
    full_attrs = {"node_id": _new_node_id(), **attrs}
    return {"type": node_type, "attrs": full_attrs, "content": content}


def _parse_blocks(body: str) -> list[dict[str, Any]]:
    """본문을 block 노드 list 로 파싱."""
    lines = body.splitlines()
    i = 0
    n = len(lines)
    blocks: list[dict[str, Any]] = []

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 빈 줄 — paragraph 경계
        if not stripped:
            i += 1
            continue

        # horizontalRule — heading 보다 먼저 (단순 `---` 라인이 frontmatter 후 등장 가능)
        if _HR_RE.match(stripped):
            blocks.append({
                "type": "horizontalRule",
                "attrs": {"node_id": _new_node_id()},
            })
            i += 1
            continue

        # heading
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            text = m.group(2)
            blocks.append(_block(
                "heading", {"level": level}, _parse_inline(text),
            ))
            i += 1
            continue

        # codeBlock fence
        m = _FENCE_OPEN_RE.match(line)
        if m:
            language = m.group(1) or None
            i += 1
            code_lines: list[str] = []
            while i < n and not _FENCE_CLOSE_RE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            if i < n:  # closing fence 소비
                i += 1
            content = (
                [_make_text_run("\n".join(code_lines))] if code_lines else []
            )
            blocks.append(_block("codeBlock", {"language": language}, content))
            continue

        # bulletList / orderedList — 연속 라인 묶음
        if _BULLET_RE.match(line) or _ORDERED_RE.match(line):
            is_ordered = bool(_ORDERED_RE.match(line))
            list_re = _ORDERED_RE if is_ordered else _BULLET_RE
            item_lines: list[str] = []
            while i < n and (
                _BULLET_RE.match(lines[i]) if not is_ordered else _ORDERED_RE.match(lines[i])
            ):
                m2 = list_re.match(lines[i])
                if m2:
                    item_lines.append(m2.group(1))
                i += 1
            list_items = [
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "attrs": {"node_id": _new_node_id()},
                            "content": _parse_inline(text),
                        },
                    ],
                }
                for text in item_lines
            ]
            list_type = "orderedList" if is_ordered else "bulletList"
            blocks.append({
                "type": list_type,
                "attrs": {"node_id": _new_node_id()},
                "content": list_items,
            })
            continue

        # blockquote — 연속 `> ` 라인 묶음
        if _BLOCKQUOTE_RE.match(line):
            quote_lines: list[str] = []
            while i < n and _BLOCKQUOTE_RE.match(lines[i]):
                m2 = _BLOCKQUOTE_RE.match(lines[i])
                if m2:
                    quote_lines.append(m2.group(1))
                i += 1
            text = "\n".join(quote_lines)
            blocks.append(_block(
                "blockquote", {},
                [{
                    "type": "paragraph",
                    "attrs": {"node_id": _new_node_id()},
                    "content": _parse_inline(text),
                }],
            ))
            continue

        # paragraph — 연속 일반 라인
        para_lines: list[str] = []
        while i < n and lines[i].strip() and not _is_special_block_start(lines[i]):
            para_lines.append(lines[i])
            i += 1
        text = " ".join(para_lines).strip()
        if text:
            blocks.append(_block(
                "paragraph", {}, _parse_inline(text),
            ))

    return blocks


def _is_special_block_start(line: str) -> bool:
    return bool(
        _HEADING_RE.match(line)
        or _FENCE_OPEN_RE.match(line)
        or _BULLET_RE.match(line)
        or _ORDERED_RE.match(line)
        or _BLOCKQUOTE_RE.match(line)
        or _HR_RE.match(line.strip())
    )


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def markdown_to_prosemirror(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """markdown 문서 → (ProseMirror doc, frontmatter dict).

    빈 문서 / frontmatter 만 있는 문서도 정상 처리 (빈 paragraph 1개 보존 — ProseMirror schema 요구).

    Returns:
        (doc, frontmatter)
        - doc: `{"type": "doc", "schema_version": 1, "content": [...blocks]}`
        - frontmatter: 파싱된 dict. 부재 시 빈 dict.
    """
    if text is None:
        text = ""
    frontmatter, body = split_frontmatter(text)
    blocks = _parse_blocks(body)
    if not blocks:
        # ProseMirror schema 는 doc.content 에 최소 1 개 노드 요구 — 빈 paragraph 추가
        blocks = [_block("paragraph", {}, [])]
    doc = {
        "type": "doc",
        "schema_version": 1,
        "content": blocks,
    }
    return doc, frontmatter


__all__ = [
    "markdown_to_prosemirror",
    "split_frontmatter",
]
