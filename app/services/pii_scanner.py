"""
PII Scanner — S3 Phase 2 FG 2-6.

옵시디언 vault import 시점에 markdown 본문에서 한국 PII 후보를 탐지·마스킹한다.

탐지 대상 (task2-6.md §2.1 (5))
-------------------------------
- **주민등록번호** (한국): `\\d{6}-\\d{7}` + checksum 검증
- **이메일**: RFC 5322 단순 정규식 (실용)
- **전화번호**: 한국 휴대폰 (`010-?\\d{4}-?\\d{4}`) + 유선 (지역번호 + 4-digit)
- **신용카드 번호**: 13~19 digit + Luhn 검증

설계
----
- **원문 저장 금지** — 발견된 PII 의 raw 값은 보고서에 저장하지 않음 (task2-6.md §9).
- 대신 **마스킹된 sample snippet** (앞뒤 context 20자 + 값 부분 `***`) 을 report 에 포함.
- `mask_pii(text)` 가 마스킹된 텍스트를 반환 — 사용자 옵션으로 ProseMirror 문서 자체에도 적용 가능.
- 거짓양성 (false positive) 최소화 — checksum / Luhn 의무.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterator, Literal, Optional

logger = logging.getLogger(__name__)


PIIKind = Literal["rrn", "email", "phone", "card"]


# ---------------------------------------------------------------------------
# 정규식 — 1차 후보 탐지 (validator 가 false positive 제거)
# ---------------------------------------------------------------------------

# 주민등록번호: 6자리-7자리. 첫 7자리 합리성 검증 후 checksum.
_RRN_RE = re.compile(r"\b(\d{6})-(\d{7})\b")

# 이메일: 단순 RFC 5322. 외부 의존 회피.
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
)

# 한국 휴대폰: 010 / 011 / 016 / 017 / 018 / 019. -없음 / 한 개 / 두 개 모두 허용.
_PHONE_MOBILE_RE = re.compile(
    r"\b(01[016789])[- .]?(\d{3,4})[- .]?(\d{4})\b",
)
# 한국 유선: 02 (서울) 또는 03X / 04X / 05X / 06X 지역번호. 4자리-4자리.
_PHONE_LANDLINE_RE = re.compile(
    r"\b(0[2-6]\d?)[- .]?(\d{3,4})[- .]?(\d{4})\b",
)

# 카드 번호: 13~19 digit. 공백/하이픈 허용. 매칭 후 Luhn 검증.
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _validate_rrn(digits: str) -> bool:
    """주민등록번호 13자리 checksum 검증.

    공식: 가중치 [2,3,4,5,6,7,8,9,2,3,4,5] * 앞 12자리. 합 mod 11 == 11 - 마지막자리 mod 10.
    위키 공식 기준.
    """
    if len(digits) != 13:
        return False
    if not digits.isdigit():
        return False
    weights = [2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5]
    s = sum(int(d) * w for d, w in zip(digits[:12], weights))
    check = (11 - (s % 11)) % 10
    return check == int(digits[12])


def _luhn_valid(digits: str) -> bool:
    """카드 Luhn 검증 — 마지막 자리가 check digit."""
    if not digits.isdigit() or len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    reverse = digits[::-1]
    for i, ch in enumerate(reverse):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ---------------------------------------------------------------------------
# PIIFinding
# ---------------------------------------------------------------------------

@dataclass
class PIIFinding:
    kind: PIIKind
    masked_snippet: str  # 앞뒤 context 20자 + 값 마스킹
    file_path: Optional[str] = None  # 호출자가 채워줌
    span: Optional[tuple[int, int]] = None  # text 내 시작/끝 (디버그용)


@dataclass
class ScanResult:
    findings: list[PIIFinding] = field(default_factory=list)

    def count_by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.kind] = out.get(f.kind, 0) + 1
        return out


# ---------------------------------------------------------------------------
# Snippet 생성 — 앞뒤 20자 context + 값 마스킹
# ---------------------------------------------------------------------------

_CONTEXT_LEN = 20


def _make_snippet(text: str, start: int, end: int, mask: str) -> str:
    """앞뒤 context 20자 + 값을 mask 로 치환한 짧은 snippet."""
    pre = text[max(0, start - _CONTEXT_LEN):start]
    post = text[end:end + _CONTEXT_LEN]
    return f"...{pre}{mask}{post}..."


# ---------------------------------------------------------------------------
# scan_text — 단일 텍스트의 PII 탐지
# ---------------------------------------------------------------------------

def scan_text(text: str) -> ScanResult:
    """text 의 모든 PII 후보를 검증·수집. 원문은 저장하지 않음.

    매칭 우선순위 — 같은 위치에서 여러 종류가 매칭될 수 있으나 본 함수는 모두 보고.
    중복 제거 / dedup 은 호출자가 결정.
    """
    result = ScanResult()
    if not text:
        return result

    # 1. RRN
    for m in _RRN_RE.finditer(text):
        digits = m.group(1) + m.group(2)
        if not _validate_rrn(digits):
            continue
        snippet = _make_snippet(text, m.start(), m.end(), "******-*******")
        result.findings.append(PIIFinding(
            kind="rrn",
            masked_snippet=snippet,
            span=(m.start(), m.end()),
        ))

    # 2. Email
    for m in _EMAIL_RE.finditer(text):
        # email value 마스킹 — local part 보존, domain 만 마스킹
        # 단 PII 보고서는 raw 저장 금지 정책이라 전체 마스킹
        snippet = _make_snippet(text, m.start(), m.end(), "***@***")
        result.findings.append(PIIFinding(
            kind="email",
            masked_snippet=snippet,
            span=(m.start(), m.end()),
        ))

    # 3. Phone (mobile + landline)
    for m in _PHONE_MOBILE_RE.finditer(text):
        snippet = _make_snippet(text, m.start(), m.end(), "***-****-****")
        result.findings.append(PIIFinding(
            kind="phone",
            masked_snippet=snippet,
            span=(m.start(), m.end()),
        ))
    for m in _PHONE_LANDLINE_RE.finditer(text):
        # mobile 정규식과 겹칠 가능성 — 동일 span 이미 추가됐으면 skip
        if any(f.kind == "phone" and f.span == (m.start(), m.end()) for f in result.findings):
            continue
        snippet = _make_snippet(text, m.start(), m.end(), "***-***-****")
        result.findings.append(PIIFinding(
            kind="phone",
            masked_snippet=snippet,
            span=(m.start(), m.end()),
        ))

    # 4. Credit card
    for m in _CARD_RE.finditer(text):
        digits_only = re.sub(r"\D", "", m.group(0))
        if not _luhn_valid(digits_only):
            continue
        snippet = _make_snippet(text, m.start(), m.end(), "****-****-****-****")
        result.findings.append(PIIFinding(
            kind="card",
            masked_snippet=snippet,
            span=(m.start(), m.end()),
        ))

    # span 정렬 (보고서 가독성)
    result.findings.sort(key=lambda f: (f.span or (0, 0))[0])
    return result


# ---------------------------------------------------------------------------
# mask_pii — 텍스트 자체에 마스킹 적용
# ---------------------------------------------------------------------------

def mask_pii(text: str) -> tuple[str, ScanResult]:
    """text 안의 PII 를 `*` 로 치환한 텍스트 + ScanResult 반환.

    옵션: 업로드 폼에서 "PII 자동 마스킹" 체크 시 변환된 ProseMirror doc text 에도 적용
    (task2-6.md §2.1 (5)).

    중요 (보안): finding 의 masked_snippet 은 **마스킹된 텍스트** 기준으로 재생성한다 —
    같은 컨텍스트에 다른 PII 가 있어도 둘 다 마스킹된 형태로 보고 (스니펫에 raw 노출 차단).
    같은 길이 마스크 (`*` × 자릿수) 로 치환해 후속 span 위치를 보존한다.
    """
    if not text:
        return text, ScanResult()
    result = scan_text(text)

    # 1. 모든 PII 위치를 같은 길이 마스크로 in-place 치환 (길이 보존 → span 안정)
    masked_chars = list(text)
    seen_spans: list[tuple[int, int]] = []
    for f in result.findings:
        if f.span is None:
            continue
        s, e = f.span
        # 겹치는 span 은 첫 매칭만 마스킹
        if any(not (e <= os_ or s >= oe_) for os_, oe_ in seen_spans):
            continue
        masked_chars[s:e] = list("*" * (e - s))
        seen_spans.append((s, e))
    masked_text = "".join(masked_chars)

    # 2. findings 의 masked_snippet 을 마스킹된 텍스트 기준 재생성 — context 누설 차단
    new_findings: list[PIIFinding] = []
    for f in result.findings:
        if f.span is None:
            new_findings.append(f)
            continue
        s, e = f.span
        pre = masked_text[max(0, s - _CONTEXT_LEN):s]
        post = masked_text[e:e + _CONTEXT_LEN]
        new_findings.append(PIIFinding(
            kind=f.kind,
            masked_snippet=f"...{pre}{masked_text[s:e]}{post}...",
            file_path=f.file_path,
            span=f.span,
        ))
    return masked_text, ScanResult(findings=new_findings)


__all__ = [
    "PIIFinding",
    "PIIKind",
    "ScanResult",
    "mask_pii",
    "scan_text",
]
