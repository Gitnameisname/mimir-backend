"""HTTP 예외 헬퍼 — 4xx 보일러플레이트 통합.

본 모듈은 ``docs/함수도서관/backend.md`` §1.5 BE-G3 에 등록된 공통 유틸이다.

제공 함수 (4종 세트):
    - :func:`not_found` — 404 Not Found
    - :func:`bad_request` — 400 Bad Request
    - :func:`unprocessable_entity` — 422 Unprocessable Entity
    - :func:`conflict` — 409 Conflict

도입 배경:
    - ``raise HTTPException(status_code=404, detail="...")`` 패턴이 라우터 14 파일에
      183건 분산되어 있었다 (404=111회 / 422=51회 / 400=25회 / 409 일부).
    - 호출 형태의 99%가 `(status_code, detail)` 두 인자만 사용하는 단순 케이스.
    - CONSTITUTION 제8조(Single Responsibility), 제10조(Docstring as Agent Contract),
      제14조(Shared Error Contract) 준수.

호환 정책 (2026-04-25 G-BE-G3 도입 시점):
    - ``detail`` 은 **문자열을 그대로 전달**한다. 프런트의
      ``getApiErrorMessage()`` 가 detail 을 문자열로 가정하므로 wire format 호환을
      유지하기 위함. dict detail 로의 본격 이행은 별도 라운드에서 결정한다.
    - ``error_code`` 가 주어지면 응답 헤더 ``X-Error-Code: <value>`` 로 노출한다.
      이렇게 하면 detail wire format 호환을 깨지 않으면서 CONSTITUTION 제14조
      Shared Error Contract 의 ``error_code`` 필드를 점진적으로 도입할 수 있다.
    - 호출자가 ``headers`` 를 추가로 전달하면 두 dict 가 머지된다.
      충돌 키는 호출자 ``headers`` 가 우선 (호출자가 명시적으로 지정한 헤더 보존).

비대상 (intentional non-goals):
    - 401 Unauthorized, 403 Forbidden 등은 본 그룹 사양 외이다 (별 그룹).
    - Shared Error Contract 의 나머지 필드(``recoverable`` / ``safe_retry`` /
      ``required_scope`` / ``suggested_next_action``) 의 wire 노출 방식은 후속 결정.
    - 실제 ``raise`` 는 호출자 책임. 본 함수들은 예외 인스턴스를 만들 뿐이다.

보안 메모:
    - ``detail`` / ``error_code`` 는 외부 응답으로 그대로 노출되므로 호출자는
      내부 SQL·스택트레이스·비밀값을 포함하지 말 것 (CLAUDE.md §4.3).
    - ``X-Error-Code`` 헤더 값에 개행/CR 이 들어가면 헤더 인젝션 위험. 호출자가
      domain enum 으로 좁혀 쓰는 것을 권장 (예: ``"NOT_FOUND_DOCUMENT"``).
"""
from __future__ import annotations

from fastapi import HTTPException

__all__ = [
    "not_found",
    "bad_request",
    "unprocessable_entity",
    "conflict",
    # B-N4 (2026-04-25): ApiNotFoundError 변형 — handlers 가 404 로 변환
    "not_found_resource",
]

# 헤더 키 — Shared Error Contract 의 error_code 를 wire 에 노출하는 임시 통로.
# Phase 별 본격 dict-detail 도입까지 호환 레이어 역할.
_ERROR_CODE_HEADER = "X-Error-Code"


def _build_headers(
    error_code: str | None,
    user_headers: dict[str, str] | None,
) -> dict[str, str] | None:
    """``error_code`` 와 ``user_headers`` 를 머지해 최종 headers 를 반환한다.

    - 둘 다 None 이면 None 반환 (FastAPI 가 헤더 dict 자체를 생략).
    - ``error_code`` 는 ``X-Error-Code`` 키로 변환.
    - 같은 키가 양쪽에 있으면 ``user_headers`` 가 우선 (호출자 의도 보존).
    """
    if not error_code and not user_headers:
        return None
    merged: dict[str, str] = {}
    if error_code:
        merged[_ERROR_CODE_HEADER] = error_code
    if user_headers:
        # user_headers 가 X-Error-Code 를 명시했다면 그것이 이긴다.
        merged.update(user_headers)
    return merged


