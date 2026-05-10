"""
Vault Import 상한값 단일 정본 — S3 Phase 2 FG 2-6.

task2-6.md §5 의 6 환경변수를 하나의 모듈에 모은다 (S2 ⑥ 하드코딩 금지).
zip 안전 처리 / markdown 변환 / 워커 타임아웃 모두 본 모듈을 import.

환경변수 부재 시 보수적 기본값. 운영 환경에서 override 시 `os.environ` 직접.
"""

from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


# zip 자체 크기 상한 (압축 해제 전 검증)
MAX_ZIP_BYTES = _int_env("VAULT_IMPORT_MAX_ZIP_BYTES", 100 * 1024 * 1024)

# 엔트리 수 상한
MAX_ENTRY_COUNT = _int_env("VAULT_IMPORT_MAX_ENTRY_COUNT", 10000)

# 개별 파일 압축 해제 후 크기 상한
MAX_FILE_BYTES = _int_env("VAULT_IMPORT_MAX_FILE_BYTES", 10 * 1024 * 1024)

# 총 압축 해제 크기 상한 (모든 파일 합계)
MAX_TOTAL_EXTRACTED_BYTES = _int_env(
    "VAULT_IMPORT_MAX_TOTAL_EXTRACTED_BYTES", 500 * 1024 * 1024,
)

# 압축 비율 상한 (file_size / compress_size 가 이 값 초과면 폭탄으로 간주)
MAX_COMPRESSION_RATIO = _int_env("VAULT_IMPORT_MAX_COMPRESSION_RATIO", 100)

# 워커 타임아웃 (초). 이 시간 초과 시 cancelled 처리.
WORKER_TIMEOUT_SEC = _int_env("VAULT_IMPORT_WORKER_TIMEOUT_SEC", 1800)


__all__ = [
    "MAX_ZIP_BYTES",
    "MAX_ENTRY_COUNT",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_EXTRACTED_BYTES",
    "MAX_COMPRESSION_RATIO",
    "WORKER_TIMEOUT_SEC",
]
