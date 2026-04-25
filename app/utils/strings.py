"""문자열 정규화 유틸.

본 모듈은 ``docs/함수도서관/backend.md`` §1.4 에 등록된 공통 유틸이다.

제공 함수:
    - :func:`normalize_display_name` — 사용자 표시용 이름(폴더/컬렉션 등)을 정규화한다 (B6, 2026-04-25).
    - :func:`normalize_lower` — ``str | None`` 을 ``strip().lower()`` 로 정규화 (BE-G1, 2026-04-25).

도입 배경:
    - ``app.services.folders_service._normalize_name`` 과
      ``app.services.collections_service._normalize_name`` 이 구조상 거의 동일했다
      (앞뒤 trim + 연속 공백 1개로 압축 + 길이 검사 + 선택적 ``/`` 금지).
    - CONSTITUTION 제8조(Single Responsibility), 제10조(Docstring as Agent Contract),
      제14조(Shared Error Contract) 준수 목적.

보안·도메인 메모:
    - 본 유틸은 외부 I/O·DB·로깅을 수행하지 않는다(순수 함수).
    - 대소문자는 **보존**한다. UNIQUE 제약은 DB 레이어(COLLATE 등)에서 다룬다.
    - 검증 실패 시 반드시 :class:`ApiValidationError` 를 던진다. 상위 라우터는
      이를 400/422 로 맵핑한다 (CONSTITUTION 제14조).
"""
from __future__ import annotations

from app.api.errors.exceptions import ApiValidationError

__all__ = ["normalize_display_name", "normalize_lower"]


def normalize_display_name(
    raw: str | None,
    min_len: int,
    max_len: int,
    *,
    forbid_slash: bool = False,
    label: str = "이름",
) -> str:
    """사용자 표시용 이름을 정규화하고 검증한다.

    수행 순서:
        1. ``raw is None`` 이면 ``ApiValidationError("{label}은 필수입니다")``.
        2. ``" ".join(raw.split())`` 로 앞뒤 공백 제거 + 연속 공백 1개로 압축.
        3. 길이가 ``[min_len, max_len]`` 범위를 벗어나면
           ``ApiValidationError("{label}은 {min}~{max}자 사이여야 합니다")``.
        4. ``forbid_slash`` 가 참이고 정규화 결과에 ``"/"`` 가 포함되면
           ``ApiValidationError("{label}에 '/' 는 포함할 수 없습니다")``.
        5. 정규화된 문자열을 그대로 반환한다.

    Args:
        raw: 원본 입력. ``None`` 이면 필수 에러로 분기.
        min_len: 정규화 후 최소 길이(포함). 1 이상 권장.
        max_len: 정규화 후 최대 길이(포함). ``min_len`` 이상이어야 한다.
        forbid_slash: ``True`` 이면 정규화 결과에 ``"/"`` 를 금지한다.
            materialized path 를 쓰는 폴더 이름 등에서 사용.
        label: 에러 메시지의 접두 라벨. 예: ``"폴더 이름"``, ``"컬렉션 이름"``.

    Returns:
        str: 앞뒤 공백 제거 + 연속 공백이 압축된 문자열. 대소문자는 보존.

    Raises:
        ApiValidationError: 위 1·3·4 조건에 해당할 때.

    Examples:
        >>> normalize_display_name("  hello   world  ", 1, 200, label="이름")
        'hello world'

        >>> normalize_display_name("a/b", 1, 200, forbid_slash=True,
        ...                        label="폴더 이름")  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        ApiValidationError: 폴더 이름에 '/' 는 포함할 수 없습니다

    Notes:
        - 대소문자 정규화가 필요하면 별도 유틸(예: ``normalize_lower``)을 조합한다.
        - 본 유틸은 유니코드 NFKC 정규화를 수행하지 않는다.
        - ``label`` 의 한국어 조사(은/는)는 호출자가 "이름"·"폴더 이름"·"컬렉션 이름"
          등 받침으로 끝나는 라벨을 넘긴다는 전제로 ``"은"`` 을 고정한다.
    """
    if raw is None:
        raise ApiValidationError(f"{label}은 필수입니다")

    name = " ".join(raw.split())

    if len(name) < min_len or len(name) > max_len:
        raise ApiValidationError(
            f"{label}은 {min_len}~{max_len}자 사이여야 합니다",
        )

    if forbid_slash and "/" in name:
        raise ApiValidationError(f"{label}에 '/' 는 포함할 수 없습니다")

    return name


def normalize_lower(raw: str | None) -> str | None:
    """``str | None`` 을 ``strip().lower()`` 로 정규화한다 (None 패스스루).

    동치 표현:
        ``raw.strip().lower() if raw is not None else None``

    수행 순서는 **strip → lower** (앞뒤 공백 먼저 제거 후 소문자화). 기존 호출지에는
    ``raw.lower().strip()`` 과 ``raw.strip().lower()`` 가 혼재했으나 두 결과는
    *대부분의 경우* 동일하다. 다른 결과가 나오는 엣지 케이스 (예: 대문자가 trim 후 길이를
    바꾸는 합자 같은 유니코드) 는 매우 드물고 본 유틸 도입을 막을 정도는 아니다.

    Args:
        raw: 원본 문자열 또는 ``None``. 빈 문자열 ``""`` 은 그대로 ``""`` 반환.

    Returns:
        str | None: ``strip().lower()`` 적용 결과, 또는 ``None`` (입력이 ``None`` 일 때).

    Examples:
        >>> normalize_lower("  Hello World  ")
        'hello world'
        >>> normalize_lower("ABC")
        'abc'
        >>> normalize_lower("") == ""
        True
        >>> normalize_lower(None) is None
        True

    Notes:
        - 유니코드 NFKC/NFC 정규화는 본 유틸이 수행하지 않는다 (별 라운드).
        - 이메일 정규화 같은 도메인 의미 정규화는 호출자가 추가 책임을 진다 (예:
          IDNA 변환, plus-addressing 처리는 별 유틸).
    """
    if raw is None:
        return None
    return raw.strip().lower()
