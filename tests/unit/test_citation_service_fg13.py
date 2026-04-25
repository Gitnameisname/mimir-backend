"""S3 Phase 1 FG 1-3 Step 2 — citation 좌표 유효성 유닛.

content_snapshot(ProseMirror doc) 의 block 노드 attrs.node_id 가 citation 의
node_id 좌표와 동일한 지, content_hash 가 편집 후 달라지는지 확인한다.

범위:
  * nodes_from_prosemirror 로 flat node 추출 후 각 node.id 가 UUID 포맷
  * 문서 일부를 수정하면 영향받은 노드만 content 변경
  * sha256(content_text) 재계산 로직 (citation_service 와 대칭)
"""
from __future__ import annotations

import hashlib
import re

import pytest

from app.services.snapshot_sync_service import (
    nodes_from_prosemirror,
    prosemirror_from_nodes,
    prosemirror_from_text,
)

pytestmark = pytest.mark.unit


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestCitationCoordinateValidity:
    def test_all_block_nodes_have_uuid_v4_compatible_id(self):
        """prosemirror_from_nodes 결과 block 노드 모두 UUID 포맷 node_id."""
        nodes = [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "parent_id": None,
                "node_type": "paragraph",
                "order_index": 0,
                "content": "첫 문단",
            },
            {
                "id": None,  # 자동 생성 경로
                "parent_id": None,
                "node_type": "paragraph",
                "order_index": 1,
                "content": "둘째 문단",
            },
        ]
        doc = prosemirror_from_nodes(nodes)
        for block in doc["content"]:
            nid = block.get("attrs", {}).get("node_id")
            assert UUID_RE.match(nid or ""), f"invalid node_id: {nid}"

    def test_edit_changes_content_hash_only_for_affected_node(self):
        """한 노드 content 만 수정 → 해당 노드 sha256 만 바뀐다."""
        original_nodes = [
            {"id": "11111111-1111-4111-8111-111111111111", "parent_id": None,
             "node_type": "paragraph", "order_index": 0, "content": "원본 A"},
            {"id": "22222222-2222-4222-8222-222222222222", "parent_id": None,
             "node_type": "paragraph", "order_index": 1, "content": "원본 B"},
        ]
        edited_nodes = [
            dict(original_nodes[0]),
            {**original_nodes[1], "content": "편집 B"},
        ]

        original_doc = prosemirror_from_nodes(original_nodes)
        edited_doc = prosemirror_from_nodes(edited_nodes)

        original_flat = {n["id"]: n for n in nodes_from_prosemirror(original_doc)}
        edited_flat = {n["id"]: n for n in nodes_from_prosemirror(edited_doc)}

        assert set(original_flat.keys()) == set(edited_flat.keys())

        # node A 의 content_hash 동일
        a_id = "11111111-1111-4111-8111-111111111111"
        assert _sha256(original_flat[a_id]["content"] or "") == _sha256(edited_flat[a_id]["content"] or "")

        # node B 의 content_hash 다름
        b_id = "22222222-2222-4222-8222-222222222222"
        assert _sha256(original_flat[b_id]["content"] or "") != _sha256(edited_flat[b_id]["content"] or "")

    def test_prosemirror_from_text_node_id_is_uuid(self):
        """agent_proposal 경유 — prosemirror_from_text 결과 단일 paragraph 의 node_id 가 UUID."""
        doc = prosemirror_from_text("에이전트 제안 본문")
        assert len(doc["content"]) == 1
        nid = doc["content"][0]["attrs"]["node_id"]
        assert UUID_RE.match(nid)

    def test_node_id_preserved_across_roundtrip(self):
        """prosemirror_from_nodes → nodes_from_prosemirror 왕복 시 node_id 집합 불변."""
        original = [
            {"id": f"1111111{i}-1111-4111-8111-111111111111", "parent_id": None,
             "node_type": "paragraph", "order_index": i, "content": f"line {i}"}
            for i in range(3)
        ]
        doc = prosemirror_from_nodes(original)
        back = nodes_from_prosemirror(doc)
        assert {n["id"] for n in back} == {n["id"] for n in original}
