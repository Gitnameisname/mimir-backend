"""
S3 Phase 2 FG 2-6 — vault_zip_safety 공격 케이스 단위 회귀.

task2-6.md §4 Step 1 의 10+ 공격 시나리오:
  - 42.zip (재귀 zip bomb 흉내 — 큰 file_size + 작은 compress_size)
  - 단일 큰 파일 (개별 file_size 초과)
  - 엔트리 수 초과
  - path traversal `../../etc/passwd`
  - absolute path `/etc/passwd`
  - symlink entry
  - null byte 파일명
  - 중복 파일명
  - 유니코드 정규화 공격
  - 정상 vault (파일 N + 폴더 M)
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 헬퍼 — 메모리 zip 생성
# ---------------------------------------------------------------------------

def _build_zip(entries: list[tuple[str, bytes]]) -> zipfile.ZipFile:
    """(name, data) 리스트로 메모리 zip 생성."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


def _build_zip_with_info(infos_data: list[tuple[zipfile.ZipInfo, bytes]]) -> zipfile.ZipFile:
    """ZipInfo 직접 조작 (external_attr 등 설정용)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for info, data in infos_data:
            zf.writestr(info, data)
    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


# ---------------------------------------------------------------------------
# 1. 정상 vault
# ---------------------------------------------------------------------------

class TestNormalVault:
    def test_typical_vault_passes(self):
        from app.services.vault_zip_safety import inspect_zip

        zf = _build_zip([
            ("notes/foo.md", b"# Foo\n"),
            ("notes/bar.md", b"# Bar\n"),
            ("daily/2026-05-10.md", b"# Today\n"),
        ])
        result = inspect_zip(zf)
        # 디렉토리 자동 생성은 zipfile 이 안 함 — 파일만 카운트
        names = [e.name for e in result.entries]
        assert "notes/foo.md" in names
        assert "notes/bar.md" in names
        assert "daily/2026-05-10.md" in names
        assert len(result.entries) == 3
        assert result.total_extracted_bytes == sum(e.file_size for e in result.entries)


# ---------------------------------------------------------------------------
# 2. zip bomb — 압축 비율 초과
# ---------------------------------------------------------------------------

class TestZipBomb:
    def test_compression_ratio_exceeds_limit(self):
        """단일 파일의 file_size / compress_size 가 100:1 초과 → zip_bomb."""
        from app.services.vault_zip_safety import inspect_zip, VaultZipRejected

        # 1MB 의 0 으로 채운 파일은 deflate 후 ~ 1KB 미만 → 비율 1000:1 +
        big_zero_data = b"\x00" * (1 * 1024 * 1024)
        zf = _build_zip([("payload.bin", big_zero_data)])

        with pytest.raises(VaultZipRejected) as ei:
            inspect_zip(zf)
        assert ei.value.reason == "zip_bomb"

    def test_total_extracted_size_exceeds_limit(self):
        """누적 file_size 가 MAX_TOTAL_EXTRACTED_BYTES 초과 → zip_bomb."""
        from app.services.vault_zip_safety import inspect_zip, VaultZipRejected
        from app.services import vault_import_config

        # 작은 한도 monkeypatch — 3MB
        with patch.object(vault_import_config, "MAX_TOTAL_EXTRACTED_BYTES", 3 * 1024 * 1024), \
             patch.object(vault_import_config, "MAX_FILE_BYTES", 2 * 1024 * 1024), \
             patch.object(vault_import_config, "MAX_COMPRESSION_RATIO", 100000):
            # 2 MB 파일 두 개 — 압축 비 통과, 총 4 MB 초과
            data = (b"abcd" * 200000)  # 0.8 MB 정도, 무압축 가까움
            data_2mb = data * 3  # ~2.4 MB
            zf = _build_zip([
                ("a.bin", data_2mb[:1500000]),
                ("b.bin", data_2mb[:1500000]),
                ("c.bin", data_2mb[:1500000]),
            ])
            with pytest.raises(VaultZipRejected) as ei:
                inspect_zip(zf)
            assert ei.value.reason == "zip_bomb"


# ---------------------------------------------------------------------------
# 3. 개별 파일 크기 초과
# ---------------------------------------------------------------------------

class TestFileTooLarge:
    def test_single_file_exceeds_limit(self):
        from app.services.vault_zip_safety import inspect_zip, VaultZipRejected
        from app.services import vault_import_config

        with patch.object(vault_import_config, "MAX_FILE_BYTES", 100):
            zf = _build_zip([("big.md", b"x" * 200)])  # 200 > 100
            with pytest.raises(VaultZipRejected) as ei:
                inspect_zip(zf)
            assert ei.value.reason == "file_too_large"


# ---------------------------------------------------------------------------
# 4. 엔트리 수 초과
# ---------------------------------------------------------------------------

class TestEntryLimit:
    def test_entry_count_exceeds(self):
        from app.services.vault_zip_safety import inspect_zip, VaultZipRejected
        from app.services import vault_import_config

        with patch.object(vault_import_config, "MAX_ENTRY_COUNT", 3):
            zf = _build_zip([
                (f"f{i}.md", b"x") for i in range(5)
            ])
            with pytest.raises(VaultZipRejected) as ei:
                inspect_zip(zf)
            assert ei.value.reason == "entry_limit"


# ---------------------------------------------------------------------------
# 5. path traversal
# ---------------------------------------------------------------------------

class TestPathTraversal:
    def test_dot_dot_segment_rejected(self):
        from app.services.vault_zip_safety import inspect_zip, VaultZipRejected

        zf = _build_zip([("../../etc/passwd", b"hax")])
        with pytest.raises(VaultZipRejected) as ei:
            inspect_zip(zf)
        assert ei.value.reason == "path_traversal"

    def test_absolute_unix_path_rejected(self):
        from app.services.vault_zip_safety import inspect_zip, VaultZipRejected

        zf = _build_zip([("/etc/passwd", b"hax")])
        with pytest.raises(VaultZipRejected) as ei:
            inspect_zip(zf)
        assert ei.value.reason == "path_traversal"

    def test_windows_drive_path_rejected(self):
        from app.services.vault_zip_safety import inspect_zip, VaultZipRejected

        zf = _build_zip([("C:\\Windows\\System32\\bad.txt", b"hax")])
        with pytest.raises(VaultZipRejected) as ei:
            inspect_zip(zf)
        assert ei.value.reason == "path_traversal"

    def test_null_byte_rejected_at_normalize(self):
        """Python ZipInfo.__init__ 이 null byte 를 자동 truncate 하므로 외부 zip 으로
        들어올 가능성은 낮지만, _normalize_path 자체는 defense-in-depth 로 거부 보장.
        """
        from app.services.vault_zip_safety import _normalize_path

        assert _normalize_path("evil\x00name.md") is None
        assert _normalize_path("\x00leading.md") is None


# ---------------------------------------------------------------------------
# 6. symlink entry
# ---------------------------------------------------------------------------

class TestSymlink:
    def test_symlink_entry_rejected(self):
        from app.services.vault_zip_safety import inspect_entry
        import zipfile as zf_mod

        # unix symlink: create_system=3, mode 0o120000
        info = zf_mod.ZipInfo(filename="link.md")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)  # symlink + perms
        entry, reason = inspect_entry(info)
        assert reason == "symlink_entry"


# ---------------------------------------------------------------------------
# 7. encrypted entry
# ---------------------------------------------------------------------------

class TestEncrypted:
    def test_encrypted_entry_rejected(self):
        from app.services.vault_zip_safety import inspect_entry
        import zipfile as zf_mod

        info = zf_mod.ZipInfo(filename="secret.md")
        info.flag_bits = 0x1  # 암호화 비트
        entry, reason = inspect_entry(info)
        assert reason == "encrypted_entry"


# ---------------------------------------------------------------------------
# 8. 중복 파일명
# ---------------------------------------------------------------------------

class TestDuplicate:
    def test_duplicate_path_rejected(self):
        from app.services.vault_zip_safety import inspect_zip, VaultZipRejected
        import zipfile as zf_mod

        # 같은 정규화된 경로를 두 번 — ZipFile.writestr 는 동일 이름 허용 (덮어쓰기 공격)
        buf = io.BytesIO()
        with zf_mod.ZipFile(buf, "w", zf_mod.ZIP_DEFLATED) as zf:
            zf.writestr("notes/foo.md", b"original")
            zf.writestr("notes/foo.md", b"hijacked")
        buf.seek(0)
        zf2 = zf_mod.ZipFile(buf, "r")

        with pytest.raises(VaultZipRejected) as ei:
            inspect_zip(zf2)
        assert ei.value.reason == "duplicate_path"


# ---------------------------------------------------------------------------
# 9. 유니코드 정규화 — NFC 후 동일 경로면 중복 처리
# ---------------------------------------------------------------------------

class TestUnicodeNormalization:
    def test_nfd_and_nfc_collide_as_duplicate(self):
        """한글 NFD/NFC 가 정규화 후 같은 경로 → 중복 거부."""
        from app.services.vault_zip_safety import inspect_zip, VaultZipRejected
        import unicodedata
        import zipfile as zf_mod

        nfc_name = "한글/노트.md"
        nfd_name = unicodedata.normalize("NFD", nfc_name)
        assert nfc_name != nfd_name

        buf = io.BytesIO()
        with zf_mod.ZipFile(buf, "w", zf_mod.ZIP_DEFLATED) as zf:
            zf.writestr(nfc_name, b"a")
            zf.writestr(nfd_name, b"b")
        buf.seek(0)
        zf2 = zf_mod.ZipFile(buf, "r")

        with pytest.raises(VaultZipRejected) as ei:
            inspect_zip(zf2)
        assert ei.value.reason == "duplicate_path"

    def test_korean_filename_passes(self):
        from app.services.vault_zip_safety import inspect_zip

        zf = _build_zip([("한글/노트.md", "# 안녕".encode("utf-8"))])
        result = inspect_zip(zf)
        assert len(result.entries) == 1
        # 정규화된 NFC 이름
        assert result.entries[0].name == "한글/노트.md"


# ---------------------------------------------------------------------------
# 10. zip 자체 크기 / 손상된 zip
# ---------------------------------------------------------------------------

class TestZipMetadata:
    def test_check_zip_byte_size_under_limit(self):
        from app.services.vault_zip_safety import check_zip_byte_size
        check_zip_byte_size(1024)  # 정상

    def test_check_zip_byte_size_over_limit(self):
        from app.services.vault_zip_safety import check_zip_byte_size, VaultZipRejected
        from app.services import vault_import_config

        with patch.object(vault_import_config, "MAX_ZIP_BYTES", 1024):
            with pytest.raises(VaultZipRejected) as ei:
                check_zip_byte_size(2048)
            assert ei.value.reason == "zip_too_large"

    def test_invalid_zip_file(self, tmp_path):
        from app.services.vault_zip_safety import safe_open_zip, VaultZipRejected

        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"this is not a zip")
        with pytest.raises(VaultZipRejected) as ei:
            safe_open_zip(str(bad))
        assert ei.value.reason == "invalid_zip"

    def test_missing_zip_file(self, tmp_path):
        from app.services.vault_zip_safety import safe_open_zip, VaultZipRejected

        with pytest.raises(VaultZipRejected) as ei:
            safe_open_zip(str(tmp_path / "nonexistent.zip"))
        assert ei.value.reason == "invalid_zip"


# ---------------------------------------------------------------------------
# 11. iter_safe_files — 정상 추출
# ---------------------------------------------------------------------------

class TestIterSafeFiles:
    def test_iter_extracts_files(self):
        from app.services.vault_zip_safety import inspect_zip, iter_safe_files

        zf = _build_zip([
            ("notes/foo.md", b"# Foo\n"),
            ("notes/bar.md", b"# Bar"),
        ])
        result = inspect_zip(zf)
        extracted = {entry.name: data for entry, data in iter_safe_files(zf, result)}
        assert extracted == {
            "notes/foo.md": b"# Foo\n",
            "notes/bar.md": b"# Bar",
        }

    def test_iter_empty_file(self):
        from app.services.vault_zip_safety import inspect_zip, iter_safe_files

        zf = _build_zip([("empty.md", b"")])
        result = inspect_zip(zf)
        items = list(iter_safe_files(zf, result))
        assert len(items) == 1
        assert items[0][1] == b""
