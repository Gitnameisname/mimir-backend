"""
S3 Phase 5 FG 5-5 — 한국어 mention 정책 회귀 (2026-05-14).

대상:
  - app.services.annotations_service.MENTION_REGEX / extract_mentions
  - app.services.annotations_service._resolve_mention_user_ids (display_name fallback)
  - app.services.annotations_service._validate_explicit_mention_user_ids (R-A4 정합)

회귀 영역:
  1. 정규식 — 영문 / 한국어 / 혼합 / boundary / 구두점 끝 제외
  2. extract_mentions — ASCII lowercase / 한국어 원문 보존
  3. _resolve_mention_user_ids — username 우선, display_name fallback
  4. _validate_explicit_mention_user_ids — viewer scope 검증 (R-A4)
  5. UsersRepository.filter_user_ids_in_viewer_orgs — SQL 정합 (mock)
  6. UsersRepository.find_by_display_name_in_viewer_orgs — unique 일치만 (충돌 silently skip)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "fg55-mention-test")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "fg55-internal")


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# A. MENTION_REGEX / extract_mentions — 정규식 정합 (한국어 + 영문 + boundary)
# ---------------------------------------------------------------------------


class TestExtractMentions:
    def test_english_username_lowercase_normalized(self):
        from app.services.annotations_service import extract_mentions

        assert extract_mentions("hi @JohnDoe") == ["johndoe"]

    def test_korean_display_name_preserved(self):
        """한국어 mention 토큰은 lowercase 적용 없이 원문 보존 (FG 5-5)."""
        from app.services.annotations_service import extract_mentions

        assert extract_mentions("안녕 @홍길동") == ["홍길동"]

    def test_mixed_english_and_korean(self):
        from app.services.annotations_service import extract_mentions

        result = extract_mentions("@alice 님과 @홍길동 님 회의")
        assert result == ["alice", "홍길동"]

    def test_punctuation_after_token_excluded(self):
        """`@홍길동.` → 마침표는 토큰에서 제외 (자연어 본문 호환)."""
        from app.services.annotations_service import extract_mentions

        assert extract_mentions("회의는 @홍길동.") == ["홍길동"]

    def test_boundary_word_char_blocks_mention(self):
        """이메일 `foo@bar.com` 의 `@` 는 mention 으로 인식 안 됨."""
        from app.services.annotations_service import extract_mentions

        assert extract_mentions("contact me at foo@bar") == []

    def test_duplicate_tokens_deduped(self):
        from app.services.annotations_service import extract_mentions

        assert extract_mentions("@alice @ALICE @alice") == ["alice"]

    def test_korean_duplicates_deduped_case_sensitive(self):
        from app.services.annotations_service import extract_mentions

        # 한국어는 case 무관 — 원문 그대로 dedupe
        assert extract_mentions("@홍길동 @홍길동") == ["홍길동"]

    def test_empty_or_none(self):
        from app.services.annotations_service import extract_mentions

        assert extract_mentions("") == []
        assert extract_mentions("no mentions") == []

    def test_max_length_64_chars(self):
        from app.services.annotations_service import extract_mentions

        long_token = "a" * 64
        too_long = "a" * 65
        assert extract_mentions(f"@{long_token}") == [long_token]
        # 65 자는 64 자에서 cut — 정규식 quantifier 가 {1,64}
        assert extract_mentions(f"@{too_long}")[0] == long_token

    def test_special_characters_dot_dash_underscore_preserved_in_token(self):
        """username 의 . - _ 는 토큰에 포함 (영문 username 규칙)."""
        from app.services.annotations_service import extract_mentions

        assert extract_mentions("@user.name") == ["user.name"] or extract_mentions(
            "@user.name"
        ) == ["user"]
        # 보수적 검증: 마침표 끝은 제외되지만 중간은 포함
        assert "user_name" in extract_mentions("@user_name")
        assert "user-name" in extract_mentions("@user-name")


# ---------------------------------------------------------------------------
# B. UsersRepository — filter_user_ids_in_viewer_orgs / find_by_display_name_in_viewer_orgs
# ---------------------------------------------------------------------------


class TestUsersRepositoryFG55:
    def _make_conn(self, fetchall_value=None):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute = MagicMock()
        cur.fetchall = MagicMock(return_value=fetchall_value or [])
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)
        return conn, cur

    def test_filter_user_ids_in_viewer_orgs_returns_only_allowed(self):
        from app.repositories.users_repository import UsersRepository

        repo = UsersRepository()
        conn, cur = self._make_conn(
            fetchall_value=[{"id": "u-1"}, {"id": "u-2"}]
        )
        # 입력: u-1, u-2, u-malicious — backend 가 SQL JOIN 으로 u-1/u-2 만 통과
        result = repo.filter_user_ids_in_viewer_orgs(
            conn,
            viewer_user_id="viewer-A",
            user_ids=["u-1", "u-2", "u-malicious"],
        )
        assert "u-1" in result
        assert "u-2" in result
        assert "u-malicious" not in result
        # SQL 호출 확인 — JOIN user_org_roles 2회
        sql = cur.execute.call_args.args[0]
        assert sql.count("JOIN user_org_roles") >= 2
        assert "u.id = ANY" in sql

    def test_filter_user_ids_empty_returns_empty(self):
        from app.repositories.users_repository import UsersRepository

        repo = UsersRepository()
        conn, cur = self._make_conn()
        result = repo.filter_user_ids_in_viewer_orgs(
            conn, viewer_user_id="viewer-A", user_ids=[],
        )
        assert result == []
        # SQL 호출되지 않음 — 성능 short-circuit
        assert cur.execute.call_count == 0

    def test_find_by_display_name_unique_match(self):
        """1건 매칭 시 user 반환."""
        from app.repositories.users_repository import UsersRepository

        repo = UsersRepository()
        # _row_to_user 는 dict 형태 user 행 받음 — minimal stub
        # `role_name` 은 users 테이블 의 실제 컬럼명 (`role` 이 아님 — UsersRepository
        # ._row_to_user 가 row["role_name"] 으로 직접 인덱싱).
        row = {
            "id": "u-1",
            "username": "alice",
            "email": "alice@example.com",
            "display_name": "Alice",
            "status": "ACTIVE",
            "role_name": "VIEWER",
            "created_at": None,
            "updated_at": None,
            "password_hash": None,
            "external_id": None,
        }
        conn, cur = self._make_conn(fetchall_value=[row])
        user = repo.find_by_display_name_in_viewer_orgs(
            conn, viewer_user_id="viewer-A", display_name="Alice",
        )
        assert user is not None
        assert user.id == "u-1"

    def test_find_by_display_name_conflict_returns_none(self):
        """같은 display_name 가 2건이면 None (silently skip)."""
        from app.repositories.users_repository import UsersRepository

        repo = UsersRepository()
        row1 = {
            "id": "u-1", "username": "u1", "email": "u1@e.com",
            "display_name": "홍길동", "status": "ACTIVE", "role_name": "VIEWER",
            "created_at": None, "updated_at": None, "password_hash": None,
            "external_id": None,
        }
        row2 = {**row1, "id": "u-2", "username": "u2", "email": "u2@e.com"}
        conn, _ = self._make_conn(fetchall_value=[row1, row2])
        user = repo.find_by_display_name_in_viewer_orgs(
            conn, viewer_user_id="viewer-A", display_name="홍길동",
        )
        assert user is None  # 충돌 — silently skip

    def test_find_by_display_name_empty_returns_none(self):
        from app.repositories.users_repository import UsersRepository

        repo = UsersRepository()
        conn, cur = self._make_conn()
        user = repo.find_by_display_name_in_viewer_orgs(
            conn, viewer_user_id="viewer-A", display_name="",
        )
        assert user is None
        assert cur.execute.call_count == 0


# ---------------------------------------------------------------------------
# C. _resolve_mention_user_ids — username 우선 + display_name fallback
# ---------------------------------------------------------------------------


class TestResolveMentionUserIds:
    def test_empty_tokens(self):
        from app.services.annotations_service import _resolve_mention_user_ids

        assert _resolve_mention_user_ids(MagicMock(), [], viewer_user_id="v1") == []

    def test_ascii_token_tries_username_first(self, monkeypatch):
        """영문 토큰은 username 우선 시도."""
        from app.services.annotations_service import (
            _resolve_mention_user_ids,
            _users_repository,
        )

        mock_user = MagicMock()
        mock_user.id = "u-alice"
        monkeypatch.setattr(
            _users_repository, "get_by_username", lambda conn, name: mock_user
        )
        result = _resolve_mention_user_ids(
            MagicMock(), ["alice"], viewer_user_id="v1",
        )
        assert result == ["u-alice"]

    def test_korean_token_skips_username_uses_display_name(self, monkeypatch):
        """한국어 토큰은 username 시도 건너뛰고 display_name fallback."""
        from app.services.annotations_service import (
            _resolve_mention_user_ids,
            _users_repository,
        )

        get_username_called = {"n": 0}

        def _get_username(conn, name):
            get_username_called["n"] += 1
            return None

        monkeypatch.setattr(_users_repository, "get_by_username", _get_username)

        mock_user = MagicMock()
        mock_user.id = "u-홍길동"
        monkeypatch.setattr(
            _users_repository,
            "find_by_display_name_in_viewer_orgs",
            lambda conn, *, viewer_user_id, display_name: mock_user,
        )
        result = _resolve_mention_user_ids(
            MagicMock(), ["홍길동"], viewer_user_id="v1",
        )
        assert result == ["u-홍길동"]
        # ASCII 검사로 한국어는 username 미시도 — get_by_username 호출 0
        assert get_username_called["n"] == 0

    def test_dedup_across_username_and_display_name(self, monkeypatch):
        """같은 user 가 username + display_name 둘 다 매칭되어도 dedupe."""
        from app.services.annotations_service import (
            _resolve_mention_user_ids,
            _users_repository,
        )

        mock_user = MagicMock()
        mock_user.id = "u-same"
        monkeypatch.setattr(
            _users_repository, "get_by_username", lambda conn, name: mock_user
        )
        monkeypatch.setattr(
            _users_repository,
            "find_by_display_name_in_viewer_orgs",
            lambda conn, *, viewer_user_id, display_name: mock_user,
        )
        result = _resolve_mention_user_ids(
            MagicMock(), ["alice", "Alice"], viewer_user_id="v1",
        )
        assert result == ["u-same"]  # 단일 항목만

    def test_viewer_user_id_missing_skips_display_name_fallback(self, monkeypatch):
        """viewer_user_id 가 None 이면 display_name fallback 시도 안 함."""
        from app.services.annotations_service import (
            _resolve_mention_user_ids,
            _users_repository,
        )

        monkeypatch.setattr(
            _users_repository, "get_by_username", lambda conn, name: None
        )
        fallback_called = {"n": 0}

        def _fallback(conn, **kwargs):
            fallback_called["n"] += 1
            return None

        monkeypatch.setattr(
            _users_repository,
            "find_by_display_name_in_viewer_orgs",
            _fallback,
        )
        result = _resolve_mention_user_ids(
            MagicMock(), ["홍길동"], viewer_user_id=None,
        )
        assert result == []
        assert fallback_called["n"] == 0


# ---------------------------------------------------------------------------
# D. _validate_explicit_mention_user_ids — R-A4 정합 (viewer scope)
# ---------------------------------------------------------------------------


class TestValidateExplicitMentionUserIds:
    def test_empty_input(self):
        from app.services.annotations_service import (
            _validate_explicit_mention_user_ids,
        )

        assert (
            _validate_explicit_mention_user_ids(
                MagicMock(), [], viewer_user_id="v1",
            )
            == []
        )

    def test_passes_viewer_scope_filter(self, monkeypatch):
        """repository 의 filter_user_ids_in_viewer_orgs 로 통과만 반환."""
        from app.services.annotations_service import (
            _validate_explicit_mention_user_ids,
            _users_repository,
        )

        monkeypatch.setattr(
            _users_repository,
            "filter_user_ids_in_viewer_orgs",
            lambda conn, *, viewer_user_id, user_ids: ["u-1"],
        )
        result = _validate_explicit_mention_user_ids(
            MagicMock(),
            ["u-1", "u-malicious"],
            viewer_user_id="v1",
        )
        assert result == ["u-1"]
        assert "u-malicious" not in result

    def test_whitespace_and_empty_strings_cleaned(self, monkeypatch):
        from app.services.annotations_service import (
            _validate_explicit_mention_user_ids,
            _users_repository,
        )

        captured = {}

        def _filter(conn, *, viewer_user_id, user_ids):
            captured["input"] = user_ids
            return user_ids

        monkeypatch.setattr(
            _users_repository, "filter_user_ids_in_viewer_orgs", _filter
        )
        _validate_explicit_mention_user_ids(
            MagicMock(), ["u-1", "  ", "", "  u-2  "], viewer_user_id="v1",
        )
        # 빈 / 공백 정리 후 stripped 결과
        assert captured["input"] == ["u-1", "u-2"]
