"""
S3 Phase 2 FG 2-6 — markdown_to_prosemirror 단위 회귀.

task2-6.md §4 Step 2 의 회귀 시나리오 (≥ 25):
  - frontmatter 파싱 (정상 / 손상 / 부재 / dict 가 아닌 단일값)
  - heading 1~6 레벨
  - paragraph (단일 / 다중 줄)
  - bulletList / orderedList
  - codeBlock (language / no language)
  - blockquote
  - horizontalRule
  - inline marks: bold / italic / code / link
  - HashtagMark / WikiLinkMark 정합 (FG 2-2 / 2-3 정규식 재사용)
  - NodeId 자동 부여 (모든 block 노드)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

class TestFrontmatter:
    def test_frontmatter_parsed(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        text = "---\ntitle: Hello\ntags:\n  - ai\n  - ml\n---\n\n# Body"
        doc, fm = markdown_to_prosemirror(text)
        assert fm == {"title": "Hello", "tags": ["ai", "ml"]}
        # Body 가 첫 heading
        assert doc["content"][0]["type"] == "heading"

    def test_no_frontmatter(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        doc, fm = markdown_to_prosemirror("# Hello")
        assert fm == {}
        assert doc["content"][0]["type"] == "heading"

    def test_corrupt_yaml_ignored(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        text = "---\ntitle: [unclosed\n---\n\n# Body"
        doc, fm = markdown_to_prosemirror(text)
        # 손상된 YAML 은 빈 dict + 본문은 그대로
        assert fm == {}
        assert doc["content"][0]["type"] == "heading"

    def test_non_dict_yaml_ignored(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        text = "---\n- not_a_dict\n---\n\nBody"
        doc, fm = markdown_to_prosemirror(text)
        assert fm == {}


# ---------------------------------------------------------------------------
# Heading
# ---------------------------------------------------------------------------

class TestHeading:
    @pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
    def test_all_heading_levels(self, level):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        text = f"{'#' * level} Title"
        doc, _ = markdown_to_prosemirror(text)
        h = doc["content"][0]
        assert h["type"] == "heading"
        assert h["attrs"]["level"] == level
        assert h["content"][0]["text"] == "Title"

    def test_heading_with_inline_marks(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        text = "# **Bold** title"
        doc, _ = markdown_to_prosemirror(text)
        h = doc["content"][0]
        runs = h["content"]
        # bold mark 가 적용된 run 존재
        assert any(
            r.get("marks") and r["marks"][0]["type"] == "bold"
            for r in runs
        )


# ---------------------------------------------------------------------------
# Paragraph
# ---------------------------------------------------------------------------

class TestParagraph:
    def test_single_line_paragraph(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        doc, _ = markdown_to_prosemirror("Hello world")
        p = doc["content"][0]
        assert p["type"] == "paragraph"
        assert p["content"][0]["text"] == "Hello world"

    def test_multi_line_paragraph_joined(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        doc, _ = markdown_to_prosemirror("Hello\nworld")
        p = doc["content"][0]
        assert p["type"] == "paragraph"
        # 줄바꿈은 공백으로 합쳐짐 (markdown 호환)
        assert "Hello" in p["content"][0]["text"] and "world" in p["content"][0]["text"]

    def test_blank_line_separates_paragraphs(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        doc, _ = markdown_to_prosemirror("First\n\nSecond")
        assert len(doc["content"]) == 2
        assert all(b["type"] == "paragraph" for b in doc["content"])


# ---------------------------------------------------------------------------
# List (bullet / ordered)
# ---------------------------------------------------------------------------

class TestList:
    def test_bullet_list(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        text = "- A\n- B\n- C"
        doc, _ = markdown_to_prosemirror(text)
        b = doc["content"][0]
        assert b["type"] == "bulletList"
        assert len(b["content"]) == 3
        assert all(li["type"] == "listItem" for li in b["content"])

    def test_ordered_list(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        text = "1. one\n2. two\n3. three"
        doc, _ = markdown_to_prosemirror(text)
        b = doc["content"][0]
        assert b["type"] == "orderedList"
        assert len(b["content"]) == 3

    def test_mixed_list_with_inline_marks(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        text = "- **bold** item\n- normal"
        doc, _ = markdown_to_prosemirror(text)
        b = doc["content"][0]
        first_para_runs = b["content"][0]["content"][0]["content"]
        assert any(
            r.get("marks") and r["marks"][0]["type"] == "bold"
            for r in first_para_runs
        )


# ---------------------------------------------------------------------------
# CodeBlock
# ---------------------------------------------------------------------------

class TestCodeBlock:
    def test_fenced_code_block_with_language(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        text = "```python\nprint('hi')\n```"
        doc, _ = markdown_to_prosemirror(text)
        cb = doc["content"][0]
        assert cb["type"] == "codeBlock"
        assert cb["attrs"]["language"] == "python"
        assert cb["content"][0]["text"] == "print('hi')"

    def test_fenced_code_block_no_language(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        text = "```\nplain code\n```"
        doc, _ = markdown_to_prosemirror(text)
        cb = doc["content"][0]
        assert cb["type"] == "codeBlock"
        assert cb["attrs"]["language"] is None

    def test_code_block_preserves_inner_special_chars(self):
        """codeBlock 안의 `[[`, `#tag` 등은 inline mark 변환 안 됨 (raw text)."""
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        text = "```\n#nottag and [[notwiki]]\n```"
        doc, _ = markdown_to_prosemirror(text)
        cb = doc["content"][0]
        assert cb["type"] == "codeBlock"
        # text run 그대로, marks 없음
        assert "#nottag" in cb["content"][0]["text"]
        assert "[[notwiki]]" in cb["content"][0]["text"]
        assert "marks" not in cb["content"][0]


# ---------------------------------------------------------------------------
# Blockquote / HR
# ---------------------------------------------------------------------------

class TestBlockquote:
    def test_simple_blockquote(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        doc, _ = markdown_to_prosemirror("> quoted line")
        bq = doc["content"][0]
        assert bq["type"] == "blockquote"

    def test_horizontal_rule(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        doc, _ = markdown_to_prosemirror("Above\n\n---\n\nBelow")
        # paragraph / hr / paragraph
        types = [b["type"] for b in doc["content"]]
        assert "horizontalRule" in types


# ---------------------------------------------------------------------------
# Inline marks
# ---------------------------------------------------------------------------

class TestInlineMarks:
    def test_bold(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror
        doc, _ = markdown_to_prosemirror("This is **strong** text")
        runs = doc["content"][0]["content"]
        bold_runs = [r for r in runs if r.get("marks") and r["marks"][0]["type"] == "bold"]
        assert bold_runs and bold_runs[0]["text"] == "strong"

    def test_italic(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror
        doc, _ = markdown_to_prosemirror("This is *emphasized* text")
        runs = doc["content"][0]["content"]
        italic_runs = [r for r in runs if r.get("marks") and r["marks"][0]["type"] == "italic"]
        assert italic_runs and italic_runs[0]["text"] == "emphasized"

    def test_inline_code(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror
        doc, _ = markdown_to_prosemirror("This is `code` text")
        runs = doc["content"][0]["content"]
        code_runs = [r for r in runs if r.get("marks") and r["marks"][0]["type"] == "code"]
        assert code_runs and code_runs[0]["text"] == "code"

    def test_link(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror
        doc, _ = markdown_to_prosemirror("[example](https://example.com)")
        runs = doc["content"][0]["content"]
        assert runs[0]["text"] == "example"
        assert runs[0]["marks"][0]["type"] == "link"
        assert runs[0]["marks"][0]["attrs"]["href"] == "https://example.com"


# ---------------------------------------------------------------------------
# Hashtag / Wikilink (FG 2-2 / 2-3 정합)
# ---------------------------------------------------------------------------

class TestHashtagWikilink:
    def test_hashtag_recognized(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror
        doc, _ = markdown_to_prosemirror("about #ai topic")
        runs = doc["content"][0]["content"]
        hashtag_runs = [r for r in runs if r.get("marks") and r["marks"][0]["type"] == "hashtag"]
        assert hashtag_runs
        assert hashtag_runs[0]["marks"][0]["attrs"]["tag"] == "ai"

    def test_wikilink_recognized(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror
        doc, _ = markdown_to_prosemirror("see [[Other Doc]] for context")
        runs = doc["content"][0]["content"]
        wiki_runs = [r for r in runs if r.get("marks") and r["marks"][0]["type"] == "wikilink"]
        assert wiki_runs
        assert wiki_runs[0]["marks"][0]["attrs"]["target"] == "Other Doc"

    def test_wikilink_with_pipe_alias_uses_target(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror
        doc, _ = markdown_to_prosemirror("see [[Foo|displayed]] here")
        runs = doc["content"][0]["content"]
        wiki_runs = [r for r in runs if r.get("marks") and r["marks"][0]["type"] == "wikilink"]
        assert wiki_runs[0]["marks"][0]["attrs"]["target"] == "Foo"


# ---------------------------------------------------------------------------
# NodeId 자동 부여
# ---------------------------------------------------------------------------

class TestNodeId:
    def test_all_blocks_have_node_id(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror

        text = (
            "# Title\n\n"
            "para 1\n\n"
            "- item\n\n"
            "```py\ncode\n```\n\n"
            "> quote\n\n"
            "---\n"
        )
        doc, _ = markdown_to_prosemirror(text)
        for block in doc["content"]:
            assert "node_id" in block.get("attrs", {})
            # UUID 형식 검증 (간단)
            assert len(block["attrs"]["node_id"]) == 36

    def test_node_ids_unique(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror
        doc, _ = markdown_to_prosemirror("p1\n\np2\n\np3\n")
        ids = [b["attrs"]["node_id"] for b in doc["content"]]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# 빈 문서 / edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_document_creates_empty_paragraph(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror
        doc, fm = markdown_to_prosemirror("")
        assert doc["type"] == "doc"
        assert len(doc["content"]) == 1
        assert doc["content"][0]["type"] == "paragraph"

    def test_none_input(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror
        doc, fm = markdown_to_prosemirror(None)  # type: ignore[arg-type]
        assert doc["type"] == "doc"
        assert fm == {}

    def test_only_frontmatter_no_body(self):
        from app.services.markdown_to_prosemirror import markdown_to_prosemirror
        text = "---\ntitle: x\n---\n"
        doc, fm = markdown_to_prosemirror(text)
        assert fm == {"title": "x"}
        # body 가 비어도 paragraph 1개 보존
        assert len(doc["content"]) == 1
