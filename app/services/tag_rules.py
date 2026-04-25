"""
태그 파싱·정규화 규약 — S3 Phase 2 FG 2-2.

블로커1 결정서 (`docs/개발문서/S3/phase2/산출물/블로커1_태그규약결정서.md`) §3.3·§3.4
의 규약을 코드로 고정한다.

정규화 규칙
----------
* 앞의 ``#`` 제거 (인라인에서 온 원시 토큰 허용)
* 앞뒤 공백 제거
* NFKC 유니코드 정규화 (한글 조합/완성형 차이 흡수)
* lowercase
* 허용 문자: ``[\\w/-]{1,64}`` (영숫자 + `_` + `-` + `/` 중첩 태그)

인라인 hashtag 정규식
-------------------
* `##` 또는 `###` 으로 시작하는 토큰은 매칭되지 않는다 (Markdown heading 방어).
* ``#foo`` 와 ``#한글`` 모두 매칭.
* 중첩 태그 ``#ai/ml`` 지원.
* 뒤에 단어문자나 허용 외 문자가 오면 경계 판단.

서버측 안전 상수
---------------
* ``TAG_NAME_MAX_LEN = 64`` — DB UNIQUE 컬럼과 일치
* ``INLINE_HASHTAG_PATTERN`` — 미리 compile 된 정규식. ``re.UNICODE`` 포함
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional


TAG_NAME_MAX_LEN = 64

# 인라인 hashtag 정규식
#   (?:^|(?<=\\s)|(?<=[^\\w#]))  — 단어/해시가 아닌 경계 뒤
#   #                            — 시작 `#` 하나
#   ([\\w][\\w/-]{0,63})          — 첫 글자는 word, 이후 word / `_` / `-` / `/`
#   (?=\\s|$|[^\\w/-])            — 뒤는 공백·끝·허용 외 문자 (뒤에 # 또는 추가 word 금지)
INLINE_HASHTAG_PATTERN = re.compile(
    r"(?:^|(?<=\s)|(?<=[^\w#]))"
    r"#([\w][\w/-]{0,63})"
    r"(?=\s|$|[^\w/-])",
    flags=re.UNICODE,
)

# 정규화 후 허용되는 최종 패턴 (NFKC+lower 거친 문자열에 대한 full match)
_NORMALIZED_PATTERN = re.compile(r"[\w/-]{1,%d}" % TAG_NAME_MAX_LEN, flags=re.UNICODE)


def normalize_tag(raw: str) -> Optional[str]:
    """원시 태그 토큰을 정규화. 규칙 위반 시 ``None``.

    예시::

        normalize_tag("#ai")        == "ai"
        normalize_tag("  #AI/ML ")  == "ai/ml"
        normalize_tag("한글")       == "한글"
        normalize_tag("##heading")  == None       # 앞에 # 여러 개
        normalize_tag("")           == None
        normalize_tag("a" * 100)    == None       # 길이 초과
        normalize_tag("sp ace")     == None       # 공백 불허
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    # 앞에 `#` 은 한 개까지 허용 (인라인에서 온 토큰)
    if s.startswith("#"):
        s = s[1:]
    if not s or s.startswith("#"):
        return None  # `##foo` 같은 비정상
    # NFKC → lowercase
    s = unicodedata.normalize("NFKC", s).lower()
    if not _NORMALIZED_PATTERN.fullmatch(s):
        return None
    return s


def extract_tags_from_snapshot(
    snapshot: dict | None,
    metadata: dict | None,
) -> list[tuple[str, str]]:
    """content_snapshot + document.metadata 에서 태그를 수집한다.

    반환은 ``(name_normalized, source)`` 튜플의 리스트. ``source`` 는
    ``'inline' | 'frontmatter' | 'both'`` 중 하나. 같은 태그가 두 소스에서
    모두 발견되면 ``'both'`` 로 집약.

    제외 규칙:
    * ``codeBlock`` 노드의 text 는 수집 대상 아님 (코드는 문서 구조 태그와 무관)
    * text run 에 ``link`` mark 가 걸려 있으면 해당 text 는 무시 (링크 텍스트 안
      hashtag 오탐 방지)

    Args:
        snapshot: ``content_snapshot`` JSONB (ProseMirror doc)
        metadata: ``documents.metadata`` JSONB — ``tags: [...]`` 또는
                  ``tags: ["a", "b"]`` 형태만 해석 (다른 형식은 무시)
    """
    inline: set[str] = set()
    frontmatter: set[str] = set()

    # 1) snapshot 순회 (block 단위). codeBlock / link text 는 스킵
    if isinstance(snapshot, dict):
        _collect_inline_hashtags(snapshot.get("content") or [], inline)

    # 2) metadata.tags (frontmatter)
    if isinstance(metadata, dict):
        tags_field = metadata.get("tags")
        if isinstance(tags_field, list):
            for raw in tags_field:
                if not isinstance(raw, str):
                    continue
                norm = normalize_tag(raw)
                if norm:
                    frontmatter.add(norm)

    # union + source 계산
    result: list[tuple[str, str]] = []
    for name in sorted(inline | frontmatter):
        in_inline = name in inline
        in_fm = name in frontmatter
        if in_inline and in_fm:
            source = "both"
        elif in_inline:
            source = "inline"
        else:
            source = "frontmatter"
        result.append((name, source))
    return result


def _collect_inline_hashtags(pm_nodes: list, out: set[str]) -> None:
    """ProseMirror block 노드 리스트를 순회하며 인라인 hashtag 를 수집."""
    for node in pm_nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type == "codeBlock":
            continue  # 코드블록은 스킵
        children = node.get("content") or []
        if node_type in ("doc", "section", "bulletList", "orderedList", "listItem", "blockquote"):
            # 컨테이너 — 재귀
            _collect_inline_hashtags(children, out)
            continue
        # paragraph / heading 등 text 컨테이너 — 내부 text run 순회
        _scan_text_children(children, out)


def _scan_text_children(children: list, out: set[str]) -> None:
    """paragraph 등의 content 배열에서 text run 을 꺼내 hashtag 매칭."""
    for child in children:
        if not isinstance(child, dict):
            continue
        if child.get("type") == "text":
            # link mark 가 걸린 텍스트는 링크 내부 → 스킵
            marks = child.get("marks") or []
            if any(isinstance(m, dict) and m.get("type") == "link" for m in marks):
                continue
            text = child.get("text") or ""
            for match in INLINE_HASHTAG_PATTERN.finditer(text):
                norm = normalize_tag(match.group(1))
                if norm:
                    out.add(norm)
        elif child.get("content"):
            # 중첩 컨테이너 — 재귀
            _scan_text_children(child.get("content") or [], out)
