"""
S3 Phase 2 FG 2-2 — tag_rules.normalize_tag / extract_tags_from_snapshot 파서 단위.

블로커1 결정서 §3.3·§3.4 규약 회귀.
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


class TestNormalizeTag:
    def test_plain(self):
        from app.services.tag_rules import normalize_tag
        assert normalize_tag("ai") == "ai"

    def test_hash_prefix_stripped(self):
        from app.services.tag_rules import normalize_tag
        assert normalize_tag("#ai") == "ai"

    def test_trim_and_lower(self):
        from app.services.tag_rules import normalize_tag
        assert normalize_tag("  #AI/ML ") == "ai/ml"

    def test_hangul(self):
        from app.services.tag_rules import normalize_tag
        assert normalize_tag("한글") == "한글"

    def test_nested(self):
        from app.services.tag_rules import normalize_tag
        assert normalize_tag("ai/ml") == "ai/ml"

    def test_underscore_dash(self):
        from app.services.tag_rules import normalize_tag
        assert normalize_tag("ml_engineer") == "ml_engineer"
        assert normalize_tag("test-tag") == "test-tag"

    def test_double_hash_rejected(self):
        from app.services.tag_rules import normalize_tag
        # ##foo 는 위반 (앞의 # 1개만 허용)
        assert normalize_tag("##heading") is None

    def test_empty_rejected(self):
        from app.services.tag_rules import normalize_tag
        assert normalize_tag("") is None
        assert normalize_tag("   ") is None

    def test_too_long_rejected(self):
        from app.services.tag_rules import normalize_tag
        assert normalize_tag("a" * 100) is None

    def test_whitespace_inside_rejected(self):
        from app.services.tag_rules import normalize_tag
        assert normalize_tag("sp ace") is None

    def test_nfkc_normalization(self):
        """NFKC — 한글 호환 글자를 같은 코드포인트로 정규화."""
        from app.services.tag_rules import normalize_tag
        # 가(가 = NFC) + 가(조합형) 은 NFKC 후 동일
        # FULLWIDTH 숫자도 NFKC 로 반절폭으로
        assert normalize_tag("ＡＩ") == "ai"  # 전각 알파벳 → 반각 + lower


class TestExtractTagsFromSnapshot:
    @staticmethod
    def _doc(text: str, **extra_text_kw) -> dict:
        return {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": text, **extra_text_kw}],
                }
            ],
        }

    def test_inline_simple(self):
        from app.services.tag_rules import extract_tags_from_snapshot
        result = extract_tags_from_snapshot(self._doc("안녕 #ai 하세요"), None)
        assert result == [("ai", "inline")]

    def test_multiple_inline(self):
        from app.services.tag_rules import extract_tags_from_snapshot
        result = extract_tags_from_snapshot(
            self._doc("##heading 첫 줄 #foo 다음 #bar"), None,
        )
        assert sorted(n for n, _ in result) == ["bar", "foo"]

    def test_nested_inline(self):
        from app.services.tag_rules import extract_tags_from_snapshot
        result = extract_tags_from_snapshot(self._doc("#ai/ml 중첩 태그"), None)
        assert result == [("ai/ml", "inline")]

    def test_frontmatter_only(self):
        from app.services.tag_rules import extract_tags_from_snapshot
        result = extract_tags_from_snapshot(None, {"tags": ["AI", "  Ml/X  "]})
        assert result == [("ai", "frontmatter"), ("ml/x", "frontmatter")]

    def test_both_source_combined(self):
        from app.services.tag_rules import extract_tags_from_snapshot
        result = extract_tags_from_snapshot(
            self._doc("본문 #ai"), {"tags": ["AI"]},
        )
        assert result == [("ai", "both")]

    def test_code_block_excluded(self):
        from app.services.tag_rules import extract_tags_from_snapshot
        snap = {
            "type": "doc",
            "content": [
                {
                    "type": "codeBlock",
                    "content": [{"type": "text", "text": "#skipme"}],
                }
            ],
        }
        assert extract_tags_from_snapshot(snap, None) == []

    def test_link_mark_text_excluded(self):
        from app.services.tag_rules import extract_tags_from_snapshot
        result = extract_tags_from_snapshot(
            self._doc(
                "#skip 링크 안",
                marks=[{"type": "link", "attrs": {"href": "https://example.com"}}],
            ),
            None,
        )
        assert result == []

    def test_double_hash_not_captured(self):
        from app.services.tag_rules import extract_tags_from_snapshot
        result = extract_tags_from_snapshot(self._doc("##heading 실제 #ok"), None)
        assert result == [("ok", "inline")]

    def test_metadata_tags_non_list_ignored(self):
        """metadata.tags 가 list 가 아니면 조용히 무시."""
        from app.services.tag_rules import extract_tags_from_snapshot
        assert extract_tags_from_snapshot(None, {"tags": "not-a-list"}) == []
        assert extract_tags_from_snapshot(None, {"tags": None}) == []
        assert extract_tags_from_snapshot(None, {}) == []

    def test_bullet_list_recurse(self):
        from app.services.tag_rules import extract_tags_from_snapshot
        snap = {
            "type": "doc",
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "#foo"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        result = extract_tags_from_snapshot(snap, None)
        assert result == [("foo", "inline")]

    def test_empty_snapshot_and_metadata(self):
        from app.services.tag_rules import extract_tags_from_snapshot
        assert extract_tags_from_snapshot(None, None) == []
        assert extract_tags_from_snapshot({"type": "doc", "content": []}, None) == []

    def test_hangul_tag_inline(self):
        from app.services.tag_rules import extract_tags_from_snapshot
        result = extract_tags_from_snapshot(self._doc("안녕 #기획"), None)
        assert result == [("기획", "inline")]

    def test_duplicate_inline_deduped(self):
        from app.services.tag_rules import extract_tags_from_snapshot
        result = extract_tags_from_snapshot(
            self._doc("#foo #foo 여러 번 #foo"), None,
        )
        assert result == [("foo", "inline")]

    def test_same_tag_both_sources(self):
        """본문에 #ai 이 있고 frontmatter 에도 AI 가 있으면 source=both."""
        from app.services.tag_rules import extract_tags_from_snapshot
        result = extract_tags_from_snapshot(
            self._doc("본문 #ai 포함"),
            {"tags": ["ai", "extra"]},
        )
        names = {n: s for n, s in result}
        assert names.get("ai") == "both"
        assert names.get("extra") == "frontmatter"

    def test_invalid_metadata_item_ignored(self):
        """metadata.tags 리스트 안에 str 아닌 항목은 개별 무시."""
        from app.services.tag_rules import extract_tags_from_snapshot
        result = extract_tags_from_snapshot(
            None, {"tags": ["ok", 123, None, {"nested": True}]},
        )
        assert result == [("ok", "frontmatter")]
