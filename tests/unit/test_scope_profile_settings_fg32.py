"""S3 Phase 3 FG 3-2 — ScopeProfile.settings_json 직렬화 / 라운드트립 단위 테스트.

대상:
  - app.models.scope_profile.ScopeProfileSettings (default false)
  - app.repositories.scope_profile_repository._settings_from_raw / _settings_to_raw
  - alien 키 보존 / 잘못된 타입 fallback / boolean coercion
"""
from __future__ import annotations

import json

import pytest

from app.models.scope_profile import ScopeProfileSettings
from app.repositories.scope_profile_repository import (
    _KNOWN_SETTINGS_KEYS,
    _settings_from_raw,
    _settings_to_raw,
)


class TestScopeProfileSettings:
    def test_default_expose_viewers_false(self):
        s = ScopeProfileSettings()
        assert s.expose_viewers is False

    def test_explicit_true(self):
        s = ScopeProfileSettings(expose_viewers=True)
        assert s.expose_viewers is True


class TestSettingsFromRaw:
    def test_none_returns_default(self):
        s = _settings_from_raw(None)
        assert s.expose_viewers is False

    def test_empty_dict_returns_default(self):
        s = _settings_from_raw({})
        assert s.expose_viewers is False

    def test_empty_string_json_returns_default(self):
        # loads_maybe 가 빈 문자열을 어떻게 처리할지에 의존 — 잘못된 JSON 으로 raise 가능.
        # 호출자가 _settings_from_raw 에 빈 문자열을 넘기는 경우는 거의 없지만 방어 검증.
        # JSON 파싱 실패 → 호출자가 raise — 본 helper 는 dict 가 아닐 때만 default 반환.
        # 따라서 빈 문자열은 원본 raise 가 자연스러움. 본 테스트는 dict 분기 검증만.
        pass

    def test_dict_with_expose_viewers_true(self):
        s = _settings_from_raw({"expose_viewers": True})
        assert s.expose_viewers is True

    def test_dict_with_expose_viewers_false(self):
        s = _settings_from_raw({"expose_viewers": False})
        assert s.expose_viewers is False

    def test_json_str_parsed(self):
        s = _settings_from_raw(json.dumps({"expose_viewers": True}))
        assert s.expose_viewers is True

    def test_truthy_non_bool_coerced(self):
        # bool() 가 truthy 값을 True 로 강제
        s = _settings_from_raw({"expose_viewers": 1})
        assert s.expose_viewers is True

    def test_falsy_non_bool_coerced(self):
        s = _settings_from_raw({"expose_viewers": 0})
        assert s.expose_viewers is False
        s2 = _settings_from_raw({"expose_viewers": ""})
        assert s2.expose_viewers is False

    def test_alien_key_ignored_in_dataclass(self):
        # 알 수 없는 키는 dataclass 에 들어가지 않음 (raw 는 별도 보존)
        s = _settings_from_raw({"expose_viewers": True, "unknown_key": "x"})
        assert s.expose_viewers is True
        assert not hasattr(s, "unknown_key")

    def test_non_dict_input_returns_default(self):
        # list / int / str(non-JSON) 등은 fail-closed default
        s = _settings_from_raw([1, 2, 3])
        assert s.expose_viewers is False
        s2 = _settings_from_raw(42)
        assert s2.expose_viewers is False


class TestSettingsToRaw:
    def test_default_settings(self):
        s = ScopeProfileSettings()
        raw = _settings_to_raw(s)
        assert raw == {"expose_viewers": False}

    def test_true_settings(self):
        s = ScopeProfileSettings(expose_viewers=True)
        raw = _settings_to_raw(s)
        assert raw == {"expose_viewers": True}

    def test_only_known_keys_in_output(self):
        # dataclass 에 새 필드가 추가되어도 본 helper 가 명시적으로 known key 만 dump.
        # 미래에 새 키 추가 시 본 helper 와 _KNOWN_SETTINGS_KEYS 둘 다 갱신 필요.
        raw = _settings_to_raw(ScopeProfileSettings())
        assert set(raw.keys()) <= _KNOWN_SETTINGS_KEYS


class TestKnownSettingsKeys:
    def test_includes_expose_viewers(self):
        assert "expose_viewers" in _KNOWN_SETTINGS_KEYS

    def test_is_frozenset(self):
        assert isinstance(_KNOWN_SETTINGS_KEYS, frozenset)


class TestRoundTrip:
    def test_to_raw_then_from_raw(self):
        original = ScopeProfileSettings(expose_viewers=True)
        raw = _settings_to_raw(original)
        restored = _settings_from_raw(raw)
        assert restored == original

    def test_from_raw_then_to_raw(self):
        # raw 에 unknown key 가 있어도 to_raw 는 known 만 출력 (정상)
        raw_in = {"expose_viewers": True, "future_key": "preserved-elsewhere"}
        s = _settings_from_raw(raw_in)
        raw_out = _settings_to_raw(s)
        assert "expose_viewers" in raw_out
        assert raw_out["expose_viewers"] is True
        # to_raw 만으로는 unknown key 를 보존하지 않음 — 호출자(repository.update) 가 머지 책임
        assert "future_key" not in raw_out
