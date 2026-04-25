"""Cron 표현식 유틸 (Phase 14-14).

5-field cron (`m h dom mon dow`) 만 지원.
의존성 없는 순수 stdlib 구현 — 외부 라이브러리 취약점 회피 (CLAUDE.md §2).

제공 기능:
  - `validate(expr)`         → str 원본 반환 (유효) 또는 ValueError
  - `describe_ko(expr)`      → 사람이 읽을 수 있는 한국어 설명
  - `next_run(expr, now)`    → 다음 실행 시각 (UTC-naive datetime)

제한:
  - `@reboot`, step syntax (`*/5`), list (`1,15`), range (`1-5`) 지원 → 단순 구현
  - `?`/`L`/`W`/`#` 등 Quartz 확장은 미지원
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable
from app.utils.time import utcnow

_FIELD_RANGES = [
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day-of-month
    (1, 12),   # month
    (0, 6),    # day-of-week (0=Sunday)
]
_FIELD_NAMES_KO = ["분", "시", "일", "월", "요일"]

# 각 필드 토큰에 허용되는 문자 화이트리스트 — 이 이외는 즉시 거부
_FIELD_PATTERN = re.compile(r"^[0-9*,/\-]+$")

_WEEKDAY_KO = ["일요일", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일"]


def _parse_field(token: str, lo: int, hi: int) -> set[int]:
    """단일 필드 → 허용되는 정수 집합."""
    if not _FIELD_PATTERN.match(token):
        raise ValueError(f"허용되지 않은 문자 포함: {token}")
    values: set[int] = set()
    for part in token.split(","):
        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            if not step_str.isdigit() or int(step_str) < 1:
                raise ValueError(f"잘못된 step: {part}")
            step = int(step_str)
        else:
            base = part
        if base == "*" or base == "":
            start, end = lo, hi
        elif "-" in base:
            s, e = base.split("-", 1)
            if not s.isdigit() or not e.isdigit():
                raise ValueError(f"잘못된 range: {part}")
            start, end = int(s), int(e)
        else:
            if not base.isdigit():
                raise ValueError(f"잘못된 숫자: {part}")
            start = end = int(base)
        if start < lo or end > hi or start > end:
            raise ValueError(f"범위 벗어남: {part} (허용 {lo}-{hi})")
        values.update(range(start, end + 1, step))
    return values


def validate(expr: str) -> str:
    """5-field cron 검증. 유효하지 않으면 ValueError."""
    if not isinstance(expr, str):
        raise ValueError("cron 표현식은 문자열이어야 합니다.")
    expr = expr.strip()
    if len(expr) > 120:
        raise ValueError("cron 표현식이 너무 깁니다 (120자 이하).")
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("5개 필드(분 시 일 월 요일)여야 합니다.")
    for tok, (lo, hi), name in zip(fields, _FIELD_RANGES, _FIELD_NAMES_KO):
        try:
            _parse_field(tok, lo, hi)
        except ValueError as e:
            raise ValueError(f"{name} 필드 오류: {e}") from None
    return expr


def _fmt_set(values: Iterable[int], lo: int, hi: int, pad: bool = False) -> str:
    vs = sorted(set(values))
    if len(vs) == hi - lo + 1:
        return "매"
    if len(vs) == 1:
        v = vs[0]
        return f"{v:02d}" if pad else str(v)
    shown = ", ".join(f"{v:02d}" if pad else str(v) for v in vs[:5])
    if len(vs) > 5:
        shown += "…"
    return shown


def describe_ko(expr: str) -> str:
    """사람이 읽을 수 있는 한국어 설명. 유효한 cron 만 받는다."""
    validate(expr)
    m_tok, h_tok, dom_tok, mon_tok, dow_tok = expr.strip().split()
    mins = _parse_field(m_tok, 0, 59)
    hours = _parse_field(h_tok, 0, 23)
    doms = _parse_field(dom_tok, 1, 31)
    mons = _parse_field(mon_tok, 1, 12)
    dows = _parse_field(dow_tok, 0, 6)

    every_dom = len(doms) == 31
    every_mon = len(mons) == 12
    every_dow = len(dows) == 7

    # 주기
    if every_dom and every_mon and every_dow:
        when = "매일"
    elif every_dom and every_mon and not every_dow:
        names = ", ".join(_WEEKDAY_KO[d] for d in sorted(dows))
        when = f"매주 {names}"
    elif not every_dom and every_mon and every_dow:
        when = f"매월 {_fmt_set(doms, 1, 31)}일"
    else:
        parts = []
        if not every_mon:
            parts.append(f"{_fmt_set(mons, 1, 12)}월")
        if not every_dom:
            parts.append(f"{_fmt_set(doms, 1, 31)}일")
        if not every_dow:
            names = ", ".join(_WEEKDAY_KO[d] for d in sorted(dows))
            parts.append(f"{names}")
        when = " ".join(parts) or "매일"

    # 시각
    if len(hours) == 1 and len(mins) == 1:
        h = next(iter(hours))
        mi = next(iter(mins))
        time_str = f"{h:02d}:{mi:02d}"
    elif len(hours) == 1 and len(mins) == 60:
        h = next(iter(hours))
        time_str = f"{h:02d}시 매 분"
    elif len(mins) == 1 and len(hours) == 24:
        mi = next(iter(mins))
        time_str = f"매시 {mi:02d}분"
    else:
        time_str = f"{_fmt_set(hours, 0, 23, pad=True)}시 {_fmt_set(mins, 0, 59, pad=True)}분"

    return f"{when} {time_str}"


# ─── 다음 실행 시각 계산 ───────────────────────────────────────────
_MAX_ITERATIONS = 4 * 365 * 24 * 60  # 최대 4년 탐색 (실질 상한)


def next_run(expr: str, now: datetime | None = None) -> datetime | None:
    """현재 시각 이후 다음 실행 시각.

    `now` 미지정 시 현재 UTC 사용. 성공 시 UTC-naive datetime 반환.
    """
    validate(expr)
    m_tok, h_tok, dom_tok, mon_tok, dow_tok = expr.strip().split()
    mins = _parse_field(m_tok, 0, 59)
    hours = _parse_field(h_tok, 0, 23)
    doms = _parse_field(dom_tok, 1, 31)
    mons = _parse_field(mon_tok, 1, 12)
    dows = _parse_field(dow_tok, 0, 6)

    # Python 3.12+ datetime.utcnow() deprecated — aware UTC 를 naive 로 변환
    _fallback = utcnow().replace(tzinfo=None)
    t = (now or _fallback).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(_MAX_ITERATIONS):
        # DOM + DOW: cron 관례 — 둘 다 제한 있으면 OR, 하나만 제한이면 AND
        dom_restricted = len(doms) != 31
        dow_restricted = len(dows) != 7
        if dom_restricted and dow_restricted:
            date_ok = (t.day in doms) or (_weekday_sun(t) in dows)
        elif dom_restricted:
            date_ok = t.day in doms
        elif dow_restricted:
            date_ok = _weekday_sun(t) in dows
        else:
            date_ok = True
        if (
            t.month in mons
            and date_ok
            and t.hour in hours
            and t.minute in mins
        ):
            return t
        t += timedelta(minutes=1)
    return None


def _weekday_sun(dt: datetime) -> int:
    """datetime.weekday() 는 월요일=0 이나 cron 관례는 일요일=0. 변환."""
    # Python: Mon=0..Sun=6 → cron: Sun=0..Sat=6
    return (dt.weekday() + 1) % 7
