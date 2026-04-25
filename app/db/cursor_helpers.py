"""Cursor 실행 + mapper 적용 보일러 통합.

본 모듈은 ``docs/함수도서관/backend.md`` §1.8 BE-G5 에 등록된 공통 유틸이다.

제공 함수:
    - :func:`fetch_one_as` — execute → fetchone → mapper(row) (없으면 None)
    - :func:`fetch_many_as` — execute → fetchall → [mapper(row) ...]

도입 배경:
    - 리포지토리 전반에 ``with conn.cursor() as cur: cur.execute(...); row =
      cur.fetchone(); return mapper(row) if row else None`` 6~8줄 패턴이 산재
      (실측 fetchone 266회 / fetchall 107회).
    - 보일러를 단일 호출로 압축해 가독성 + 회귀 테스트 단위 단순화.

ACL · 보안 메모:
    - **ACL 적용은 SQL 작성 책임자(호출자) 의 몫**. 본 helper 가 임의로
      ``scope_profile_id`` 필터를 넣지 않는다 (S2 ⑤ scope 하드코딩 금지).
    - DB 드라이버 예외 (psycopg2.Error 등) 는 그대로 전파 — 호출자가 도메인
      예외로 매핑.
    - mapper 가 row 변환 중 예외를 던지면 그대로 전파 (감추지 않음).
"""
from __future__ import annotations

from typing import Any, Callable, TypeVar

T = TypeVar("T")

__all__ = ["fetch_one_as", "fetch_many_as"]


def fetch_one_as(
    conn: Any,
    sql: str,
    params: Any,
    mapper: Callable[[Any], T],
) -> T | None:
    """SQL 실행 후 첫 row 를 mapper 로 변환해 반환한다.

    동치 표현::

        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return mapper(row) if row else None

    :param conn: psycopg2 connection (또는 ``.cursor()`` context manager 를
        제공하는 호환 객체).
    :param sql: SQL 문자열 (parameterized — placeholder ``%s`` 또는 ``%(name)s``).
    :param params: ``cur.execute`` 의 두 번째 인자 — tuple / dict / list.
    :param mapper: row → 도메인 객체 변환 callable. row 형식은 cursor 의
        ``cursor_factory`` 에 따라 ``tuple`` 또는 ``dict``.
    :returns: ``mapper(row)`` 결과 또는 None (row 없음).
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return mapper(row) if row else None


def fetch_many_as(
    conn: Any,
    sql: str,
    params: Any,
    mapper: Callable[[Any], T],
) -> list[T]:
    """SQL 실행 후 모든 row 를 mapper 로 변환해 list 로 반환한다.

    동치 표현::

        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            return [mapper(r) for r in rows]

    :returns: 빈 결과는 빈 list ``[]``.
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [mapper(r) for r in rows]
