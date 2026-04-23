"""
P4-A 서버 측 diff 유틸 단위 테스트 (compute_fields_diff, _deep_equal)

대상: app.schemas.extraction 의 compute_fields_diff / _deep_equal

검증 범위:
- _deep_equal: 기본 타입/None/컨테이너의 재귀 비교, bool 과 int 의 엄격 구분,
  key 순서와 무관하게 dict 동치.
- compute_fields_diff:
    * added/removed/modified 기본 분류
    * 정렬 순서 (added/removed 는 키 기준 sorted)
    * unchanged_count 정확성
    * 양쪽 모두 없는 속성은 변경으로 기록되지 않음
    * None/빈 fields 에 대한 방어적 동작
"""
from __future__ import annotations

import pytest

from app.schemas.extraction import (
    ExtractionSchemaDiffResponse,
    ModifiedFieldDiff,
    PropertyDiff,
    compute_fields_diff,
)
from app.schemas.extraction import _deep_equal  # noqa: WPS450 (테스트 목적 임포트)


# ---------------------------------------------------------------------------
# _deep_equal
# ---------------------------------------------------------------------------


class TestDeepEqual:
    def test_primitives_equal(self):
        assert _deep_equal(1, 1) is True
        assert _deep_equal("a", "a") is True
        assert _deep_equal(None, None) is True

    def test_primitives_not_equal(self):
        assert _deep_equal(1, 2) is False
        assert _deep_equal("a", "b") is False

    def test_bool_and_int_are_distinguished(self):
        # Python 에서 True == 1 이지만, 서버 정본 diff 는 타입 구분이 필요하다.
        assert _deep_equal(True, 1) is False
        assert _deep_equal(False, 0) is False
        assert _deep_equal(True, True) is True

    def test_dict_key_order_invariant(self):
        assert _deep_equal({"a": 1, "b": 2}, {"b": 2, "a": 1}) is True

    def test_dict_different_keys(self):
        assert _deep_equal({"a": 1}, {"a": 1, "b": 2}) is False
        assert _deep_equal({"a": 1, "b": 2}, {"a": 1}) is False

    def test_nested_dict_equal(self):
        assert (
            _deep_equal({"x": {"y": [1, 2, 3]}}, {"x": {"y": [1, 2, 3]}}) is True
        )

    def test_nested_dict_value_differs(self):
        assert (
            _deep_equal({"x": {"y": [1, 2, 3]}}, {"x": {"y": [1, 2, 4]}}) is False
        )

    def test_list_order_matters(self):
        assert _deep_equal([1, 2, 3], [3, 2, 1]) is False

    def test_type_mismatch_between_container_and_primitive(self):
        assert _deep_equal({"a": 1}, [("a", 1)]) is False


# ---------------------------------------------------------------------------
# compute_fields_diff
# ---------------------------------------------------------------------------


def _field(name: str, *, ftype: str = "string", desc: str = "desc", **extra) -> dict:
    base = {
        "field_name": name,
        "field_type": ftype,
        "required": True,
        "description": desc,
    }
    base.update(extra)
    return base


