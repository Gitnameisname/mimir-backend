"""
S3 Phase 2 FG 2-6 — pii_scanner 단위 회귀.

task2-6.md §4 Step 3: 각 PII 유형별 ≥ 2 건, 합 ≥ 10 건.
보안 핵심: false positive 최소화 (checksum / Luhn 의무) + 원문 저장 금지.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 주민등록번호 (RRN)
# ---------------------------------------------------------------------------

class TestRRN:
    def test_valid_rrn_detected(self):
        from app.services.pii_scanner import scan_text
        # 알려진 valid 주민번호 예시 (checksum 통과 — 가공 데이터)
        # 800101-1234567: weights=[2,3,4,5,6,7,8,9,2,3,4,5]
        # s = 8*2+0*3+0*4+1*5+0*6+1*7+1*8+2*9+3*2+4*3+5*4+6*5
        #   = 16+0+0+5+0+7+8+18+6+12+20+30 = 122
        # check = (11 - (122 % 11)) % 10 = (11 - 1) % 10 = 0
        # 마지막 digit 7 ≠ 0 → invalid. 다른 valid 시퀀스 만들기:
        # 900101-1234567 → s = 9*2+0*3+0*4+1*5+0*6+1*7+1*8+2*9+3*2+4*3+5*4+6*5
        # = 18+0+0+5+0+7+8+18+6+12+20+30 = 124
        # check = (11 - 124%11) % 10 = (11 - 3) % 10 = 8. 마지막 7 ≠ 8 → invalid.
        # 강제 — 마지막 자리에 valid check digit 적용
        # 900101-123456[8]:
        digits = "900101-1234568"
        result = scan_text(f"my id is {digits} please")
        assert len(result.findings) == 1
        assert result.findings[0].kind == "rrn"
        # 원문 저장 금지 — snippet 에 raw digits 없음
        assert digits not in result.findings[0].masked_snippet
        assert "******" in result.findings[0].masked_snippet

    def test_invalid_rrn_checksum_rejected(self):
        from app.services.pii_scanner import scan_text
        # checksum 안 맞는 임의 13자리
        result = scan_text("fake 123456-1234567 abc")
        rrn = [f for f in result.findings if f.kind == "rrn"]
        assert rrn == []  # false positive 차단


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

class TestEmail:
    def test_simple_email(self):
        from app.services.pii_scanner import scan_text
        result = scan_text("contact me at user@example.com please")
        emails = [f for f in result.findings if f.kind == "email"]
        assert len(emails) == 1
        assert "user@example.com" not in emails[0].masked_snippet
        assert "***@***" in emails[0].masked_snippet

    def test_multiple_emails(self):
        from app.services.pii_scanner import scan_text
        text = "first foo@bar.com and second baz.qux+tag@sub.example.co.kr"
        result = scan_text(text)
        emails = [f for f in result.findings if f.kind == "email"]
        assert len(emails) == 2


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------

class TestPhone:
    def test_mobile_with_dashes(self):
        from app.services.pii_scanner import scan_text
        result = scan_text("call 010-1234-5678")
        phones = [f for f in result.findings if f.kind == "phone"]
        assert len(phones) == 1
        assert "010-1234-5678" not in phones[0].masked_snippet

    def test_mobile_no_dashes(self):
        from app.services.pii_scanner import scan_text
        result = scan_text("call 01012345678 ")
        phones = [f for f in result.findings if f.kind == "phone"]
        assert len(phones) >= 1

    def test_landline(self):
        from app.services.pii_scanner import scan_text
        result = scan_text("call 02-1234-5678 today")
        phones = [f for f in result.findings if f.kind == "phone"]
        assert len(phones) >= 1


# ---------------------------------------------------------------------------
# Credit card (Luhn)
# ---------------------------------------------------------------------------

class TestCard:
    def test_valid_luhn_card_detected(self):
        from app.services.pii_scanner import scan_text
        # 4111111111111111 — 알려진 valid Luhn (Visa 테스트)
        result = scan_text("card 4111-1111-1111-1111 paid")
        cards = [f for f in result.findings if f.kind == "card"]
        assert len(cards) == 1
        assert "4111" not in cards[0].masked_snippet

    def test_invalid_luhn_rejected(self):
        from app.services.pii_scanner import scan_text
        # 4111111111111112 (마지막 자리 변경) — Luhn invalid
        result = scan_text("card 4111-1111-1111-1112 paid")
        cards = [f for f in result.findings if f.kind == "card"]
        assert cards == []  # false positive 차단

    def test_short_digits_not_card(self):
        from app.services.pii_scanner import scan_text
        result = scan_text("ref 12345 done")
        cards = [f for f in result.findings if f.kind == "card"]
        assert cards == []


# ---------------------------------------------------------------------------
# 통합
# ---------------------------------------------------------------------------

class TestCombined:
    def test_count_by_kind(self):
        from app.services.pii_scanner import scan_text
        text = (
            "id 900101-1234568, "
            "email a@b.com, "
            "phone 010-1234-5678, "
            "card 4111111111111111"
        )
        result = scan_text(text)
        counts = result.count_by_kind()
        assert counts.get("rrn", 0) >= 1
        assert counts.get("email", 0) >= 1
        assert counts.get("phone", 0) >= 1
        assert counts.get("card", 0) >= 1

    def test_no_pii_clean_text(self):
        from app.services.pii_scanner import scan_text
        result = scan_text("This is a normal note with no sensitive data.")
        assert result.findings == []

    def test_empty_text(self):
        from app.services.pii_scanner import scan_text
        result = scan_text("")
        assert result.findings == []


# ---------------------------------------------------------------------------
# mask_pii — 텍스트 마스킹
# ---------------------------------------------------------------------------

class TestMaskPII:
    def test_mask_replaces_pii_with_asterisks(self):
        from app.services.pii_scanner import mask_pii
        text = "email a@b.com here"
        masked, result = mask_pii(text)
        assert "a@b.com" not in masked
        assert "***" in masked
        assert len(result.findings) >= 1

    def test_mask_preserves_non_pii(self):
        from app.services.pii_scanner import mask_pii
        text = "Hello world this is fine"
        masked, result = mask_pii(text)
        assert masked == text
        assert result.findings == []

    def test_mask_does_not_save_raw(self):
        """마스킹 결과 + ScanResult 어디에도 원문 PII 가 남지 않는다."""
        from app.services.pii_scanner import mask_pii
        text = "id 900101-1234568 phone 010-1234-5678"
        masked, result = mask_pii(text)
        assert "900101-1234568" not in masked
        assert "010-1234-5678" not in masked
        for f in result.findings:
            assert "900101-1234568" not in f.masked_snippet
            assert "010-1234-5678" not in f.masked_snippet
