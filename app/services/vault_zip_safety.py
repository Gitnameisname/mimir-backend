"""
Vault Import — zip 안전 처리 유틸 — S3 Phase 2 FG 2-6.

task2-6.md §2.1 (3) / §4 Step 1 의 공격 방어 정책 정본.

방어 대상
---------
1. **zip bomb**:
   - zip 자체 크기 상한 (MAX_ZIP_BYTES)
   - 엔트리 수 상한 (MAX_ENTRY_COUNT)
   - 개별 파일 압축 해제 크기 상한 (MAX_FILE_BYTES)
   - 총 압축 해제 크기 상한 (MAX_TOTAL_EXTRACTED_BYTES)
   - **압축 비율 상한** (MAX_COMPRESSION_RATIO) — `file_size / compress_size` 가 임계 초과 시 거부

2. **path traversal**:
   - `..` 포함 / 절대경로 / 정규화 후 root 벗어나는 경로 거부
   - null byte (`\\x00`) 포함 거부

3. **symlink entry**: zip 의 external_attr 상위 비트로 symlink 식별 → 거부

4. **파일명 중복**: 같은 정규화 경로가 두 번 등장하면 두 번째 거부 (덮어쓰기 공격)

설계
----
- 입력은 ``ZipFile`` 또는 byte stream. 본 유틸은 메모리에 모든 파일을 로드하지 않고
  ``ZipInfo`` 리스트 + 옵션 streaming 추출.
- ``RejectionReason`` (str enum) 으로 거부 이유 코드화 — frontend 가 사용자 메시지 매핑.
"""

from __future__ import annotations

import logging
import os
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Iterator, Literal, Optional

from app.services import vault_import_config as cfg

logger = logging.getLogger(__name__)


RejectionReason = Literal[
    "zip_bomb",          # 압축 비율 / 총 크기 / 개별 크기 초과
    "entry_limit",       # 엔트리 수 초과
    "file_too_large",    # 개별 파일 크기 초과
    "path_traversal",    # `..` / 절대경로 / null byte / 정규화 후 root 벗어남
    "symlink_entry",     # symlink 엔트리
    "duplicate_path",    # 같은 정규화 경로 중복
    "zip_too_large",     # zip 자체 크기 초과
    "invalid_zip",       # 손상된 zip
    "encrypted_entry",   # 암호화된 엔트리 (미지원)
]


class VaultZipRejected(ValueError):
    """zip 안전 검증 실패 — 사용자 친화 메시지 + 코드."""

    def __init__(self, reason: RejectionReason, detail: str = "") -> None:
        self.reason: RejectionReason = reason
        self.detail = detail
        super().__init__(f"[{reason}] {detail}")


@dataclass
class SafeEntry:
    """검증 통과한 단일 엔트리. 호출자는 이 dataclass 만 다룬다."""

    name: str  # 정규화된 상대 경로 (예: "notes/foo.md")
    file_size: int
    compress_size: int
    is_dir: bool


@dataclass
class ZipInspectionResult:
    entries: list[SafeEntry] = field(default_factory=list)
    total_extracted_bytes: int = 0
    rejected: list[tuple[str, RejectionReason]] = field(default_factory=list)  # 엔트리별 거부 (참고)


# ---------------------------------------------------------------------------
# 사이즈 / 엔트리 수 사전 검증
# ---------------------------------------------------------------------------

def check_zip_byte_size(zip_byte_size: int) -> None:
    """zip 파일 자체 크기 상한 검증 (압축 해제 전)."""
    if zip_byte_size > cfg.MAX_ZIP_BYTES:
        raise VaultZipRejected(
            "zip_too_large",
            f"zip 크기 {zip_byte_size:,} 바이트 가 상한 {cfg.MAX_ZIP_BYTES:,} 을 초과했습니다.",
        )


# ---------------------------------------------------------------------------
# 엔트리별 검증
# ---------------------------------------------------------------------------

def _normalize_path(raw_name: str) -> Optional[str]:
    """zip 엔트리 이름을 안전 정규화. None 이면 거부 사유 발견.

    규칙:
      - null byte 포함 거부
      - 절대경로 거부 (앞에 `/`)
      - `..` 세그먼트 포함 거부
      - 정규화 후 root 벗어나는 경로 거부
      - NFC 정규화 (한글 조합형/완성형 흡수 — task2-3 R-04 와 같은 정신)
    """
    if not raw_name:
        return None
    if "\x00" in raw_name:
        return None
    # 절대경로 (POSIX 또는 Windows)
    if raw_name.startswith("/") or raw_name.startswith("\\"):
        return None
    if len(raw_name) >= 2 and raw_name[1] == ":":  # "C:\..."
        return None

    # NFC 정규화 (유니코드 정규화 공격 흡수)
    name = unicodedata.normalize("NFC", raw_name)

    # 슬래시 정규화 (Windows zip 일 때 \ → /)
    name = name.replace("\\", "/")

    # 세그먼트별 검증
    segments = [s for s in name.split("/") if s != ""]
    for seg in segments:
        if seg == "..":
            return None
        if seg == ".":
            return None  # current-dir 세그먼트도 거부 (단순화)

    # 재조합 — 디렉토리 엔트리는 끝에 `/` 보존
    is_dir = name.endswith("/")
    normalized = "/".join(segments)
    if is_dir and normalized:
        normalized += "/"
    if not normalized:
        return None

    return normalized


