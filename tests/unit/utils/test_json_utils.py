"""Unit tests for :mod:`app.utils.json_utils`.

Covers:
    - ``loads_maybe``: str → loads / dict 그대로 / list 그대로 / None 그대로 /
      잘못된 JSON → JSONDecodeError 전파.
    - ``dumps_ko``: 한글 보존 (no escape) / dict / list / 숫자 / 옵션 통과
      (separators / sort_keys / indent) / ensure_ascii 중복 키 시 TypeError.
Docs: ``docs/함수도서관/backend.md`` §1.3 BE-G2.
"""
from __future__ import annotations

import json

import pytest

from app.api.errors.exceptions import ApiValidationError
from app.utils.json_utils import dumps_ko, loads_maybe, loads_strict


# ---------------------------------------------------------------------------
# loads_maybe
# ---------------------------------------------------------------------------


class TestLoadsMaybe:
    def test_str_to_dict(self) -> None:
        assert loads_maybe('{"a": 1}') == {"a": 1}

    def test_str_to_list(self) -> None:
        assert loads_maybe("[1, 2, 3]") == [1, 2, 3]

    def test_str_with_korean(self) -> None:
        assert loads_maybe('{"name": "한글"}') == {"name": "한글"}

    def test_dict_passthrough(self) -> None:
        d = {"a": 1, "b": [2, 3]}
        result = loads_maybe(d)
        # 동일 객체 (복사 안 함)
        assert result is d

    def test_list_passthrough(self) -> None:
        lst = [1, 2, {"a": 1}]
        assert loads_maybe(lst) is lst

    def test_none_passthrough(self) -> None:
        assert loads_maybe(None) is None

    def test_int_passthrough(self) -> None:
        # 사양은 str|dict|list|None 이지만 호출자 안전성 위해 임의 타입 그대로
        assert loads_maybe(42) == 42

    def test_invalid_json_raises_json_decode_error(self) -> None:
        # 호출자 try/except json.JSONDecodeError 흐름 보존
        with pytest.raises(json.JSONDecodeError):
            loads_maybe("not-a-json")

    def test_empty_string_raises(self) -> None:
        # json.loads("") → JSONDecodeError
        with pytest.raises(json.JSONDecodeError):
            loads_maybe("")


# ---------------------------------------------------------------------------
# dumps_ko
# ---------------------------------------------------------------------------


class TestDumpsKo:
    def test_korean_not_escaped(self) -> None:
        result = dumps_ko({"k": "한글"})
        assert "한글" in result  # \uXXXX escape 가 없어야 함
        assert "\\u" not in result

    def test_dict_basic(self) -> None:
        # 키 순서는 Python 3.7+ insertion order
        assert dumps_ko({"a": 1, "b": 2}) == '{"a": 1, "b": 2}'

    def test_list_basic(self) -> None:
        assert dumps_ko([1, 2, 3]) == "[1, 2, 3]"

    def test_nested(self) -> None:
        result = dumps_ko({"items": ["가", "나"], "count": 2})
        # ASCII 비교 대신 round-trip
        assert json.loads(result) == {"items": ["가", "나"], "count": 2}

    def test_none(self) -> None:
        assert dumps_ko(None) == "null"

    def test_bool(self) -> None:
        assert dumps_ko(True) == "true"
        assert dumps_ko(False) == "false"

    def test_separators_passthrough(self) -> None:
        # 압축 출력
        assert dumps_ko({"a": 1, "b": 2}, separators=(",", ":")) == '{"a":1,"b":2}'

    def test_sort_keys_passthrough(self) -> None:
        assert dumps_ko({"b": 2, "a": 1}, sort_keys=True) == '{"a": 1, "b": 2}'

    def test_indent_passthrough(self) -> None:
        assert dumps_ko({"a": 1}, indent=2) == '{\n  "a": 1\n}'

    def test_default_passthrough(self) -> None:
        # 비-직렬화 객체에 default 콜백 적용
        from datetime import date
        result = dumps_ko({"d": date(2026, 4, 25)}, default=str)
        assert "2026-04-25" in result

    def test_ensure_ascii_double_specified_raises(self) -> None:
        # 호출자가 ensure_ascii 를 다시 주면 TypeError (보호)
        with pytest.raises(TypeError):
            dumps_ko({"k": "v"}, ensure_ascii=True)  # type: ignore[misc]

    def test_round_trip_with_loads_maybe(self) -> None:
        original = {"한글": "값", "list": [1, 2, "셋"]}
        serialized = dumps_ko(original)
        round_tripped = loads_maybe(serialized)
        assert round_tripped == original


# ---------------------------------------------------------------------------
# loads_strict (R1, 2026-04-25)
# ---------------------------------------------------------------------------


class TestLoadsStrict:
    def test_valid_json(self) -> None:
        assert loads_strict('{"a": 1}') == {"a": 1}

    def test_dict_passthrough(self) -> None:
        d = {"a": 1}
        assert loads_strict(d) is d

    def test_invalid_json_raises_api_validation_error(self) -> None:
        with pytest.raises(ApiValidationError) as exc_info:
            loads_strict("not-json")
        assert "올바른 JSON 형식이 아닙니다" in exc_info.value.message

    def test_label_in_message_and_details(self) -> None:
        with pytest.raises(ApiValidationError) as exc_info:
            loads_strict("{bad", label="config")
        err = exc_info.value
        assert "config" in err.message
        assert err.details and err.details[0]["field"] == "config"
        assert err.details[0]["code"] == "INVALID_JSON"

    def test_keyword_only_label(self) -> None:
        with pytest.raises(TypeError):
            loads_strict("{}", "config")  # type: ignore[misc]

    def test_chained_exception_preserves_root(self) -> None:
        with pytest.raises(ApiValidationError) as exc_info:
            loads_strict("not-json")
        assert exc_info.value.__cause__ is not None
