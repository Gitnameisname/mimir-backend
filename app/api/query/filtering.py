"""
Filter parameter parser / validator.

이번 단계 범위:
  - equality 기반 filter만 지원
  - 허용 필드 (FilterFieldSpec)만 수용
  - 기본 타입 검증 (str / int / bool / uuid)
  - 허용 값 목록 검증 (allowed_values가 있을 경우)

미구현 (이후 Phase):
  - range 조건 (field__gte, field__lt 등)
  - or / and / nested 복합 조건
  - full-text search

입력 예:
  ?status=draft&document_type=policy

처리 결과 예:
  {"status": "draft", "document_type": "policy"}
"""

import re
from typing import Any

from app.api.errors.exceptions import ApiValidationError
from app.api.query.models import FilterFieldSpec, ListQuerySpec

# pagination / sort 전용 파라미터명 — filter 파싱 시 건너뜀
_RESERVED_PARAMS = frozenset({"page", "page_size", "cursor", "limit", "sort"})

# UUID v4 패턴
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def parse_filters(
    query_params: dict[str, str],
    spec: ListQuerySpec,
) -> dict[str, Any]:
    """query params에서 허용된 filter 필드만 추출하고 검증한다.

    Args:
        query_params : Request.query_params 딕셔너리 (raw string 값)
        spec         : resource별 ListQuerySpec

    Returns:
        {field_name: parsed_value} 딕셔너리

    Raises:
        ApiValidationError: 허용되지 않은 filter 필드 또는 invalid 값
    """
    if not spec.allowed_filter_fields:
        return {}

    allowed_map: dict[str, FilterFieldSpec] = {f.name: f for f in spec.allowed_filter_fields}
    errors = []
    filters: dict[str, Any] = {}

    for param_name, raw_value in query_params.items():
        if param_name in _RESERVED_PARAMS:
            continue

        if param_name not in allowed_map:
            # 알 수 없는 filter 필드 — 조용히 무시하지 않고 오류 반환
            errors.append(
                {
                    "field": param_name,
                    "reason": f"filter by '{param_name}' is not supported; "
                    f"allowed: {list(allowed_map.keys())}",
                    "category": "invalid_filter",
                }
            )
            continue

        field_spec = allowed_map[param_name]
        parsed, err = _coerce_value(param_name, raw_value, field_spec)
        if err:
            errors.append(err)
        else:
            filters[param_name] = parsed

    if errors:
        raise ApiValidationError("Invalid filter parameter(s)", details=errors)

    return filters


def _coerce_value(
    name: str,
    raw: str,
    spec: FilterFieldSpec,
) -> tuple[Any, dict | None]:
    """raw string 값을 FilterFieldSpec 타입으로 변환하고 검증한다.

    Returns:
        (parsed_value, error_dict | None)
    """
    # allowed_values 검사
    if spec.allowed_values is not None and raw not in spec.allowed_values:
        return None, {
            "field": name,
            "reason": f"invalid value '{raw}'; allowed: {spec.allowed_values}",
            "category": "invalid_filter",
        }

    match spec.type:
        case "int":
            try:
                return int(raw), None
            except ValueError:
                return None, {
                    "field": name,
                    "reason": f"expected integer, got '{raw}'",
                    "category": "invalid_filter",
                }

        case "bool":
            lower = raw.lower()
            if lower in ("true", "1", "yes"):
                return True, None
            if lower in ("false", "0", "no"):
                return False, None
            return None, {
                "field": name,
                "reason": f"expected boolean (true/false), got '{raw}'",
                "category": "invalid_filter",
            }

        case "uuid":
            if not _UUID_PATTERN.match(raw):
                return None, {
                    "field": name,
                    "reason": f"expected UUID v4, got '{raw}'",
                    "category": "invalid_filter",
                }
            return raw, None

        case _:  # "str" (default)
            return raw, None