def not_found(
    detail: str,
    *,
    error_code: str | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """404 Not Found ``HTTPException`` 을 만든다.

    :param detail: 사용자 노출용 메시지 (한국어 권장). 문자열 그대로 wire 에 실림.
    :param error_code: 선택. ``X-Error-Code`` 헤더로 노출 (예: ``"NOT_FOUND_DOCUMENT"``).
    :param headers: 선택. 추가 응답 헤더. 같은 키 충돌 시 호출자 값이 우선.

    >>> exc = not_found("문서를 찾을 수 없습니다.")
    >>> exc.status_code
    404
    >>> exc.detail
    '문서를 찾을 수 없습니다.'
    """
    return HTTPException(
        status_code=404,
        detail=detail,
        headers=_build_headers(error_code, headers),
    )


def bad_request(
    detail: str,
    *,
    error_code: str | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """400 Bad Request ``HTTPException`` 을 만든다."""
    return HTTPException(
        status_code=400,
        detail=detail,
        headers=_build_headers(error_code, headers),
    )


def unprocessable_entity(
    detail: str,
    *,
    error_code: str | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """422 Unprocessable Entity ``HTTPException`` 을 만든다.

    검증을 통과했으나 도메인 의미 위반 (FK 미존재, 상태 머신 위반 등) 에 사용.
    """
    return HTTPException(
        status_code=422,
        detail=detail,
        headers=_build_headers(error_code, headers),
    )


def conflict(
    detail: str,
    *,
    error_code: str | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """409 Conflict ``HTTPException`` 을 만든다.

    중복 생성, 동시 수정 충돌, 상태 race 등에 사용.
    """
    return HTTPException(
        status_code=409,
        detail=detail,
        headers=_build_headers(error_code, headers),
    )


# ===========================================================================
# B-N4 (2026-04-25) — 도메인 not_found (ApiNotFoundError 변형)
# ===========================================================================
#
# 위 4종 (not_found / bad_request / ...) 은 FastAPI ``HTTPException`` 인스턴스를
# 만든다. handlers.py 가 직접 처리.
#
# 본 helper 는 다른 계층 — :class:`ApiNotFoundError` (플랫폼 비즈니스 예외) 를
# 만든다. handlers.py 의 :func:`api_error_handler` 가 ApiError 계층을 응답으로
# 변환할 때 ``http_status=404`` + ``error_code="resource_not_found"`` 를 사용.
#
# 시맨틱 차이:
#   - ``not_found(detail)``           → ``HTTPException(404, detail=str)`` (단순)
#   - ``not_found_resource(label, id)`` → ``ApiNotFoundError`` (구조화 + 한국어 표준 메시지)
#
# 라우터에서 영문 ``f"X '{id}' not found"`` 패턴 24 사이트를 통일.

# ApiNotFoundError 를 직접 import (지연 import 로 순환 회피).
def not_found_resource(
    label_ko: str,
    resource_id: str,
    *,
    error_code: str | None = None,
):
    """도메인 리소스 not_found 표준 :class:`ApiNotFoundError` 인스턴스를 만든다.

    24 사이트 영문 패턴 (`f"Document '{id}' not found"`) 의 한국어 통일 버전.

    :param label_ko: 한국어 라벨 (예: ``"문서"``, ``"버전"``, ``"폴더"``, ``"컬렉션"``).
        호출자가 명시 — 도메인 사전 매핑은 의도적으로 하지 않음 (helper 가 도메인을 모름).
    :param resource_id: 리소스 식별자 (UUID 문자열 등). 메시지에 그대로 포함됨.
    :param error_code: 선택. 백엔드 ``error.code`` 필드. 미지정 시 ApiNotFoundError 의
        클래스 default ``"resource_not_found"`` 사용.

    :returns: :class:`ApiNotFoundError` 인스턴스 — 호출자가 ``raise`` 한다.
    :raises: 본 helper 자체는 raise 안 함. 인스턴스만 반환.

    >>> exc = not_found_resource("문서", "doc-42")
    >>> exc.message
    '문서을(를) 찾을 수 없습니다: doc-42'
    >>> exc.http_status
    404

    .. note::
        - **메시지 표준**: ``f"{label_ko}을(를) 찾을 수 없습니다: {resource_id}"``.
          한국어 조사가 도메인 단어 받침에 따라 어색할 수 있어 ``"을(를)"`` 양립.
        - error_code 자동 생성 (예: ``f"NOT_FOUND_{label_ko_upper}"``) 은 한국어
          라벨이라 의미 없음 → 호출자 명시.
        - 정확히 같은 패턴 (`f"{Resource} '{id}' not found"` 영문) 24 사이트:
          Document 15 / Version 3 / Folder 3 / Collection 2 / 기타 1.
    """
    # 지연 import — utils/http_errors → api/errors/exceptions 순환 회피.
    from app.api.errors.exceptions import ApiNotFoundError

    err = ApiNotFoundError(
        f"{label_ko}을(를) 찾을 수 없습니다: {resource_id}",
        details=[
            {
                "field": "resource_id",
                "reason": "not found",
                "label": label_ko,
                "resource_id": resource_id,
            }
        ],
    )
    if error_code:
        err.error_code = error_code  # 인스턴스 attribute override
    return err
