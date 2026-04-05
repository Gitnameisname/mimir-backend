"""
Sort parameter parser / validator.

규약:
  - 입력: sort=field1,-field2  (comma-separated)
  - prefix 없음 = asc, prefix '-' = desc
  - 허용되지 않은 필드 → ApiValidationError
  - malformed 입력 → ApiValidationError

예:
  sort=created_at          → [SortOrder(field="created_at", direction="asc")]
  sort=-updated_at         → [SortOrder(field="updated_at", direction="desc")]
  sort=title,-created_at   → [SortOrder("title","asc"), SortOrder("created_at","desc")]

금지 혼용:
  sortBy, order, descending=true 등의 다른 방식 사용 금지.
  이번 Task부터 sort 규약은 위 방식으로 고정.
"""

from app.api.errors.exceptions import ApiValidationError
from app.api.query.models import ListQuerySpec, SortOrder

_MAX_SORT_FIELDS = 5  # 다중 sort 최대 허용 개수 (과도한 input 방지)


def parse_sort(sort_str: str, spec: ListQuerySpec) -> list[SortOrder]:
    """sort 문자열을 파싱하고 허용 필드를 검증한다.

    Args:
        sort_str : 쉼표로 구분된 sort 표현 (예: "created_at,-updated_at")
        spec     : resource별 ListQuerySpec

    Returns:
        list[SortOrder]

    Raises:
        ApiValidationError: malformed 또는 unsupported sort 입력
    """
    if not sort_str or not sort_str.strip():
        return []

    raw_fields = [f.strip() for f in sort_str.split(",") if f.strip()]

    if not raw_fields:
        return []

    if not spec.allow_multi_sort and len(raw_fields) > 1:
        raise ApiValidationError(
            "Multiple sort fields are not allowed for this resource",
            details=[
                {
                    "field": "sort",
                    "reason": "only a single sort field is allowed",
                    "category": "unsupported_sort",
                }
            ],
        )

    if len(raw_fields) > _MAX_SORT_FIELDS:
        raise ApiValidationError(
            f"Too many sort fields (max {_MAX_SORT_FIELDS})",
            details=[
                {
                    "field": "sort",
                    "reason": f"at most {_MAX_SORT_FIELDS} sort fields are allowed",
                    "category": "unsupported_sort",
                }
            ],
        )

    errors = []
    orders: list[SortOrder] = []

    for raw in raw_fields:
        if not raw:
            continue

        if raw.startswith("-"):
            field_name = raw[1:]
            direction = "desc"
        else:
            field_name = raw
            direction = "asc"

        if not field_name:
            errors.append(
                {
                    "field": "sort",
                    "reason": f"malformed sort token: '{raw}'",
                    "category": "unsupported_sort",
                }
            )
            continue

        if not _is_valid_field_name(field_name):
            errors.append(
                {
                    "field": "sort",
                    "reason": f"invalid sort field name: '{field_name}'",
                    "category": "unsupported_sort",
                }
            )
            continue

        if spec.allowed_sort_fields and field_name not in spec.allowed_sort_fields:
            errors.append(
                {
                    "field": "sort",
                    "reason": f"sort by '{field_name}' is not supported; "
                    f"allowed: {spec.allowed_sort_fields}",
                    "category": "unsupported_sort",
                }
            )
            continue

        orders.append(SortOrder(field=field_name, direction=direction))

    if errors:
        raise ApiValidationError("Invalid sort parameter", details=errors)

    return orders


def _is_valid_field_name(name: str) -> bool:
    """field name이 alphanumeric + underscore 형식인지 검사."""
    return bool(name) and all(c.isalnum() or c == "_" for c in name)
