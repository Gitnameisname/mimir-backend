"""
S3 Phase 2 FG 2-3 — wikilink_rules.extract_wikilinks_from_snapshot 파서 단위.

task2-3.md §4 Step 1 의 회귀 시나리오 충족.
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


def _doc(*blocks: dict) -> dict:
    return {"type": "doc", "content": list(blocks)}


def _para(node_id: str, *text_runs: dict) -> dict:
    return {
        "type": "paragraph",
        "attrs": {"node_id": node_id},
        "content": list(text_runs),
    }


def _text(text: str, marks: list | None = None) -> dict:
    run: dict = {"type": "text", "text": text}
    if marks is not None:
        run["marks"] = marks
    return run


class TestSimpleExtraction:
    def test_simple_link(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(_para("n1", _text("see [[Foo]] here")))
        assert extract_wikilinks_from_snapshot(doc) == [("Foo", "n1")]

    def test_pipe_alias_keeps_target(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(_para("n1", _text("see [[Foo|displayed name]]")))
        # group 1 = target (Foo), alias (displayed name) 은 본 파서가 보존하지 않음
        assert extract_wikilinks_from_snapshot(doc) == [("Foo", "n1")]

    def test_multiple_in_same_block(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(_para("n1", _text("[[A]] and [[B]] and [[C]]")))
        assert extract_wikilinks_from_snapshot(doc) == [
            ("A", "n1"),
            ("B", "n1"),
            ("C", "n1"),
        ]

    def test_links_in_different_blocks_track_node_id(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(
            _para("n1", _text("[[A]]")),
            _para("n2", _text("[[B]]")),
            _para("n3", _text("[[C]]")),
        )
        assert extract_wikilinks_from_snapshot(doc) == [
            ("A", "n1"),
            ("B", "n2"),
            ("C", "n3"),
        ]

    def test_korean_title(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(_para("n1", _text("참조: [[한글 제목]]")))
        assert extract_wikilinks_from_snapshot(doc) == [("한글 제목", "n1")]

    def test_dedup_within_same_node(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(_para("n1", _text("[[A]] and again [[A]] and [[A|alias]]")))
        # 같은 node_id 안에서 raw_text=="A" 중복은 첫 항목만. [[A|alias]] 도 raw="A" 라 중복.
        assert extract_wikilinks_from_snapshot(doc) == [("A", "n1")]

    def test_same_raw_in_different_nodes_not_dedup(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(
            _para("n1", _text("[[A]]")),
            _para("n2", _text("[[A]]")),
        )
        assert extract_wikilinks_from_snapshot(doc) == [("A", "n1"), ("A", "n2")]


class TestExclusionRules:
    def test_codeblock_excluded(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(
            _para("n1", _text("real [[A]]")),
            {
                "type": "codeBlock",
                "attrs": {"node_id": "n2"},
                "content": [_text("[[B]] inside code")],
            },
        )
        assert extract_wikilinks_from_snapshot(doc) == [("A", "n1")]

    def test_link_mark_text_excluded(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(
            _para(
                "n1",
                _text("[[A]] outside, "),
                _text("[[B]] inside link", marks=[{"type": "link", "attrs": {"href": "https://x"}}]),
                _text(" and [[C]]"),
            )
        )
        assert extract_wikilinks_from_snapshot(doc) == [("A", "n1"), ("C", "n1")]

    def test_nested_double_brackets_rejected(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        # `[[[[foo]]]]` — 외측 매칭 시 group1 안에 `[[` 포함 → 거부
        doc = _doc(_para("n1", _text("[[[[foo]]]]")))
        assert extract_wikilinks_from_snapshot(doc) == []

    def test_empty_brackets_rejected(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(_para("n1", _text("nothing [[]] here")))
        assert extract_wikilinks_from_snapshot(doc) == []

    def test_newline_inside_rejected(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(_para("n1", _text("split [[foo\nbar]] here")))
        assert extract_wikilinks_from_snapshot(doc) == []

    def test_pipe_only_no_target_rejected(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        # `[[|alias]]` — group 1 길이 0 → {1,200} 불일치
        doc = _doc(_para("n1", _text("ghost [[|alias]] gone")))
        assert extract_wikilinks_from_snapshot(doc) == []

    def test_too_long_target_rejected(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        long_title = "a" * 201
        doc = _doc(_para("n1", _text(f"[[{long_title}]]")))
        assert extract_wikilinks_from_snapshot(doc) == []

    def test_max_length_accepted(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        max_title = "b" * 200
        doc = _doc(_para("n1", _text(f"[[{max_title}]]")))
        assert extract_wikilinks_from_snapshot(doc) == [(max_title, "n1")]


class TestNodeAttribution:
    def test_section_nested_paragraph(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        # section 컨테이너 — section 의 node_id 는 carry 되고 inner paragraph 가 우선
        doc = _doc(
            {
                "type": "section",
                "attrs": {"node_id": "sec1"},
                "content": [
                    _para("p1", _text("[[A]]")),
                    _para("p2", _text("[[B]]")),
                ],
            }
        )
        # paragraph 의 attrs.node_id 가 우선
        assert extract_wikilinks_from_snapshot(doc) == [("A", "p1"), ("B", "p2")]

    def test_listitem_paragraph_uses_paragraph_node_id(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(
            {
                "type": "bulletList",
                "attrs": {"node_id": "list1"},
                "content": [
                    {
                        "type": "listItem",
                        "content": [_para("item1", _text("- [[A]]"))],
                    },
                    {
                        "type": "listItem",
                        "content": [_para("item2", _text("- [[B]]"))],
                    },
                ],
            }
        )
        assert extract_wikilinks_from_snapshot(doc) == [("A", "item1"), ("B", "item2")]

    def test_listitem_without_paragraph_node_id_falls_back_to_list(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        # paragraph 에 attrs.node_id 부재 — bulletList 의 node_id 가 carry 됨
        doc = _doc(
            {
                "type": "bulletList",
                "attrs": {"node_id": "list1"},
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [_text("[[A]]")],
                            }
                        ],
                    }
                ],
            }
        )
        assert extract_wikilinks_from_snapshot(doc) == [("A", "list1")]

    def test_text_without_any_node_id_dropped(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        # carry 가 아무것도 없는 경우 — anchor 불가능 → 스킵
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [_text("[[A]]")],
                }
            ],
        }
        assert extract_wikilinks_from_snapshot(doc) == []


class TestInputResilience:
    def test_none_snapshot(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        assert extract_wikilinks_from_snapshot(None) == []

    def test_non_dict_snapshot(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        assert extract_wikilinks_from_snapshot([]) == []  # type: ignore[arg-type]
        assert extract_wikilinks_from_snapshot("not a doc") == []  # type: ignore[arg-type]

    def test_no_content_key(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        assert extract_wikilinks_from_snapshot({"type": "doc"}) == []

    def test_content_not_list(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        assert extract_wikilinks_from_snapshot({"type": "doc", "content": "x"}) == []


class TestBoundaryEdgeCases:
    def test_unbalanced_brackets_no_match(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(_para("n1", _text("[[Foo] and [Foo]] and [Foo]")))
        assert extract_wikilinks_from_snapshot(doc) == []

    def test_single_bracket_pairs_ignored(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(_para("n1", _text("see [Foo](url) markdown link")))
        assert extract_wikilinks_from_snapshot(doc) == []

    def test_whitespace_in_target_preserved(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(_para("n1", _text("[[Multi Word Title]]")))
        assert extract_wikilinks_from_snapshot(doc) == [("Multi Word Title", "n1")]

    def test_outer_whitespace_stripped(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        doc = _doc(_para("n1", _text("[[  spaced  ]]")))
        # raw_text 는 strip 후 결과
        assert extract_wikilinks_from_snapshot(doc) == [("spaced", "n1")]

    def test_pipe_with_empty_alias(self):
        from app.services.wikilink_rules import extract_wikilinks_from_snapshot

        # `[[Foo|]]` — group 2 가 {1,200} 길이 강제이므로 alias 부 매칭 실패 → 전체 alt 패스 (group 2 = None)
        doc = _doc(_para("n1", _text("[[Foo|]]")))
        # `[[Foo|]]` 는 정규식의 alt branch 둘 다 시도: (a) 단순 [[Foo]] — 후행 |]] 가 남아서 매칭 안되고,
        # (b) [[Foo|alias]] — alias 길이 0 이라 매칭 안됨. 결과 0건.
        assert extract_wikilinks_from_snapshot(doc) == []
