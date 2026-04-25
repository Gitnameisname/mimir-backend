"""S3 Phase 1 FG 1-1 — snapshot_sync_service 유닛 테스트.

커버 범위:
  * prosemirror_from_nodes / nodes_from_prosemirror 왕복 보존
  * is_valid_prosemirror_doc 분기
  * prosemirror_from_text 기본 shape
  * section 중첩 / list 줄 분해 / heading level
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.snapshot_sync_service import (
    CONTENT_SNAPSHOT_SCHEMA_VERSION,
    is_valid_prosemirror_doc,
    nodes_from_prosemirror,
    prosemirror_from_nodes,
    prosemirror_from_text,
    rebuild_nodes_from_snapshot,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# is_valid_prosemirror_doc
# --------------------------------------------------------------------------- #


class TestIsValidProseMirrorDoc:
    def test_valid_empty_doc(self) -> None:
        assert is_valid_prosemirror_doc({"type": "doc", "content": []}) is True

    def test_valid_doc_with_content(self) -> None:
        doc = {"type": "doc", "content": [{"type": "paragraph", "content": []}]}
        assert is_valid_prosemirror_doc(doc) is True

    def test_rejects_non_dict(self) -> None:
        assert is_valid_prosemirror_doc("str") is False
        assert is_valid_prosemirror_doc(None) is False
        assert is_valid_prosemirror_doc([]) is False

    def test_rejects_non_doc_type(self) -> None:
        assert is_valid_prosemirror_doc({"type": "document", "content": []}) is False
        assert is_valid_prosemirror_doc({"type": "text", "content": "hello"}) is False

    def test_rejects_missing_content_list(self) -> None:
        assert is_valid_prosemirror_doc({"type": "doc"}) is False
        assert is_valid_prosemirror_doc({"type": "doc", "content": None}) is False
        assert is_valid_prosemirror_doc({"type": "doc", "content": "str"}) is False


# --------------------------------------------------------------------------- #
# prosemirror_from_nodes
# --------------------------------------------------------------------------- #


class TestProseMirrorFromNodes:
    def test_empty_nodes_returns_empty_doc(self) -> None:
        doc = prosemirror_from_nodes([])
        assert doc["type"] == "doc"
        assert doc["schema_version"] == CONTENT_SNAPSHOT_SCHEMA_VERSION
        assert doc["content"] == []

    def test_single_paragraph_block(self) -> None:
        node = {
            "id": "11111111-1111-4111-8111-111111111111",
            "parent_id": None,
            "node_type": "paragraph",
            "order_index": 0,
            "content": "Hello",
            "metadata": {},
        }
        doc = prosemirror_from_nodes([node])
        assert doc["content"] == [
            {
                "type": "paragraph",
                "attrs": {"node_id": "11111111-1111-4111-8111-111111111111"},
                "content": [{"type": "text", "text": "Hello"}],
            }
        ]

    def test_heading_uses_level_from_metadata(self) -> None:
        node = {
            "id": "22222222-2222-4222-8222-222222222222",
            "parent_id": None,
            "node_type": "heading",
            "order_index": 0,
            "content": "제목",
            "metadata": {"level": 3},
        }
        doc = prosemirror_from_nodes([node])
        assert doc["content"][0]["attrs"]["level"] == 3
        assert doc["content"][0]["attrs"]["node_id"].startswith("22222222")

    def test_heading_falls_back_to_title_when_no_content(self) -> None:
        node = {
            "id": "33333333-3333-4333-8333-333333333333",
            "parent_id": None,
            "node_type": "heading",
            "order_index": 0,
            "title": "타이틀 폴백",
            "content": None,
            "metadata": {},
        }
        doc = prosemirror_from_nodes([node])
        texts = [c["text"] for c in doc["content"][0]["content"]]
        assert texts == ["타이틀 폴백"]

    def test_list_splits_content_lines(self) -> None:
        node = {
            "id": "44444444-4444-4444-8444-444444444444",
            "parent_id": None,
            "node_type": "list",
            "order_index": 0,
            "content": "첫째\n둘째\n\n셋째",
            "metadata": {},
        }
        doc = prosemirror_from_nodes([node])
        bullet = doc["content"][0]
        assert bullet["type"] == "bulletList"
        assert len(bullet["content"]) == 3  # 빈 줄은 건너뜀
        first_item = bullet["content"][0]
        assert first_item["type"] == "listItem"
        assert first_item["content"][0]["content"][0]["text"] == "첫째"

    def test_list_empty_content_gets_one_placeholder_item(self) -> None:
        node = {
            "id": "55555555-5555-4555-8555-555555555555",
            "parent_id": None,
            "node_type": "list",
            "order_index": 0,
            "content": "",
            "metadata": {},
        }
        doc = prosemirror_from_nodes([node])
        bullet = doc["content"][0]
        assert len(bullet["content"]) == 1

    def test_code_block_preserves_language_attr(self) -> None:
        node = {
            "id": "66666666-6666-4666-8666-666666666666",
            "parent_id": None,
            "node_type": "code_block",
            "order_index": 0,
            "content": "print('hi')",
            "metadata": {"language": "python"},
        }
        doc = prosemirror_from_nodes([node])
        cb = doc["content"][0]
        assert cb["type"] == "codeBlock"
        assert cb["attrs"]["language"] == "python"
        assert cb["content"][0]["text"] == "print('hi')"

    def test_section_nests_children(self) -> None:
        section_id = "77777777-7777-4777-8777-777777777777"
        child_id = "88888888-8888-4888-8888-888888888888"
        nodes = [
            {
                "id": section_id,
                "parent_id": None,
                "node_type": "section",
                "order_index": 0,
                "title": "1장",
                "metadata": {},
            },
            {
                "id": child_id,
                "parent_id": section_id,
                "node_type": "paragraph",
                "order_index": 0,
                "content": "child",
                "metadata": {},
            },
        ]
        doc = prosemirror_from_nodes(nodes)
        section = doc["content"][0]
        assert section["type"] == "section"
        assert section["attrs"]["title"] == "1장"
        assert len(section["content"]) == 1
        assert section["content"][0]["type"] == "paragraph"
        assert section["content"][0]["attrs"]["node_id"] == child_id

    def test_generates_node_id_when_missing(self) -> None:
        node = {
            "id": None,
            "parent_id": None,
            "node_type": "paragraph",
            "order_index": 0,
            "content": "x",
            "metadata": {},
        }
        doc = prosemirror_from_nodes([node])
        nid = doc["content"][0]["attrs"]["node_id"]
        assert isinstance(nid, str) and len(nid) == 36

    def test_sorts_by_order_index(self) -> None:
        nodes = [
            {"id": f"aaaaaaa{i}-aaaa-4aaa-8aaa-aaaaaaaaaaaa".replace("aaaaaaa" + str(i), f"aaaaaaa{i}"), "parent_id": None, "node_type": "paragraph", "order_index": i, "content": str(i), "metadata": {}}
            for i in [2, 0, 1]
        ]
        doc = prosemirror_from_nodes(nodes)
        texts = [c["content"][0]["text"] for c in doc["content"]]
        assert texts == ["0", "1", "2"]

    def test_accepts_order_field_alias(self) -> None:
        """프런트 DraftNodeItem 은 ``order`` 를 쓴다. alias 지원."""
        node = {
            "id": "99999999-9999-4999-8999-999999999999",
            "parent_id": None,
            "node_type": "paragraph",
            "order": 7,
            "content": "x",
            "metadata": {},
        }
        doc = prosemirror_from_nodes([node])
        assert len(doc["content"]) == 1


# --------------------------------------------------------------------------- #
# nodes_from_prosemirror
# --------------------------------------------------------------------------- #


class TestNodesFromProseMirror:
    def test_invalid_doc_returns_empty(self) -> None:
        assert nodes_from_prosemirror({"type": "document", "content": []}) == []
        assert nodes_from_prosemirror(None) == []
        assert nodes_from_prosemirror("not json") == []

    def test_parses_paragraph(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "attrs": {"node_id": "11111111-1111-4111-8111-111111111111"},
                    "content": [{"type": "text", "text": "hello"}],
                }
            ],
        }
        nodes = nodes_from_prosemirror(doc)
        assert len(nodes) == 1
        assert nodes[0]["node_type"] == "paragraph"
        assert nodes[0]["content"] == "hello"
        assert nodes[0]["parent_id"] is None
        assert nodes[0]["order_index"] == 0

    def test_parses_list_joins_lines(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "bulletList",
                    "attrs": {"node_id": "aa111111-1111-4111-8111-111111111111"},
                    "content": [
                        {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "가"}]}]},
                        {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "나"}]}]},
                    ],
                }
            ],
        }
        nodes = nodes_from_prosemirror(doc)
        assert len(nodes) == 1
        assert nodes[0]["node_type"] == "list"
        assert nodes[0]["content"] == "가\n나"

    def test_section_children_get_parent_id(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "section",
                    "attrs": {"node_id": "sec1-1111-4111-8111-111111111111", "title": "1장"},
                    "content": [
                        {
                            "type": "paragraph",
                            "attrs": {"node_id": "chi1-1111-4111-8111-111111111111"},
                            "content": [{"type": "text", "text": "자식"}],
                        }
                    ],
                }
            ],
        }
        nodes = nodes_from_prosemirror(doc)
        assert len(nodes) == 2
        assert nodes[0]["node_type"] == "section"
        assert nodes[0]["title"] == "1장"
        assert nodes[1]["parent_id"] == nodes[0]["id"]
        assert nodes[1]["node_type"] == "paragraph"


# --------------------------------------------------------------------------- #
# 왕복 (nodes → doc → nodes) 보존
# --------------------------------------------------------------------------- #


class TestRoundtripPreservation:
    def test_roundtrip_paragraph_preserves_id_and_text(self) -> None:
        original = [{
            "id": "11111111-1111-4111-8111-111111111111",
            "parent_id": None,
            "node_type": "paragraph",
            "order_index": 0,
            "content": "Hello",
            "metadata": {},
        }]
        doc = prosemirror_from_nodes(original)
        back = nodes_from_prosemirror(doc)
        assert len(back) == 1
        assert back[0]["id"] == original[0]["id"]
        assert back[0]["content"] == "Hello"
        assert back[0]["node_type"] == "paragraph"

    def test_roundtrip_section_with_children(self) -> None:
        # section id / child parent_id 는 유효 UUID v4 형식이어야 한다 —
        # `_ensure_node_id` 가 invalid UUID 를 새 값으로 교체하면 parent_id 체인이 깨진다.
        section_id = "aaaaaaaa-0000-4111-8111-111111111111"
        child_id = "bbbbbbbb-0000-4111-8111-111111111111"
        original = [
            {"id": section_id, "parent_id": None, "node_type": "section",
             "order_index": 0, "title": "1장", "metadata": {}},
            {"id": child_id, "parent_id": section_id,
             "node_type": "paragraph", "order_index": 0, "content": "cc", "metadata": {}},
        ]
        doc = prosemirror_from_nodes(original)
        back = nodes_from_prosemirror(doc)
        assert len(back) == 2
        ids = {n["id"] for n in back}
        assert ids == {o["id"] for o in original}
        # section / child 관계 보존
        sec = next(n for n in back if n["node_type"] == "section")
        child = next(n for n in back if n["node_type"] == "paragraph")
        assert child["parent_id"] == sec["id"]
        assert sec["title"] == "1장"


# --------------------------------------------------------------------------- #
# prosemirror_from_text
# --------------------------------------------------------------------------- #


class TestProseMirrorFromText:
    def test_basic_text_wraps_in_paragraph(self) -> None:
        doc = prosemirror_from_text("안녕")
        assert doc["type"] == "doc"
        assert doc["schema_version"] == CONTENT_SNAPSHOT_SCHEMA_VERSION
        assert len(doc["content"]) == 1
        p = doc["content"][0]
        assert p["type"] == "paragraph"
        assert p["content"][0]["text"] == "안녕"
        assert "node_id" in p["attrs"]

    def test_empty_text_yields_empty_paragraph(self) -> None:
        doc = prosemirror_from_text("")
        assert doc["content"][0]["type"] == "paragraph"
        assert doc["content"][0]["content"] == []

    def test_none_text_yields_empty_paragraph(self) -> None:
        doc = prosemirror_from_text(None)
        assert doc["content"][0]["type"] == "paragraph"
        assert doc["content"][0]["content"] == []


# --------------------------------------------------------------------------- #
# rebuild_nodes_from_snapshot — nodes_repository replace 호출 확인
# --------------------------------------------------------------------------- #


class TestRebuildNodesFromSnapshot:
    def test_calls_replace_for_version_with_converted_nodes(self, monkeypatch) -> None:
        from app.repositories import nodes_repository as nr_mod
        mock_repo = MagicMock()
        mock_repo.replace_for_version.return_value = []
        monkeypatch.setattr(nr_mod, "nodes_repository", mock_repo)

        conn = MagicMock()
        doc = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "attrs": {"node_id": "11111111-1111-4111-8111-111111111111"},
                 "content": [{"type": "text", "text": "x"}]}
            ],
        }
        result = rebuild_nodes_from_snapshot(conn, "ver-1", doc)
        assert len(result) == 1
        mock_repo.replace_for_version.assert_called_once()
        args = mock_repo.replace_for_version.call_args[0]
        assert args[0] is conn
        assert args[1] == "ver-1"
        assert args[2][0]["content"] == "x"

    def test_invalid_snapshot_clears_nodes(self, monkeypatch) -> None:
        from app.repositories import nodes_repository as nr_mod
        mock_repo = MagicMock()
        mock_repo.replace_for_version.return_value = []
        monkeypatch.setattr(nr_mod, "nodes_repository", mock_repo)

        conn = MagicMock()
        result = rebuild_nodes_from_snapshot(conn, "ver-1", {"type": "text", "content": "x"})
        assert result == []
        mock_repo.replace_for_version.assert_called_once_with(conn, "ver-1", [])