def _is_symlink_entry(info: zipfile.ZipInfo) -> bool:
    """zip external_attr 상위 16비트의 unix mode 에서 symlink 비트 검출.

    UNIX symlink: file mode & 0o170000 == 0o120000.
    Windows-only zip 은 external_attr 가 다른 의미 — false negative 가능하나
    안전 측면에서 "확실히 symlink" 만 거부.
    """
    if info.create_system != 3:  # 3 = unix
        return False
    mode = info.external_attr >> 16
    return (mode & 0o170000) == 0o120000


def inspect_entry(
    info: zipfile.ZipInfo,
) -> tuple[Optional[SafeEntry], Optional[RejectionReason]]:
    """단일 엔트리 검증. (entry, None) 또는 (None, reason).

    엔트리 단위 거부는 호출자가 (a) 강제 fail, (b) skip & report 중 선택.
    """
    # symlink 거부 (강제)
    if _is_symlink_entry(info):
        return None, "symlink_entry"

    # 암호화된 엔트리 거부 (압축 해제 안전성 미보장)
    # ZipInfo.flag_bits & 0x1 == 1 이면 암호화
    if info.flag_bits & 0x1:
        return None, "encrypted_entry"

    # 개별 파일 크기 (압축 해제 후) — 디렉토리는 크기 0 정상
    if info.file_size > cfg.MAX_FILE_BYTES:
        return None, "file_too_large"

    # 압축 비율 — 디렉토리 / 빈 파일 (compress_size 0) 은 검사 skip
    # MAX_COMPRESSION_RATIO 가 100 이고 file_size > MAX_FILE_BYTES * compress_size 면 폭탄
    if info.compress_size > 0:
        if info.file_size > info.compress_size * cfg.MAX_COMPRESSION_RATIO:
            return None, "zip_bomb"

    # 경로 정규화
    normalized = _normalize_path(info.filename)
    if normalized is None:
        return None, "path_traversal"

    return SafeEntry(
        name=normalized,
        file_size=info.file_size,
        compress_size=info.compress_size,
        is_dir=normalized.endswith("/"),
    ), None


def inspect_zip(zf: zipfile.ZipFile) -> ZipInspectionResult:
    """zip 안의 모든 엔트리를 검증하고 통과한 항목 + 거부 항목 list 반환.

    한 항목이라도 강제 거부 사유 (zip_bomb / path_traversal / symlink / encrypted) 면
    즉시 VaultZipRejected. 그 외 (file_too_large 등) 도 본 함수는 강제 거부 (1차 원칙 —
    한 파일이 위반하면 zip 전체 거부 — 보안 우선).

    엔트리 수 상한 / 총 압축 해제 크기 상한도 본 함수에서 검증.
    """
    infos = zf.infolist()
    if len(infos) > cfg.MAX_ENTRY_COUNT:
        raise VaultZipRejected(
            "entry_limit",
            f"엔트리 수 {len(infos)} 가 상한 {cfg.MAX_ENTRY_COUNT} 을 초과했습니다.",
        )

    result = ZipInspectionResult()
    seen_paths: set[str] = set()
    total = 0

    for info in infos:
        entry, reason = inspect_entry(info)
        if reason is not None:
            # 1차 정책 — 한 파일이라도 위반 시 zip 전체 거부
            raise VaultZipRejected(reason, f"엔트리 '{info.filename}' 거부됨")
        assert entry is not None  # for type checker

        if entry.is_dir:
            # 디렉토리 자체는 카운트만 — 중복 / 경로 검증은 통과한 것
            continue

        # 중복 경로 검증
        if entry.name in seen_paths:
            raise VaultZipRejected(
                "duplicate_path",
                f"같은 경로 '{entry.name}' 가 zip 안에 두 번 이상 등장합니다.",
            )
        seen_paths.add(entry.name)

        # 총 크기 누적
        total += entry.file_size
        if total > cfg.MAX_TOTAL_EXTRACTED_BYTES:
            raise VaultZipRejected(
                "zip_bomb",
                f"총 압축 해제 크기 {total:,} 가 상한 "
                f"{cfg.MAX_TOTAL_EXTRACTED_BYTES:,} 을 초과했습니다.",
            )
        result.entries.append(entry)

    result.total_extracted_bytes = total
    return result


def safe_open_zip(path: str) -> zipfile.ZipFile:
    """zip 파일을 안전 모드로 연다. 손상된 zip 은 invalid_zip 으로 거부."""
    if not os.path.exists(path):
        raise VaultZipRejected("invalid_zip", f"파일이 존재하지 않습니다: {path}")
    try:
        return zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile as e:
        raise VaultZipRejected("invalid_zip", f"손상된 zip 파일: {e}") from e


def iter_safe_files(
    zf: zipfile.ZipFile,
    inspection: ZipInspectionResult,
) -> Iterator[tuple[SafeEntry, bytes]]:
    """검증된 엔트리만 순회하며 압축 해제. 호출자는 markdown 파싱에만 집중.

    압축 해제는 ``ZipFile.read`` — Python 표준 zip 의 read 는 file_size 를
    이미 검증한 만큼만 안전하게 읽는다. SafeEntry 가 file_size 통과를 보증하므로
    추가 streaming 검증은 불필요.
    """
    for entry in inspection.entries:
        if entry.is_dir:
            continue
        # file_size 가 0 인 빈 파일은 skip
        if entry.file_size == 0:
            yield entry, b""
            continue
        try:
            data = zf.read(entry.name)
        except KeyError:
            # NFC 정규화로 zip 내부 이름과 어긋나면 fallback (드물지만 가능)
            continue
        yield entry, data


__all__ = [
    "RejectionReason",
    "SafeEntry",
    "VaultZipRejected",
    "ZipInspectionResult",
    "check_zip_byte_size",
    "inspect_entry",
    "inspect_zip",
    "iter_safe_files",
    "safe_open_zip",
]
