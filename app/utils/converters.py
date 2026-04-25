"""변환 유틸 — UUID · 타입 캐스팅 보일러플레이트 통합.

본 모듈은 ``docs/함수도서관/backend.md`` §1.2 BE-G1 에 등록된 공통 유틸이다.

제공 함수:
    - :func:`uuid_str_or_none` — ``UUID | str | None`` → ``str | None`` (안전 캐스팅).
    - :func:`ensure_uuid` — ``UUID | str`` → ``UUID`` (검증 + 동일 에러 계약 매핑).

도입 배경:
    - 리포지토리·서비스 전반에 ``str(x.id) if x.id else None`` 패턴이 44회,
      ``UUID(value)`` 직호출이 72회 산재.
    - 후자는 ``ValueError`` 를 그대로 흘려 라우터마다 다른 방식으로 처리되고 있었다.
    - CONSTITUTION 제8조(Single Responsibility) · 제10조(Docstring as Agent Contract) ·
      제14조(Shared Error Contract) 준수 목적.

마이그레이션 정책 (2026-04-25 BE-G1 도입 시점):
    - ``uuid_str_or_none`` 은 호출지 의미와 100% 동치이므로 본 그룹에서 일괄 마이그레이션.
    - ``ensure_uuid`` 는 호출지마다 ``ValueError`` 흐름이 다르고
      (``try/except ValueError`` 안에 있는 경우, ``HTTPException`` 으로 직접 변환하는
      경우, FastAPI Path/Query 검증에 위임하는 경우 등) 의미가 미묘히 달라 본 그룹에서는
      **유틸만 신설**한다. 호출지 마이그레이션은 라우터별 검증 후 별 라운드.

보안 메모:
    - 본 모듈은 외부 I/O · DB · 로깅 · 부수효과가 없다 (순수 함수).
    - ``ensure_uuid`` 의 에러 메시지는 외부에 노출되므로 사용자 입력 원본을 그대로
      넣지 않는다 (인젝션 방어).
"""
from __future__ import annotations

from typing import Union
from uuid import UUID

from app.api.errors.exceptions import ApiValidationError

__all__ = ["uuid_str_or_none", "ensure_uuid"]


def uuid_str_or_none(value: Union[UUID, str, None]) -> Union[str, None]:
    """``UUID``/``str``/``None`` 을 ``str | None`` 로 안전하게 캐스팅한다.

    동치 표현:
        ``str(value) if value else None``
    하지만 본 함수는 입력 타입을 명시적으로 좁혀 가독성과 정적 분석을 돕는다.

    :param value: ``UUID`` 인스턴스, UUID 문자열, 또는 ``None``.
    :returns: ``str`` 표현 또는 ``None`` (입력이 falsy 인 경우).

    >>> from uuid import UUID
    >>> uuid_str_or_none(UUID("550e8400-e29b-41d4-a716-446655440000"))
    '550e8400-e29b-41d4-a716-446655440000'
    >>> uuid_str_or_none("550e8400-e29b-41d4-a716-446655440000")
    '550e8400-e29b-41d4-a716-446655440000'
    >>> uuid_str_or_none(None) is None
    True

    .. note::
        본 함수는 입력 ``str`` 의 UUID 형식 유효성을 **검증하지 않는다**.
        형식 검증이 필요하면 :func:`ensure_uuid` 로 먼저 정규화한 뒤 ``str`` 으로 변환한다.
        (대부분의 호출지가 DB 에서 읽은 UUID 를 직렬화 목적으로 ``str`` 으로 변환하므로
        형식은 이미 보장됨.)
    """
    if not value:
        return None
    return str(value)


def ensure_uuid(
    value: Union[UUID, str],
    *,
    label: str = "id",
    status_code: int = 400,
) -> UUID:
    """``UUID`` 인스턴스 또는 UUID 문자열을 받아 ``UUID`` 로 정규화한다.

    검증 실패 시 :class:`ApiValidationError` 를 던진다 (동일 에러 계약, 제14조).

    :param value: ``UUID`` 인스턴스 또는 UUID 형식의 문자열.
    :param label: 에러 메시지·details 의 ``field`` 키에 사용할 식별자 (예: ``"document_id"``).
    :param status_code: 검증 실패 시 응답 status code. 기본 400 (validation_error 표준).
        호출지가 422 (unprocessable_entity) 호환이 필요하면 ``status_code=422`` 명시.
        D5 (2026-04-25): extraction_schemas 등 기존 422 호출자의 마이그레이션 호환 옵션.
    :returns: ``UUID`` 인스턴스.
    :raises ApiValidationError: 입력이 ``UUID`` 형식이 아닌 경우. ``details`` 에
        ``[{"field": label, "reason": "must be a valid UUID", "code": "INVALID_UUID"}]``
        를 포함한다. 인스턴스의 ``http_status`` 는 ``status_code`` 인자로 override.

    >>> from uuid import UUID
    >>> ensure_uuid("550e8400-e29b-41d4-a716-446655440000")
    UUID('550e8400-e29b-41d4-a716-446655440000')
    >>> ensure_uuid(UUID("550e8400-e29b-41d4-a716-446655440000"))
    UUID('550e8400-e29b-41d4-a716-446655440000')

    .. note::
        D5 status_code 옵션 (2026-04-25): 기존 호출자가 ``raise unprocessable_entity(...)``
        같은 422 패턴을 쓰던 사이트는 ``ensure_uuid(value, label=..., status_code=422)``
        로 호환 마이그레이션 가능. handlers.py 가 인스턴스의 ``http_status`` 를 그대로
        사용 (line 132/158 — CONSTITUTION 제14조 호환).
    """
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        err = ApiValidationError(
            f"{label}이(가) 올바른 UUID 형식이 아닙니다",
            details=[
                {"field": label, "reason": "must be a valid UUID", "code": "INVALID_UUID"}
            ],
        )
        # 인스턴스 attribute 로 클래스 default (400) override.
        # handlers.py 가 exc.http_status 를 그대로 사용.
        if status_code != 400:
            err.http_status = status_code
        raise err from exc
