"""
Vault Import Service — S3 Phase 2 FG 2-6.

업로드된 옵시디언 zip → markdown 파싱 → ProseMirror 변환 → documents 생성 + 파생 동기화.

본 세션 1차 종결 범위 (task2-6.md §4 Step 4):
  - zip 안전 검증 → markdown 추출 → ProseMirror 변환 → PII 스캔 → report 작성
  - **status 진행 추적** (pending → running → succeeded/failed/cancelled)
  - **documents 실제 생성**: 본 세션 1차는 dry-run 모드로 정상 흐름 검증 + report.preview 만 작성.
    실제 documents/folders/links 생성 통합은 별 라운드 (의존: documents_service.create + draft_service.save_draft + 폴더 트리 재현 — 큰 통합 작업).

설계 원칙:
  - 호출자 (라우터) 가 BackgroundTasks 로 본 service 의 `process_import` 비동기 실행
  - DB row 의 status 가 단일 정본 — process 중 timeout 시 별 cleanup 작업이 'failed' 처리
  - finally 블록에서 임시 파일 즉시 삭제 (task2-6.md §8 R-05)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Optional

import psycopg2

from app.db import get_db
from app.repositories.vault_imports_repository import vault_imports_repository
from app.services import vault_import_config as cfg
from app.services.markdown_to_prosemirror import markdown_to_prosemirror
from app.services.pii_scanner import mask_pii, scan_text
from app.services.vault_zip_safety import (
    VaultZipRejected,
    inspect_zip,
    iter_safe_files,
    safe_open_zip,
    check_zip_byte_size,
)

logger = logging.getLogger(__name__)


def _empty_report() -> dict[str, Any]:
    return {
        "documents_imported": 0,
        "folders_created": 0,
        "tags_extracted": 0,
        "wikilinks_found": 0,
        "pii": {},
        "preview": [],   # 1차 dry-run 모드 — 실제 생성 전 미리보기
        "warnings": [],
        "rejection": None,
    }


def process_import(
    *,
    import_id: str,
    file_path: str,
    apply_pii_mask: bool = False,
    delete_after: bool = True,
) -> None:
    """업로드된 zip 을 처리. 본 세션 1차: 파싱 + report 작성 (실제 documents 생성은 별 라운드).

    호출자: 라우터의 BackgroundTasks. 본 함수는 예외 발생 시 status='failed' 마킹.
    """
    logger.info("vault import 시작 — import_id=%s, file=%s", import_id, file_path)
    report = _empty_report()
    bytes_extracted = 0
    file_count = 0

    try:
        # zip 자체 크기 사전 검증
        if os.path.exists(file_path):
            check_zip_byte_size(os.path.getsize(file_path))

        # status running
        with get_db() as conn:
            vault_imports_repository.update_status(
                conn, import_id=import_id, status="running",
            )

        zf = safe_open_zip(file_path)
        try:
            inspection = inspect_zip(zf)
            bytes_extracted = inspection.total_extracted_bytes
            preview: list[dict[str, Any]] = []
            pii_aggregate: dict[str, int] = {}

            for entry, raw_data in iter_safe_files(zf, inspection):
                # markdown 만 처리 — `.md` 확장자
                if not entry.name.lower().endswith(".md"):
                    continue
                file_count += 1
                try:
                    text = raw_data.decode("utf-8", errors="replace")
                except Exception as e:
                    report["warnings"].append({
                        "file": entry.name,
                        "reason": "decode_error",
                        "detail": str(e),
                    })
                    continue

                # markdown → ProseMirror 변환
                doc, frontmatter = markdown_to_prosemirror(text)

                # PII 스캔
                if apply_pii_mask:
                    masked_text, scan_result = mask_pii(text)
                else:
                    scan_result = scan_text(text)

                for kind, count in scan_result.count_by_kind().items():
                    pii_aggregate[kind] = pii_aggregate.get(kind, 0) + count

                preview.append({
                    "path": entry.name,
                    "title": frontmatter.get("title") or _derive_title_from_path(entry.name),
                    "frontmatter_keys": list(frontmatter.keys()),
                    "block_count": len(doc.get("content") or []),
                    "pii_count": len(scan_result.findings),
                    "pii_samples": [
                        {"kind": f.kind, "snippet": f.masked_snippet}
                        for f in scan_result.findings[:3]
                    ],
                })

            # report 집계
            report["documents_imported"] = file_count  # 1차 dry-run: preview 카운트
            report["preview"] = preview[:50]  # 보고서 크기 제한 (대형 vault 보호)
            report["pii"] = pii_aggregate
        finally:
            zf.close()

        with get_db() as conn:
            vault_imports_repository.update_status(
                conn,
                import_id=import_id,
                status="succeeded",
                bytes_extracted=bytes_extracted,
                file_count=file_count,
                report=report,
            )
        logger.info(
            "vault import 완료 — import_id=%s, files=%d, pii=%s",
            import_id, file_count, report["pii"],
        )

    except VaultZipRejected as e:
        logger.warning("vault import 거부 — %s: %s", e.reason, e.detail)
        report["rejection"] = {"reason": e.reason, "detail": e.detail}
        try:
            with get_db() as conn:
                vault_imports_repository.update_status(
                    conn, import_id=import_id, status="failed", report=report,
                )
        except Exception:
            logger.exception("status='failed' 갱신 실패")

    except Exception as e:
        logger.exception("vault import 예외 — import_id=%s", import_id)
        report["rejection"] = {"reason": "parse_error", "detail": str(e)}
        try:
            with get_db() as conn:
                vault_imports_repository.update_status(
                    conn, import_id=import_id, status="failed", report=report,
                )
        except Exception:
            logger.exception("status='failed' 갱신 실패")

    finally:
        # 임시 파일 정리 — task2-6.md §8 R-05
        if delete_after and file_path and os.path.exists(file_path):
            try:
                os.unlink(file_path)
                logger.info("임시 파일 삭제 — %s", file_path)
            except Exception:
                logger.exception("임시 파일 삭제 실패 — %s", file_path)


def _derive_title_from_path(path: str) -> str:
    """`notes/foo.md` → `foo`. 폴더 prefix 제거 + .md 제거."""
    base = path.rsplit("/", 1)[-1]
    if base.lower().endswith(".md"):
        base = base[:-3]
    return base or "(제목 없음)"


__all__ = ["process_import"]