class TestComputeFieldsDiff:
    def test_empty_both(self):
        out = compute_fields_diff(
            {}, {},
            doc_type_code="contract", base_version=1, target_version=2,
        )
        assert isinstance(out, ExtractionSchemaDiffResponse)
        assert out.added == []
        assert out.removed == []
        assert out.modified == []
        assert out.unchanged_count == 0
        assert out.doc_type_code == "contract"
        assert out.base_version == 1
        assert out.target_version == 2

    def test_all_unchanged(self):
        base = {"a": _field("a"), "b": _field("b")}
        out = compute_fields_diff(
            base, base,
            doc_type_code="x", base_version=1, target_version=2,
        )
        assert out.added == []
        assert out.removed == []
        assert out.modified == []
        assert out.unchanged_count == 2

    def test_added_only(self):
        out = compute_fields_diff(
            {"a": _field("a")},
            {"a": _field("a"), "b": _field("b")},
            doc_type_code="x", base_version=1, target_version=2,
        )
        assert out.added == ["b"]
        assert out.removed == []
        assert out.modified == []
        assert out.unchanged_count == 1

    def test_removed_only(self):
        out = compute_fields_diff(
            {"a": _field("a"), "b": _field("b")},
            {"a": _field("a")},
            doc_type_code="x", base_version=2, target_version=1,
        )
        assert out.added == []
        assert out.removed == ["b"]
        assert out.unchanged_count == 1

    def test_modified_only(self):
        before = _field("a", desc="old")
        after = _field("a", desc="new")
        out = compute_fields_diff(
            {"a": before}, {"a": after},
            doc_type_code="x", base_version=1, target_version=2,
        )
        assert out.added == []
        assert out.removed == []
        assert out.unchanged_count == 0
        assert len(out.modified) == 1
        m = out.modified[0]
        assert isinstance(m, ModifiedFieldDiff)
        assert m.name == "a"
        # description 속성만 변경됨
        changed_keys = [ch.key for ch in m.changes]
        assert "description" in changed_keys
        desc_change = next(ch for ch in m.changes if ch.key == "description")
        assert desc_change.before == "old"
        assert desc_change.after == "new"

    def test_added_removed_modified_sort_order(self):
        base = {
            "z": _field("z"),
            "m": _field("m", desc="old"),
            "a": _field("a"),
        }
        target = {
            "a": _field("a"),
            "m": _field("m", desc="new"),
            "b": _field("b"),
        }
        out = compute_fields_diff(
            base, target,
            doc_type_code="x", base_version=1, target_version=2,
        )
        assert out.added == ["b"]
        assert out.removed == ["z"]
        # modified 도 name 기준 정렬
        assert [m.name for m in out.modified] == ["m"]
        assert out.unchanged_count == 1  # "a"

    def test_none_field_value_treated_as_empty(self):
        # repository 에 있는 방어 코드 (b = base[k] or {}) 를 쿼리 수준에서 재확인.
        # 단 compute_fields_diff 는 `b = base[k] or {}` 를 사용하므로 None 은 {} 로 간주.
        out = compute_fields_diff(
            {"a": None},
            {"a": {"field_name": "a"}},
            doc_type_code="x", base_version=1, target_version=2,
        )
        # 양쪽이 {} vs {field_name} 이므로 modified 에 들어감
        assert out.added == []
        assert out.removed == []
        assert out.unchanged_count == 0
        assert len(out.modified) == 1

    def test_property_diff_covers_both_sides_only_present(self):
        before = {"x": 1}
        after = {"y": 2}
        out = compute_fields_diff(
            {"f": before}, {"f": after},
            doc_type_code="x", base_version=1, target_version=2,
        )
        assert len(out.modified) == 1
        changes = out.modified[0].changes
        keys = {c.key for c in changes}
        assert keys == {"x", "y"}
        x_ch = next(c for c in changes if c.key == "x")
        y_ch = next(c for c in changes if c.key == "y")
        assert x_ch.before == 1 and x_ch.after is None
        assert y_ch.before is None and y_ch.after == 2

    def test_bool_int_property_distinguished(self):
        # required 에서 True vs 1 은 서버가 다른 값으로 본다.
        before = {"required": True}
        after = {"required": 1}
        out = compute_fields_diff(
            {"f": before}, {"f": after},
            doc_type_code="x", base_version=1, target_version=2,
        )
        assert len(out.modified) == 1
        ch = out.modified[0].changes
        assert len(ch) == 1
        assert ch[0].key == "required"
        assert ch[0].before is True
        assert ch[0].after == 1

    def test_unchanged_count_does_not_include_modified(self):
        base = {
            "keep1": _field("keep1"),
            "keep2": _field("keep2"),
            "change": _field("change", desc="old"),
        }
        target = {
            "keep1": _field("keep1"),
            "keep2": _field("keep2"),
            "change": _field("change", desc="new"),
        }
        out = compute_fields_diff(
            base, target,
            doc_type_code="x", base_version=1, target_version=2,
        )
        assert out.unchanged_count == 2
        assert len(out.modified) == 1


# ---------------------------------------------------------------------------
# PropertyDiff / ModifiedFieldDiff Pydantic DTO 의 직렬화
# ---------------------------------------------------------------------------


class TestDiffResponseDtoSerialization:
    def test_model_dump_round_trip(self):
        out = compute_fields_diff(
            {"a": _field("a", desc="old")},
            {"a": _field("a", desc="new")},
            doc_type_code="contract", base_version=3, target_version=5,
        )
        payload = out.model_dump()
        assert payload["doc_type_code"] == "contract"
        assert payload["base_version"] == 3
        assert payload["target_version"] == 5
        assert payload["added"] == []
        assert payload["removed"] == []
        assert payload["unchanged_count"] == 0
        assert isinstance(payload["modified"], list)
        assert payload["modified"][0]["name"] == "a"
        changes = payload["modified"][0]["changes"]
        assert any(c["key"] == "description" for c in changes)
