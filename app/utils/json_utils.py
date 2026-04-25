"""JSON 직렬화·역직렬화 유틸 — 한글 친화 + JSONB 컬럼 친화.

본 모듈은 ``docs/함수도서관/backend.md`` §1.3 BE-G2 에 등록된 공통 유틸이다.

제공 함수:
    - :func:`loads_maybe` — ``str`` 이면 ``json.loads``, 아니면 그대로 반환.
    - :func:`dumps_ko` — ``json.dumps(value, ensure_ascii=False, **kwargs)``.

도입 배경:
    - 리포지토리의 JSONB 컬럼 읽기에서 드라이버에 따라 ``str`` 또는 ``dict``/``list``
      가 반환되는 패턴이 흔하다 (psycopg2 의 register_default_jsonb 적용 여부에
      따라 달라짐). ``if isinstance(raw, str): raw = json.loads(raw)`` 같은 분기가
      리포지토리 전반에 산재.
    - 한글 라벨·코멘트를 그대로 저장하기 위해 ``ensure_ascii=False`` 가 38곳에서
      반복.
    - CONSTITUTION 제8조(Single Responsibility) · 제10조(Docstring as Agent Contract)
      준수 목적.

마이그레이션 정책 (2026-04-25 BE-G2 도입 시점):
    - ``loads_maybe`` 는 **JSONDecodeError 흐름을 보존**한다. 호출지의
      ``try/except json.JSONDecodeError`` 가 그대로 동작하도록 하기 위함.
      도서관 사양의 ``ApiValidationError`` 매핑 (예: ``loads_strict``) 은 호출지
      try/except 흐름 다양성 검토 후 별 라운드.
    - ``dumps_ko`` 는 ``ensure_ascii=False`` 만 고정하고 나머지 kwargs 는 호출자가
      그대로 넘길 수 있다 (``separators``, ``sort_keys``, ``indent`` 등).

보안 메모:
    - 본 모듈은 외부 I/O · DB · 로깅 · 부수효과가 없다 (순수 함수).
    - ``loads_maybe`` 는 입력 ``str`` 이 신뢰되지 않은 경우 deserialization 공격을
      당할 수 있으나 ``json`` 자체는 코드 실행 채널이 아니므로 안전. 다만 결과
      자료구조 크기가 큰 경우 메모리 압박 — 호출자가 size 제한.
"""
from __future__ import annotations

import json
from typing import Any

from app.api.errors.exceptions import ApiValidationError

__all__ = ["loads_maybe", "dumps_ko", "loads_strict"]


def loads_maybe(value: Any) -> Any:
    """``value`` 가 ``str`` 이면 ``json.loads(value)``, 아니면 그대로 반환.

    JSONB 컬럼 읽기에서 드라이버 동작에 따라 ``str``/``dict``/``list`` 어느 쪽이든
    올 수 있는 케이스를 단일 호출로 정규화한다.

    :param value: ``str`` (JSON 문자열) 또는 이미 deserialize 된 객체 (보통
        ``dict`` / ``list`` / ``None``).
    :returns: ``str`` 입력은 ``json.loads`` 결과, 그 외는 ``value`` 그대로.
    :raises json.JSONDecodeError: ``value`` 가 ``str`` 이지만 JSON 으로 파싱 불가
        (호출자의 try/except 흐름 보존을 위해 그대로 전파).

    >>> loads_maybe('{"a": 1}')
    {'a': 1}
    >>> loads_maybe({'a': 1})
    {'a': 1}
    >>> loads_maybe(None) is None
    True
    >>> loads_maybe([1, 2])
    [1, 2]

    .. note::
        ``ApiValidationError`` 매핑 변형 (`loads_strict`) 은 호출지 try/except 흐름
        검토 후 별 라운드 (BE-G1 의 `ensure_uuid` 와 같은 패턴).
    """
    if isinstance(value, str):
        return json.loads(value)
    return value


def dumps_ko(value: Any, **kwargs: Any) -> str:
    """``json.dumps(value, ensure_ascii=False, **kwargs)`` 의 단일 엔트리.

    한글 주석 · 라벨을 그대로 저장 · 직렬화하기 위한 표준 호출.

    :param value: ``json.dumps`` 가 받을 수 있는 모든 값 (dict/list/str/숫자/None/bool 등).
    :param kwargs: ``json.dumps`` 의 나머지 옵션 (``separators``, ``sort_keys``,
        ``indent``, ``default`` 등). ``ensure_ascii`` 는 호출자가 다시 지정해도
        본 함수가 ``False`` 로 강제 (호출자가 명시 시 ``TypeError: multiple values``).

    :returns: UTF-8 한글이 그대로 보존된 JSON 문자열.

    >>> dumps_ko({"k": "한글"})
    '{"k": "한글"}'
    >>> dumps_ko([1, 2, 3])
    '[1, 2, 3]'
    >>> dumps_ko({"a": 1}, separators=(",", ":"))
    '{"a":1}'
    >>> dumps_ko({"b": 2, "a": 1}, sort_keys=True)
    '{"a": 1, "b": 2}'

    .. note::
        ``ensure_ascii=False`` 를 호출자가 다시 명시하면 키워드 충돌로 ``TypeError``
        가 발생한다 (의도된 보호 — 본 헬퍼가 한글 직렬화의 단일 진실점).
    """
    return json.dumps(value, ensure_ascii=False, **kwargs)


def loads_strict(value: Any, *, label: str = "value") -> Any:
    """``loads_maybe`` 와 같은 시맨틱이지만 파싱 실패 시 :class:`ApiValidationError` 매핑.

    BE-G1 ``ensure_uuid`` 와 동일 패턴 — 동일 에러 계약 (CONSTITUTION 제14조)
    으로 변환해 라우터에서 자동으로 400 응답.

    :param value: ``str`` (JSON) 또는 이미 deserialize 된 객체.
    :param label: 에러 메시지·details 의 ``field`` 키.
    :returns: ``loads_maybe(value)`` 결과.
    :raises ApiValidationError: ``value`` 가 ``str`` 이지만 JSON 으로 파싱 불가.

    >>> loads_strict('{"a": 1}')
    {'a': 1}
    >>> loads_strict({'a': 1})
    {'a': 1}

    .. note::
        호출지 마이그레이션 (try/except json.JSONDecodeError → 본 helper 호출) 은
        호출지 흐름 검토 후 별 라운드.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ApiValidationError(
                f"{label}이(가) 올바른 JSON 형식이 아닙니다",
                details=[
                    {"field": label, "reason": "must be valid JSON", "code": "INVALID_JSON"}
                ],
            ) from exc
    return value
