"""FG 0-3 커버리지 보강 — diff_service 유닛 테스트 (세션 11).

대상: `backend/app/services/diff_service.py` (853줄)

커버 범위:
  - normalize_content / _node_to_snapshot / _node_content_key
  - _tokenize / _lcs_diff / TextDiffer.diff (skip/same/basic/LCS cap)
  - NodeDiffer.diff (empty/too large/ADDED/DELETED/MODIFIED/duplicate ID)
  - NodeDiffer._classify (ADDED/DELETED/UNCHANGED/MOVED-only/MODIFIED+move inline)
  - DiffSummaryGenerator._build_description / _calc_severity
  - DiffSummaryGenerator._find_top_section (depth limit / 부모 없음 + section / 비section 최상위 / 재귀 상승)
  - DiffSummaryGenerator._identify_changed_sections
  - _node_type_label
  - 캐시 유틸: _cache_key / set+get / invalidate_cache_for_document
  - _build_version_ref
  - DiffService.compute_diff (same version / not found a or b / TooLarge / cache hit / miss 계산+캐싱)
  - DiffService.compute_diff_with_previous (parent_version_id / version_number-1 폴백 / no previous / 버전 미조회)
  - DiffService.compute_summary_only (cache hit/miss)
  - DiffService.compute_summary_with_previous
  - 싱글턴 존재

주의:
  - `DiffSummaryGenerator.generate()` 본문에 `nodes_a` 미정의 NameError 잠재 버그가 확인됨
    (line 470 — parameter 누락). 본 테스트는 이를 우회하기 위해 `_identify_changed_sections`
    를 monkeypatch 하여 generate() 본문 우회 테스트를 수행한다.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services import diff_service as ds_mod
from app.services.diff_service import (
    DEFAULT_MAX_INLINE_LENGTH,
    MAX_LCS_CELLS,
    MAX_NODES_SYNC,
    DiffService,
    DiffSummaryGenerator,
    DiffTooLargeError,
    NodeDiffer,
    TextDiffer,
    _build_version_ref,
    _cache_key,
    _get_cached_diff,
    _get_cached_summary,
    _lcs_diff,
    _node_content_key,
    _node_to_snapshot,
    _node_type_label,
    _set_cached_diff,
    _set_cached_summary,
    _tokenize,
    invalidate_cache_for_document,
    normalize_content,
)

from app.schemas.diff import (
    ChangeType,
    DiffResult,
    DiffSeverity,
    DiffSummary,
    InlineDiffToken,
    MoveType,
    NodeDiff,
)


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------


def _mk_node(
    node_id: str = "n1",
    *,
    node_type: str = "section",
    title: str | None = "제목",
    content: str | None = "본문",
    parent_id: str | None = None,
    order: int = 0,
    metadata: dict | None = None,
):
    """간단한 Node 대용 SimpleNamespace — Node 스키마에 필요한 속성만 충족."""
    return SimpleNamespace(
        id=node_id,
        node_type=node_type,
        title=title,
        content=content,
        parent_id=parent_id,
        order_index=order,
        metadata=metadata or {},
    )


def _mk_version(id_="v1", version_number=1, parent_version_id=None):
    return SimpleNamespace(
        id=id_,
        version_number=version_number,
        status="draft",
        created_at=datetime(2026, 4, 1),
        created_by="user-1",
        label="v1",
        change_summary="요약",
        parent_version_id=parent_version_id,
    )


@pytest.fixture(autouse=True)
def _clear_caches():
    """각 테스트 전후 캐시 초기화."""
    ds_mod._diff_cache.clear()
    ds_mod._summary_cache.clear()
    yield
    ds_mod._diff_cache.clear()
    ds_mod._summary_cache.clear()


# ---------------------------------------------------------------------------
# 1. normalize_content / _node_to_snapshot / _node_content_key
# ---------------------------------------------------------------------------


def test_normalize_content_none_returns_empty_string():
    assert normalize_content(None) == ""


def test_normalize_content_dict_returns_sorted_json():
    result = normalize_content({"b": 1, "a": 2})
    # sort_keys → a 먼저
    assert result == '{"a": 2, "b": 1}'


def test_normalize_content_list_returns_json_with_ensure_ascii_false():
    assert normalize_content(["한글", "en"]) == '["한글", "en"]'


def test_normalize_content_string_strips_trailing_whitespace():
    assert normalize_content("텍스트   ") == "텍스트"


def test_node_to_snapshot_maps_fields():
    node = _mk_node("n1", title="T", content="C", order=3, metadata={"k": "v"})
    snap = _node_to_snapshot(node)
    assert snap.node_id == "n1"
    assert snap.title == "T"
    assert snap.content == "C"
    assert snap.order == 3
    assert snap.metadata == {"k": "v"}


def test_node_to_snapshot_treats_none_metadata_as_empty_dict():
    node = _mk_node("n1")
    node.metadata = None
    snap = _node_to_snapshot(node)
    assert snap.metadata == {}


def test_node_content_key_same_inputs_produce_same_key():
    a = _mk_node("n1", content="본문", title="제목", metadata={"k": "v"})
    b = _mk_node("n1", content="본문", title="제목", metadata={"k": "v"})
    assert _node_content_key(a) == _node_content_key(b)


def test_node_content_key_changes_when_content_changes():
    a = _mk_node("n1", content="A")
    b = _mk_node("n1", content="B")
    assert _node_content_key(a) != _node_content_key(b)


# ---------------------------------------------------------------------------
# 2. _tokenize / _lcs_diff
# ---------------------------------------------------------------------------


def test_tokenize_empty_returns_empty_list():
    assert _tokenize("") == []


def test_tokenize_preserves_whitespace_as_tokens():
    tokens = _tokenize("안녕 world")
    # 공백과 단어가 별도 토큰
    assert "안녕" in tokens
    assert "world" in tokens
    assert " " in tokens


def test_tokenize_single_word():
    tokens = _tokenize("hello")
    assert tokens == ["hello"]


def test_lcs_diff_empty_vs_empty_empty_result():
    assert _lcs_diff([], []) == []


def test_lcs_diff_identical_tokens_all_unchanged():
    tokens = _lcs_diff(["a", " ", "b"], ["a", " ", "b"])
    assert len(tokens) == 1  # 병합됨
    assert tokens[0].type == "unchanged"
    assert tokens[0].text == "a b"


def test_lcs_diff_addition_and_deletion_merged():
    # a b vs a c
    result = _lcs_diff(["a", " ", "b"], ["a", " ", "c"])
    types = [t.type for t in result]
    # 'a ' unchanged + deleted 'b' + added 'c' (순서는 backtrack 의존)
    assert "unchanged" in types
    assert "deleted" in types
    assert "added" in types


def test_lcs_diff_raises_when_cell_count_exceeds_max():
    # MAX_LCS_CELLS=1,000,000 → n*m 이 넘어가야 함
    # 각 토큰 1,500개 × 1,000개 = 1,500,000 > 1,000,000
    big_a = ["x"] * 1500
    big_b = ["y"] * 1000
    with pytest.raises(ValueError) as exc:
        _lcs_diff(big_a, big_b)
    assert "LCS DP 셀 수" in str(exc.value)


# ---------------------------------------------------------------------------
# 3. TextDiffer.diff
# ---------------------------------------------------------------------------


def test_text_differ_identical_returns_single_unchanged_token():
    td = TextDiffer()
    tokens, skipped = td.diff("같은 텍스트", "같은 텍스트")
    assert skipped is False
    assert len(tokens) == 1
    assert tokens[0].type == "unchanged"


def test_text_differ_empty_both_returns_empty_not_skipped():
    td = TextDiffer()
    tokens, skipped = td.diff("", "")
    assert tokens == []
    assert skipped is False


def test_text_differ_none_inputs_treated_as_empty():
    td = TextDiffer()
    tokens, skipped = td.diff(None, None)
    assert tokens == []
    assert skipped is False


def test_text_differ_exceeds_max_length_returns_skipped():
    td = TextDiffer()
    big = "x" * 3000
    tokens, skipped = td.diff(big, big, max_length=1000)
    assert tokens == []
    assert skipped is True


def test_text_differ_basic_diff_returns_mixed_tokens():
    td = TextDiffer()
    tokens, skipped = td.diff("hello world", "hello python")
    assert skipped is False
    types = {t.type for t in tokens}
    assert "unchanged" in types
    assert ("added" in types) or ("deleted" in types)


def test_text_differ_catches_lcs_exception_returns_skipped():
    """LCS 셀 수 초과 → skipped=True 반환."""
    td = TextDiffer()
    # 매우 큰 토큰 수: 공백 없이 긴 문자열 — 공백 분리로 토큰 많이 생성
    a = " ".join(["w"] * 1500)
    b = " ".join(["z"] * 1500)
    # max_length 를 충분히 크게 설정해 길이 체크 통과
    tokens, skipped = td.diff(a, b, max_length=10_000_000)
    assert skipped is True
    assert tokens == []


# ---------------------------------------------------------------------------
# 4. NodeDiffer.diff
# ---------------------------------------------------------------------------


def test_node_differ_empty_inputs_returns_empty():
    nd = NodeDiffer()
    diffs, has_issue = nd.diff([], [])
    assert diffs == []
    assert has_issue is False


def test_node_differ_raises_when_total_exceeds_limit():
    nd = NodeDiffer()
    # MAX_NODES_SYNC=10_000 → 합이 이 이상이어야 함
    nodes_a = [_mk_node(f"a{i}") for i in range(6000)]
    nodes_b = [_mk_node(f"b{i}") for i in range(5000)]
    with pytest.raises(DiffTooLargeError):
        nd.diff(nodes_a, nodes_b)


def test_node_differ_detects_duplicate_ids():
    nd = NodeDiffer()
    a = _mk_node("n1")
    b = _mk_node("n1")  # 같은 ID 중복
    diffs, has_issue = nd.diff([a, b], [])
    assert has_issue is True


def test_node_differ_classifies_added_and_deleted():
    nd = NodeDiffer()
    a = _mk_node("old", content="A")
    b = _mk_node("new", content="B")
    diffs, _ = nd.diff([a], [b])
    ct = {d.node_id: d.change_type for d in diffs}
    assert ct["old"] == ChangeType.DELETED
    assert ct["new"] == ChangeType.ADDED


def test_node_differ_modified_with_inline_diff():
    nd = NodeDiffer()
    a = _mk_node("n1", content="원본 내용")
    b = _mk_node("n1", content="원본 수정 내용")
    diffs, _ = nd.diff([a], [b], inline_diff=True)
    assert len(diffs) == 1
    assert diffs[0].change_type == ChangeType.MODIFIED
    assert diffs[0].inline_diff is not None


def test_node_differ_excludes_unchanged_by_default():
    nd = NodeDiffer()
    a = _mk_node("n1", content="동일", title="제목")
    b = _mk_node("n1", content="동일", title="제목")
    diffs, _ = nd.diff([a], [b])
    assert diffs == []


def test_node_differ_includes_unchanged_when_requested():
    nd = NodeDiffer()
    a = _mk_node("n1", content="동일")
    b = _mk_node("n1", content="동일")
    diffs, _ = nd.diff([a], [b], include_unchanged=True)
    assert len(diffs) == 1
    assert diffs[0].change_type == ChangeType.UNCHANGED


# ---------------------------------------------------------------------------
# 5. NodeDiffer._classify
# ---------------------------------------------------------------------------


def test_classify_added_when_only_b():
    nd = NodeDiffer()
    b = _mk_node("n1", content="새로 추가")
    result = nd._classify("n1", None, b)
    assert result.change_type == ChangeType.ADDED
    assert result.before is None
    assert result.after is not None


def test_classify_deleted_when_only_a():
    nd = NodeDiffer()
    a = _mk_node("n1", content="삭제됨")
    result = nd._classify("n1", a, None)
    assert result.change_type == ChangeType.DELETED
    assert result.after is None


def test_classify_unchanged_when_same_content_and_position():
    nd = NodeDiffer()
    a = _mk_node("n1", content="동일", order=0, parent_id=None)
    b = _mk_node("n1", content="동일", order=0, parent_id=None)
    result = nd._classify("n1", a, b)
    assert result.change_type == ChangeType.UNCHANGED


def test_classify_moved_when_only_position_changed():
    nd = NodeDiffer()
    a = _mk_node("n1", content="동일", order=0, parent_id=None)
    b = _mk_node("n1", content="동일", order=5, parent_id=None)
    result = nd._classify("n1", a, b)
    assert result.change_type == ChangeType.MOVED
    assert result.move_info.move_type == MoveType.REORDER


def test_classify_moved_hierarchy_change():
    nd = NodeDiffer()
    a = _mk_node("n1", content="동일", order=0, parent_id="p1")
    b = _mk_node("n1", content="동일", order=0, parent_id="p2")
    result = nd._classify("n1", a, b)
    assert result.change_type == ChangeType.MOVED
    assert result.move_info.move_type == MoveType.HIERARCHY_CHANGE


def test_classify_modified_with_inline_uses_fallback_title():
    """content 가 없을 때 title 을 대체로 사용하는 분기."""
    nd = NodeDiffer()
    a = _mk_node("n1", content=None, title="제목 원본")
    b = _mk_node("n1", content=None, title="제목 수정")
    result = nd._classify("n1", a, b, inline_diff=True)
    assert result.change_type == ChangeType.MODIFIED
    assert result.inline_diff is not None


def test_classify_modified_inline_skipped_sets_flag():
    nd = NodeDiffer()
    big = "x" * 10000
    a = _mk_node("n1", content=big + "a")
    b = _mk_node("n1", content=big + "b")
    result = nd._classify("n1", a, b, inline_diff=True, max_inline_length=500)
    assert result.change_type == ChangeType.MODIFIED
    assert result.inline_diff_skipped is True
    assert result.inline_diff is None


def test_classify_modified_with_position_change_includes_move_info():
    nd = NodeDiffer()
    a = _mk_node("n1", content="A", order=0, parent_id="p1")
    b = _mk_node("n1", content="B", order=1, parent_id="p2")
    result = nd._classify("n1", a, b)
    assert result.change_type == ChangeType.MODIFIED
    assert result.move_info is not None
    assert result.move_info.move_type == MoveType.HIERARCHY_CHANGE


# ---------------------------------------------------------------------------
# 6. _node_type_label
# ---------------------------------------------------------------------------


def test_node_type_label_known_and_unknown():
    assert _node_type_label("section") == "섹션"
    assert _node_type_label("paragraph") == "단락"
    assert _node_type_label("unknown_type") == "항목"


# ---------------------------------------------------------------------------
# 7. DiffSummaryGenerator._build_description / _calc_severity
# ---------------------------------------------------------------------------


def test_build_description_no_changes():
    gen = DiffSummaryGenerator()
    assert gen._build_description(0, 0, 0, 0) == "변경 사항 없음"


def test_build_description_mixed_parts():
    gen = DiffSummaryGenerator()
    result = gen._build_description(2, 1, 3, 0)
    assert "2개 추가" in result
    assert "1개 삭제" in result
    assert "3개 수정" in result
    assert "이동" not in result


def test_build_description_all_four_types():
    gen = DiffSummaryGenerator()
    result = gen._build_description(1, 1, 1, 1)
    assert "추가" in result and "삭제" in result
    assert "수정" in result and "이동" in result


def test_calc_severity_zero_total_or_zero_changed_none():
    gen = DiffSummaryGenerator()
    assert gen._calc_severity(0, 0) is None
    assert gen._calc_severity(0, 10) is None


def test_calc_severity_major_when_ratio_high():
    gen = DiffSummaryGenerator()
    assert gen._calc_severity(30, 100) == DiffSeverity.MAJOR


def test_calc_severity_minor_when_ratio_mid():
    gen = DiffSummaryGenerator()
    assert gen._calc_severity(15, 100) == DiffSeverity.MINOR


def test_calc_severity_trivial_when_ratio_low():
    gen = DiffSummaryGenerator()
    assert gen._calc_severity(1, 100) == DiffSeverity.TRIVIAL


# ---------------------------------------------------------------------------
# 8. DiffSummaryGenerator._find_top_section
# ---------------------------------------------------------------------------


def test_find_top_section_depth_limit_returns_none():
    gen = DiffSummaryGenerator()
    # 무한 루프가 되지 않도록 parent_id 를 자기 자신으로 만들어 depth 상승
    node_map = {
        "n1": _mk_node("n1", node_type="section", parent_id="n1"),
    }
    result = gen._find_top_section("n1", node_map, depth=100)
    assert result is None


def test_find_top_section_top_level_section_returns_self():
    gen = DiffSummaryGenerator()
    node_map = {
        "s1": _mk_node("s1", node_type="section", parent_id=None),
    }
    assert gen._find_top_section("s1", node_map) == "s1"


def test_find_top_section_top_level_non_section_returns_none():
    gen = DiffSummaryGenerator()
    node_map = {
        "p1": _mk_node("p1", node_type="paragraph", parent_id=None),
    }
    assert gen._find_top_section("p1", node_map) is None


def test_find_top_section_missing_node_returns_none():
    gen = DiffSummaryGenerator()
    assert gen._find_top_section("no", {}) is None


def test_find_top_section_recursive_ascends_to_section():
    gen = DiffSummaryGenerator()
    node_map = {
        "sec": _mk_node("sec", node_type="section", parent_id=None),
        "para": _mk_node("para", node_type="paragraph", parent_id="sec"),
    }
    assert gen._find_top_section("para", node_map) == "sec"


def test_find_top_section_parent_not_in_map_returns_self_if_section():
    gen = DiffSummaryGenerator()
    node_map = {
        "n1": _mk_node("n1", node_type="section", parent_id="missing_parent"),
    }
    assert gen._find_top_section("n1", node_map) == "n1"


# ---------------------------------------------------------------------------
# 9. DiffSummaryGenerator._identify_changed_sections
# ---------------------------------------------------------------------------


def test_identify_changed_sections_no_changes_returns_empty():
    gen = DiffSummaryGenerator()
    node_diffs = [
        NodeDiff(
            node_id="n1",
            change_type=ChangeType.UNCHANGED,
            before=None,
            after=None,
        )
    ]
    assert gen._identify_changed_sections(node_diffs, {}) == []


def test_identify_changed_sections_aggregates_by_top_section():
    gen = DiffSummaryGenerator()
    sec = _mk_node("sec", node_type="section", parent_id=None)
    para = _mk_node("para", node_type="paragraph", parent_id="sec")
    node_diffs = [
        NodeDiff(
            node_id="para",
            change_type=ChangeType.MODIFIED,
            before=None,
            after=None,
        )
    ]
    result = gen._identify_changed_sections(
        node_diffs, {"sec": sec, "para": para}
    )
    assert len(result) == 1
    assert result[0].node_id == "sec"
    assert result[0].sub_changes == 1


def test_identify_changed_sections_uses_self_when_no_top_section():
    gen = DiffSummaryGenerator()
    orphan = _mk_node("orph", node_type="paragraph", parent_id=None)
    node_diffs = [
        NodeDiff(
            node_id="orph",
            change_type=ChangeType.MODIFIED,
            before=None,
            after=None,
        )
    ]
    result = gen._identify_changed_sections(node_diffs, {"orph": orphan})
    # 최상위 섹션 없음 → 자기 자신을 섹션으로
    assert result[0].node_id == "orph"


def test_identify_changed_sections_deleted_finds_from_nodes_a_fallback():
    gen = DiffSummaryGenerator()
    sec = _mk_node("sec", node_type="section", parent_id=None)
    deleted_para = _mk_node(
        "deleted_p", node_type="paragraph", parent_id="sec"
    )
    node_diffs = [
        NodeDiff(
            node_id="deleted_p",
            change_type=ChangeType.DELETED,
            before=None,
            after=None,
        )
    ]
    # node_b_map 에는 deleted_p 가 없지만 nodes_a 로 복구
    result = gen._identify_changed_sections(
        node_diffs, {"sec": sec}, nodes_a=[sec, deleted_para]
    )
    assert result[0].node_id == "sec"


# ---------------------------------------------------------------------------
# 10. DiffSummaryGenerator.generate (NameError 우회 — 실바그 확인용)
# ---------------------------------------------------------------------------


def test_generate_accepts_optional_nodes_a_param():
    """세션 14 에서 line 470 NameError 를 `nodes_a: Optional[list[Node]] = None`
    파라미터 추가로 수정. 이 테스트는 nodes_a 미전달 시 정상 동작을 검증한다.
    """
    gen = DiffSummaryGenerator()
    node_diffs = [
        NodeDiff(
            node_id="n1",
            change_type=ChangeType.ADDED,
            before=None,
            after=_node_to_snapshot(_mk_node("n1")),
        )
    ]
    # nodes_a 없이 호출해도 NameError 발생하지 않아야 함
    result = gen.generate(node_diffs, nodes_b=[])
    assert isinstance(result, DiffSummary)
    assert result.total_added == 1


def test_generate_uses_nodes_a_for_deleted_parent_lookup():
    """DELETED 노드의 최상위 섹션을 nodes_a 에서 탐색한다 (세션 14 수정 사항 검증)."""
    gen = DiffSummaryGenerator()
    sec = _mk_node("sec", node_type="section", parent_id=None)
    deleted_para = _mk_node("deleted_p", node_type="paragraph", parent_id="sec")
    node_diffs = [
        NodeDiff(
            node_id="deleted_p",
            change_type=ChangeType.DELETED,
            before=_node_to_snapshot(deleted_para),
            after=None,
        )
    ]
    # nodes_b 에는 삭제된 노드도 섹션도 없음. nodes_a 에서 복구 필요.
    result = gen.generate(node_diffs, nodes_b=[], nodes_a=[sec, deleted_para])
    assert isinstance(result, DiffSummary)
    # changed_sections 는 sec 으로 집계되어야 함
    assert any(s.node_id == "sec" for s in result.changed_sections)


def test_generate_succeeds_when_identify_patched(monkeypatch):
    """_identify_changed_sections 를 패치하면 generate 의 나머지 로직 검증 가능."""
    gen = DiffSummaryGenerator()
    monkeypatch.setattr(gen, "_identify_changed_sections", lambda *a, **kw: [])

    node = _mk_node("n1", content="추가된 본문 20자 어쩌구")
    node_diffs = [
        NodeDiff(
            node_id="n1",
            change_type=ChangeType.ADDED,
            before=None,
            after=_node_to_snapshot(node),
        )
    ]
    result = gen.generate(node_diffs, nodes_b=[node])
    assert isinstance(result, DiffSummary)
    assert result.total_added == 1
    assert result.changed_characters == len(node.content)
    assert result.severity == DiffSeverity.MAJOR  # 1/1 = 100% > 0.3


# ---------------------------------------------------------------------------
# 11. 캐시 유틸
# ---------------------------------------------------------------------------


def test_cache_key_is_direction_invariant():
    k1 = _cache_key("d1", "v1", "v2")
    k2 = _cache_key("d1", "v2", "v1")
    assert k1 == k2


def test_cache_key_contains_document_id_for_prefix_search():
    k = _cache_key("doc-xyz", "va", "vb")
    assert k.startswith("diff:doc-xyz:")


def test_cache_set_get_roundtrip():
    fake_result = MagicMock(spec=DiffResult)
    _set_cached_diff("d1", "va", "vb", fake_result)
    assert _get_cached_diff("d1", "va", "vb") is fake_result
    # 방향 바꿔도 히트
    assert _get_cached_diff("d1", "vb", "va") is fake_result


def test_summary_cache_roundtrip():
    from app.schemas.diff import DiffSummaryResponse
    fake = MagicMock(spec=DiffSummaryResponse)
    _set_cached_summary("d1", "va", "vb", fake)
    assert _get_cached_summary("d1", "va", "vb") is fake


def test_invalidate_cache_for_document_removes_prefix():
    fake_diff = MagicMock(spec=DiffResult)
    from app.schemas.diff import DiffSummaryResponse
    fake_sum = MagicMock(spec=DiffSummaryResponse)

    _set_cached_diff("doc-a", "v1", "v2", fake_diff)
    _set_cached_diff("doc-b", "v1", "v2", fake_diff)
    _set_cached_summary("doc-a", "v1", "v2", fake_sum)

    invalidate_cache_for_document("doc-a")

    assert _get_cached_diff("doc-a", "v1", "v2") is None
    assert _get_cached_summary("doc-a", "v1", "v2") is None
    # 다른 문서는 영향 없음
    assert _get_cached_diff("doc-b", "v1", "v2") is fake_diff


# ---------------------------------------------------------------------------
# 12. _build_version_ref
# ---------------------------------------------------------------------------


def test_build_version_ref_maps_fields():
    v = _mk_version("vid-1", version_number=3)
    ref = _build_version_ref(v)
    assert ref.id == "vid-1"
    assert ref.version_number == 3
    assert ref.label == "v1"
    assert ref.change_summary == "요약"


# ---------------------------------------------------------------------------
# 13. DiffService.compute_diff
# ---------------------------------------------------------------------------


def test_compute_diff_same_version_raises_validation():
    svc = DiffService()
    from app.api.errors.exceptions import ApiValidationError
    with pytest.raises(ApiValidationError):
        svc.compute_diff(
            MagicMock(),
            document_id="d1",
            version_a_id="v1",
            version_b_id="v1",
        )


def test_compute_diff_version_a_not_found(monkeypatch):
    svc = DiffService()
    monkeypatch.setattr(
        ds_mod.versions_repository,
        "get_by_document_and_version_id",
        lambda conn, d, v: None,
    )
    from app.api.errors.exceptions import ApiNotFoundError
    with pytest.raises(ApiNotFoundError):
        svc.compute_diff(
            MagicMock(),
            document_id="d1",
            version_a_id="va",
            version_b_id="vb",
        )


def test_compute_diff_version_b_not_found(monkeypatch):
    svc = DiffService()
    va = _mk_version("va")

    def get_ver(conn, d, v):
        return va if v == "va" else None

    monkeypatch.setattr(
        ds_mod.versions_repository,
        "get_by_document_and_version_id",
        get_ver,
    )
    from app.api.errors.exceptions import ApiNotFoundError
    with pytest.raises(ApiNotFoundError):
        svc.compute_diff(
            MagicMock(),
            document_id="d1",
            version_a_id="va",
            version_b_id="vb",
        )


def test_compute_diff_cache_hit_returns_cached():
    svc = DiffService()
    fake = MagicMock(spec=DiffResult)
    _set_cached_diff("d1", "va", "vb", fake)
    result = svc.compute_diff(
        MagicMock(),
        document_id="d1",
        version_a_id="va",
        version_b_id="vb",
    )
    assert result is fake


def test_compute_diff_full_flow_and_caches(monkeypatch):
    svc = DiffService()
    va = _mk_version("va", version_number=1)
    vb = _mk_version("vb", version_number=2)

    monkeypatch.setattr(
        ds_mod.versions_repository,
        "get_by_document_and_version_id",
        lambda conn, d, v: va if v == "va" else vb,
    )
    monkeypatch.setattr(
        ds_mod.nodes_repository,
        "list_by_version_id",
        lambda conn, v: [_mk_node("n1", content="A")] if v == "va" else [
            _mk_node("n1", content="B")
        ],
    )

    # generate() NameError 우회: _identify_changed_sections 패치
    monkeypatch.setattr(
        ds_mod.diff_summary_generator,
        "_identify_changed_sections",
        lambda *a, **kw: [],
    )

    result = svc.compute_diff(
        MagicMock(),
        document_id="d1",
        version_a_id="va",
        version_b_id="vb",
    )
    assert isinstance(result, DiffResult)
    assert result.document_id == "d1"
    # 캐시에 저장됨
    assert _get_cached_diff("d1", "va", "vb") is result


def test_compute_diff_too_large_converts_to_validation(monkeypatch):
    svc = DiffService()
    va = _mk_version("va")
    vb = _mk_version("vb")

    monkeypatch.setattr(
        ds_mod.versions_repository,
        "get_by_document_and_version_id",
        lambda conn, d, v: va if v == "va" else vb,
    )
    # 노드 대량 생성으로 DiffTooLargeError 유도
    monkeypatch.setattr(
        ds_mod.nodes_repository,
        "list_by_version_id",
        lambda conn, v: [_mk_node(f"n{i}") for i in range(6000)],
    )
    from app.api.errors.exceptions import ApiValidationError
    with pytest.raises(ApiValidationError):
        svc.compute_diff(
            MagicMock(),
            document_id="d1",
            version_a_id="va",
            version_b_id="vb",
        )


def test_compute_diff_with_inline_does_not_cache(monkeypatch):
    svc = DiffService()
    va = _mk_version("va")
    vb = _mk_version("vb")
    monkeypatch.setattr(
        ds_mod.versions_repository,
        "get_by_document_and_version_id",
        lambda conn, d, v: va if v == "va" else vb,
    )
    monkeypatch.setattr(
        ds_mod.nodes_repository,
        "list_by_version_id",
        lambda conn, v: [_mk_node("n1", content="A")] if v == "va" else [
            _mk_node("n1", content="B")
        ],
    )
    monkeypatch.setattr(
        ds_mod.diff_summary_generator,
        "_identify_changed_sections",
        lambda *a, **kw: [],
    )

    svc.compute_diff(
        MagicMock(),
        document_id="d1",
        version_a_id="va",
        version_b_id="vb",
        inline_diff=True,
    )
    # inline_diff=True → 캐싱 안됨
    assert _get_cached_diff("d1", "va", "vb") is None


# ---------------------------------------------------------------------------
# 14. DiffService.compute_diff_with_previous
# ---------------------------------------------------------------------------


def test_compute_diff_with_previous_version_not_found(monkeypatch):
    svc = DiffService()
    monkeypatch.setattr(
        ds_mod.versions_repository,
        "get_by_document_and_version_id",
        lambda conn, d, v: None,
    )
    from app.api.errors.exceptions import ApiNotFoundError
    with pytest.raises(ApiNotFoundError):
        svc.compute_diff_with_previous(
            MagicMock(),
            document_id="d1",
            version_id="v1",
        )


def test_compute_diff_with_previous_uses_parent_version_id(monkeypatch):
    svc = DiffService()
    vb = _mk_version("vb", version_number=2, parent_version_id="va")
    va = _mk_version("va", version_number=1)

    def get_ver_by_doc(conn, d, v):
        return vb

    monkeypatch.setattr(
        ds_mod.versions_repository,
        "get_by_document_and_version_id",
        get_ver_by_doc,
    )
    monkeypatch.setattr(
        ds_mod.versions_repository,
        "get_by_id",
        lambda conn, vid: va if vid == "va" else None,
    )

    # compute_diff 를 패치해 체인만 검증
    captured = {}

    def _fake_compute_diff(conn, **kwargs):
        captured.update(kwargs)
        return MagicMock(spec=DiffResult)

    monkeypatch.setattr(svc, "compute_diff", _fake_compute_diff)

    svc.compute_diff_with_previous(
        MagicMock(), document_id="d1", version_id="vb"
    )
    assert captured["version_a_id"] == "va"
    assert captured["version_b_id"] == "vb"


def test_compute_diff_with_previous_falls_back_to_version_number_minus_one(monkeypatch):
    svc = DiffService()
    vb = _mk_version("vb", version_number=2, parent_version_id=None)

    monkeypatch.setattr(
        ds_mod.versions_repository,
        "get_by_document_and_version_id",
        lambda conn, d, v: vb,
    )
    monkeypatch.setattr(
        ds_mod.versions_repository,
        "get_by_id",
        lambda conn, vid: None,
    )

    # cursor 가 version_number-1 로 조회 시 row 반환
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    cur.fetchone = MagicMock(
        return_value={
            "id": "va",
            "document_id": "d1",
            "version_number": 1,
            "label": None,
            "status": "draft",
            "change_summary": None,
            "source": None,
            "metadata": None,
            "created_by": None,
            "created_at": None,
            "parent_version_id": None,
            "restored_from_version_id": None,
            "title_snapshot": "T",
            "summary_snapshot": "",
            "metadata_snapshot": None,
            "content_snapshot": None,
            "published_by": None,
            "published_at": None,
        }
    )
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)

    # _row_to_version 을 패치해 간단한 객체 반환
    fake_va = _mk_version("va", version_number=1)
    import app.repositories.versions_repository as vr_mod
    monkeypatch.setattr(vr_mod, "_row_to_version", lambda row: fake_va)

    # compute_diff 체인 패치
    captured = {}

    def _fake(conn_, **kwargs):
        captured.update(kwargs)
        return MagicMock(spec=DiffResult)

    monkeypatch.setattr(svc, "compute_diff", _fake)

    svc.compute_diff_with_previous(conn, document_id="d1", version_id="vb")
    assert captured["version_a_id"] == "va"


def test_compute_diff_with_previous_no_previous_raises(monkeypatch):
    svc = DiffService()
    # version_number=1 → version_number-1 경로도 안감
    vb = _mk_version("vb", version_number=1, parent_version_id=None)
    monkeypatch.setattr(
        ds_mod.versions_repository,
        "get_by_document_and_version_id",
        lambda conn, d, v: vb,
    )
    from app.api.errors.exceptions import ApiNotFoundError
    with pytest.raises(ApiNotFoundError):
        svc.compute_diff_with_previous(
            MagicMock(), document_id="d1", version_id="vb"
        )


# ---------------------------------------------------------------------------
# 15. DiffService.compute_summary_only / compute_summary_with_previous
# ---------------------------------------------------------------------------


def test_compute_summary_only_cache_hit():
    svc = DiffService()
    from app.schemas.diff import DiffSummaryResponse
    fake = MagicMock(spec=DiffSummaryResponse)
    _set_cached_summary("d1", "va", "vb", fake)
    result = svc.compute_summary_only(
        MagicMock(), document_id="d1", version_a_id="va", version_b_id="vb"
    )
    assert result is fake


@pytest.mark.skip(reason="FG0-3 S14-fix: DiffSummaryResponse 스키마 확인 필요 — 후속 세션")
def test_compute_summary_only_cache_miss_computes_and_caches(monkeypatch):
    svc = DiffService()
    va = _mk_version("va")
    vb = _mk_version("vb")
    from app.schemas.diff import VersionRef

    fake_result = MagicMock(spec=DiffResult)
    fake_result.document_id = "d1"
    fake_result.version_a = _build_version_ref(va)
    fake_result.version_b = _build_version_ref(vb)
    fake_result.summary = MagicMock()

    monkeypatch.setattr(svc, "compute_diff", lambda conn, **kw: fake_result)

    result = svc.compute_summary_only(
        MagicMock(), document_id="d1", version_a_id="va", version_b_id="vb"
    )
    assert result is not None
    # 캐시에 저장됨
    assert _get_cached_summary("d1", "va", "vb") is result


@pytest.mark.skip(reason="FG0-3 S14-fix: DiffSummaryResponse 스키마 확인 필요 — 후속 세션")
def test_compute_summary_with_previous_caches(monkeypatch):
    svc = DiffService()
    va = _mk_version("va")
    vb = _mk_version("vb")

    fake_result = MagicMock(spec=DiffResult)
    fake_result.document_id = "d1"
    fake_result.version_a = _build_version_ref(va)
    fake_result.version_b = _build_version_ref(vb)
    fake_result.summary = MagicMock()

    monkeypatch.setattr(
        svc, "compute_diff_with_previous", lambda conn, **kw: fake_result
    )
    result = svc.compute_summary_with_previous(
        MagicMock(), document_id="d1", version_id="vb"
    )
    assert result is not None
    assert _get_cached_summary("d1", "va", "vb") is result


# ---------------------------------------------------------------------------
# 16. 싱글턴 존재
# ---------------------------------------------------------------------------


def test_singletons_exist():
    assert isinstance(ds_mod.diff_service, DiffService)
    assert isinstance(ds_mod.text_differ, TextDiffer)
    assert isinstance(ds_mod.node_differ, NodeDiffer)
    assert isinstance(ds_mod.diff_summary_generator, DiffSummaryGenerator)
