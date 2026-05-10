"""
S3 Phase 2 FG 2-3 — wikilink_resolver.resolve_wikilinks 단위.

task2-3.md §4 Step 3 의 회귀 시나리오 충족:
  - 단일 일치 → resolved
  - 복수 일치 → ambiguous
  - 없음 → missing
  - viewer Scope 밖 문서 제외 (존재 유출 방지)
  - NFC 정규화 (조합형/완성형 흡수)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake repository — DI 주입용
# ---------------------------------------------------------------------------

class _FakeDocLinksRepo:
    """find_candidates_by_title 만 사용. Scope 필터 동작도 재현."""

    def __init__(self, fixture: dict[str, list[dict[str, Any]]]):
        # fixture: {title_norm: [candidate_dict, ...]} — 모든 후보 (Scope 무관)
        # candidate_dict: {id, title, scope_profile_id, updated_at}
        self.fixture = fixture
        self.calls: list[tuple[str, Optional[Sequence[str]]]] = []

    def find_candidates_by_title(
        self, conn, *, title: str, viewer_scope_profile_ids=None
    ) -> list[dict[str, Any]]:
        self.calls.append((title, viewer_scope_profile_ids))
        all_cands = self.fixture.get(title, [])
        if viewer_scope_profile_ids is None:
            # 필터 skip
            return [{k: v for k, v in c.items() if k != "scope_profile_id"} for c in all_cands]
        ids = list(viewer_scope_profile_ids)
        if not ids:
            return []
        filtered = [c for c in all_cands if c["scope_profile_id"] in ids]
        return [{k: v for k, v in c.items() if k != "scope_profile_id"} for c in filtered]


def _candidate(doc_id: str, title: str, scope_profile_id: str = "scope-A") -> dict[str, Any]:
    return {
        "id": doc_id,
        "title": title,
        "scope_profile_id": scope_profile_id,
        "updated_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
    }


# ---------------------------------------------------------------------------
# 매칭 정책
# ---------------------------------------------------------------------------

class TestMatching:
    def test_single_match_resolved(self):
        from app.services.wikilink_resolver import resolve_wikilinks

        repo = _FakeDocLinksRepo({"Foo": [_candidate("doc-1", "Foo")]})
        result = resolve_wikilinks(
            conn=None,
            from_document_id="src",
            links=[("Foo", "n1")],
            viewer_scope_profile_ids=["scope-A"],
            repository=repo,
        )
        assert result == [("doc-1", "n1", "Foo", "resolved")]

    def test_multi_match_ambiguous(self):
        from app.services.wikilink_resolver import resolve_wikilinks

        repo = _FakeDocLinksRepo(
            {"Foo": [_candidate("doc-1", "Foo"), _candidate("doc-2", "Foo")]}
        )
        result = resolve_wikilinks(
            conn=None,
            from_document_id="src",
            links=[("Foo", "n1")],
            viewer_scope_profile_ids=["scope-A"],
            repository=repo,
        )
        assert result == [(None, "n1", "Foo", "ambiguous")]

    def test_no_match_missing(self):
        from app.services.wikilink_resolver import resolve_wikilinks

        repo = _FakeDocLinksRepo({})
        result = resolve_wikilinks(
            conn=None,
            from_document_id="src",
            links=[("Ghost", "n1")],
            viewer_scope_profile_ids=["scope-A"],
            repository=repo,
        )
        assert result == [(None, "n1", "Ghost", "missing")]

    def test_empty_links_returns_empty(self):
        from app.services.wikilink_resolver import resolve_wikilinks

        repo = _FakeDocLinksRepo({})
        result = resolve_wikilinks(
            conn=None,
            from_document_id="src",
            links=[],
            viewer_scope_profile_ids=["scope-A"],
            repository=repo,
        )
        assert result == []

    def test_multiple_links_mixed_status(self):
        from app.services.wikilink_resolver import resolve_wikilinks

        repo = _FakeDocLinksRepo(
            {
                "Resolved": [_candidate("doc-1", "Resolved")],
                "Ambig": [_candidate("doc-2", "Ambig"), _candidate("doc-3", "Ambig")],
            }
        )
        result = resolve_wikilinks(
            conn=None,
            from_document_id="src",
            links=[("Resolved", "n1"), ("Ambig", "n2"), ("Ghost", "n3")],
            viewer_scope_profile_ids=["scope-A"],
            repository=repo,
        )
        assert result == [
            ("doc-1", "n1", "Resolved", "resolved"),
            (None, "n2", "Ambig", "ambiguous"),
            (None, "n3", "Ghost", "missing"),
        ]


# ---------------------------------------------------------------------------
# ACL 누설 방지 (R2 / 보안 보고서 회귀)
# ---------------------------------------------------------------------------

class TestACLLeakPrevention:
    def test_other_scope_document_not_leaked(self):
        """A 의 viewer 가 B 의 비공개 문서 제목으로 [[...]] 입력 → missing 반환."""
        from app.services.wikilink_resolver import resolve_wikilinks

        repo = _FakeDocLinksRepo(
            {"BSecret": [_candidate("doc-B", "BSecret", scope_profile_id="scope-B")]}
        )
        result = resolve_wikilinks(
            conn=None,
            from_document_id="src-A",
            links=[("BSecret", "n1")],
            viewer_scope_profile_ids=["scope-A"],
            repository=repo,
        )
        # B 의 문서가 후보로 들어가지 않아 missing 으로 처리
        assert result == [(None, "n1", "BSecret", "missing")]

    def test_same_title_different_scopes_resolved_within_scope(self):
        from app.services.wikilink_resolver import resolve_wikilinks

        repo = _FakeDocLinksRepo(
            {
                "Shared": [
                    _candidate("doc-A", "Shared", scope_profile_id="scope-A"),
                    _candidate("doc-B", "Shared", scope_profile_id="scope-B"),
                ]
            }
        )
        # A viewer
        a_result = resolve_wikilinks(
            conn=None,
            from_document_id="src",
            links=[("Shared", "n1")],
            viewer_scope_profile_ids=["scope-A"],
            repository=repo,
        )
        assert a_result == [("doc-A", "n1", "Shared", "resolved")]

        # B viewer
        b_result = resolve_wikilinks(
            conn=None,
            from_document_id="src",
            links=[("Shared", "n1")],
            viewer_scope_profile_ids=["scope-B"],
            repository=repo,
        )
        assert b_result == [("doc-B", "n1", "Shared", "resolved")]

    def test_empty_scope_returns_no_candidates(self):
        from app.services.wikilink_resolver import resolve_wikilinks

        repo = _FakeDocLinksRepo({"Foo": [_candidate("doc-1", "Foo")]})
        result = resolve_wikilinks(
            conn=None,
            from_document_id="src",
            links=[("Foo", "n1")],
            viewer_scope_profile_ids=[],
            repository=repo,
        )
        assert result == [(None, "n1", "Foo", "missing")]

    def test_none_scope_skips_filter_admin(self):
        """admin 호출 시 ``viewer_scope_profile_ids=None`` 이면 모든 후보 노출."""
        from app.services.wikilink_resolver import resolve_wikilinks

        repo = _FakeDocLinksRepo({"Foo": [_candidate("doc-X", "Foo", scope_profile_id="scope-Z")]})
        result = resolve_wikilinks(
            conn=None,
            from_document_id="src",
            links=[("Foo", "n1")],
            viewer_scope_profile_ids=None,
            repository=repo,
        )
        assert result == [("doc-X", "n1", "Foo", "resolved")]


# ---------------------------------------------------------------------------
# NFC 정규화
# ---------------------------------------------------------------------------

class TestNFCNormalization:
    def test_combined_vs_decomposed_korean(self):
        """한글 조합형 (NFD) vs 완성형 (NFC) 동일 문자열 매칭."""
        from app.services.wikilink_resolver import resolve_wikilinks, normalize_title

        # `한글` — NFC (완성형)
        nfc = "한글"
        # NFD (조합형) — 같은 문자열의 분해 형태
        import unicodedata
        nfd = unicodedata.normalize("NFD", nfc)
        assert nfc != nfd  # 바이트 다름 확인

        # repository 의 fixture 키는 normalize 후 정본 (NFC)
        repo = _FakeDocLinksRepo({nfc: [_candidate("doc-1", nfc)]})
        # 입력이 NFD 라도 매칭됨
        result = resolve_wikilinks(
            conn=None,
            from_document_id="src",
            links=[(nfd, "n1")],
            viewer_scope_profile_ids=["scope-A"],
            repository=repo,
        )
        assert result[0][0] == "doc-1"
        assert result[0][3] == "resolved"
        # 단, raw_text 는 입력 그대로 보존
        assert result[0][2] == nfd

    def test_normalize_title_strip_and_nfc(self):
        from app.services.wikilink_resolver import normalize_title
        assert normalize_title("  Foo  ") == "Foo"
        assert normalize_title("") == ""
        assert normalize_title(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 캐싱 — 같은 raw_text 가 여러 node 에서 등장 시 1회만 조회
# ---------------------------------------------------------------------------

class TestCandidateCache:
    def test_same_raw_text_queried_once(self):
        from app.services.wikilink_resolver import resolve_wikilinks

        repo = _FakeDocLinksRepo({"Foo": [_candidate("doc-1", "Foo")]})
        resolve_wikilinks(
            conn=None,
            from_document_id="src",
            links=[("Foo", "n1"), ("Foo", "n2"), ("Foo", "n3")],
            viewer_scope_profile_ids=["scope-A"],
            repository=repo,
        )
        # 3 번 등장했지만 repository 는 1회만 호출
        assert len(repo.calls) == 1
        assert repo.calls[0][0] == "Foo"

    def test_different_raw_text_queried_separately(self):
        from app.services.wikilink_resolver import resolve_wikilinks

        repo = _FakeDocLinksRepo({"A": [_candidate("a", "A")], "B": [_candidate("b", "B")]})
        resolve_wikilinks(
            conn=None,
            from_document_id="src",
            links=[("A", "n1"), ("B", "n2")],
            viewer_scope_profile_ids=["scope-A"],
            repository=repo,
        )
        assert len(repo.calls) == 2
        titles = sorted(c[0] for c in repo.calls)
        assert titles == ["A", "B"]
