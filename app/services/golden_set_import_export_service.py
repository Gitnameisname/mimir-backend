"""
Golden Set import/export 서비스 — Phase 7 FG7.1

psycopg2 기반. S2 ⑦ 준수: 외부 API 미사용.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.models.golden_set import GoldenItemCreateRequest
from app.models.golden_set_import_export import (
    GoldenSetExportResponse,
    GoldenSetImportItem,
    GoldenSetImportRequest,
    GoldenSetImportResult,
    ImportItemResult,
    ImportValidator,
)
from app.repositories.golden_set_repository import (
    GoldenItemRepository,
    GoldenSetRepository,
)

logger = logging.getLogger(__name__)


class GoldenSetImportExportService:
    def __init__(self, conn) -> None:
        self._conn = conn
        self._set_repo = GoldenSetRepository(conn)
        self._item_repo = GoldenItemRepository(conn)

    # ── Export ──────────────────────────────────────────────────────────────

    def export(self, golden_set_id: str, scope_id: str) -> Optional[GoldenSetExportResponse]:
        """GoldenSet 전체를 JSON-serializable 응답으로 변환."""
        gs = self._set_repo.get_by_id(golden_set_id, scope_id, include_items=True)
        if not gs:
            return None

        items = [
            GoldenSetImportItem(
                question=i.question,
                expected_answer=i.expected_answer,
                expected_source_docs=i.expected_source_docs,
                expected_citations=i.expected_citations,
                notes=i.notes,
            )
            for i in (gs.items or [])
        ]

        return GoldenSetExportResponse(
            id=gs.id,
            scope_id=gs.scope_id,
            name=gs.name,
            description=gs.description,
            domain=gs.domain.value if hasattr(gs.domain, "value") else gs.domain,
            status=gs.status.value if hasattr(gs.status, "value") else gs.status,
            golden_set_version=gs.version,
            created_at=gs.created_at.isoformat(),
            created_by=gs.created_by,
            updated_at=gs.updated_at.isoformat(),
            updated_by=gs.updated_by,
            items=items,
        )

    # ── Import ───────────────────────────────────────────────────────────────

    def import_items(
        self,
        golden_set_id: str,
        scope_id: str,
        request: GoldenSetImportRequest,
        actor_id: str,
        *,
        allow_partial: bool = True,
    ) -> tuple[bool, GoldenSetImportResult]:
        """JSON 데이터에서 GoldenItem 일괄 생성.

        allow_partial=False 이면 첫 실패 시 롤백하고 즉시 반환.
        allow_partial=True 이면 실패 항목을 기록하되 나머지를 계속 import.
        """
        # 1. 의미론 검증
        ok, errs = ImportValidator.validate(request)
        if not ok:
            return False, GoldenSetImportResult(
                total_items=len(request.items),
                successful_items=0,
                failed_items=len(request.items),
                created_item_ids=[],
                errors=[ImportItemResult(index=0, question="<validation>", success=False,
                                         error="; ".join(errs))],
            )

        # 2. Parent 존재 확인
        if not self._set_repo.get_by_id(golden_set_id, scope_id):
            return False, GoldenSetImportResult(
                total_items=len(request.items),
                successful_items=0,
                failed_items=len(request.items),
                created_item_ids=[],
                errors=[ImportItemResult(index=0, question="<parent>", success=False,
                                         error="GoldenSet을 찾을 수 없습니다.")],
            )

        # 3. 항목별 생성 (트랜잭션은 psycopg2 autocommit=False 기본값 활용)
        created_ids: list[str] = []
        item_errors: list[ImportItemResult] = []

        for idx, imp_item in enumerate(request.items):
            try:
                create_req = GoldenItemCreateRequest(
                    question=imp_item.question,
                    expected_answer=imp_item.expected_answer,
                    expected_source_docs=imp_item.expected_source_docs,
                    expected_citations=imp_item.expected_citations,
                    notes=imp_item.notes,
                )
                item = self._item_repo.create_item(
                    golden_set_id=golden_set_id,
                    scope_id=scope_id,
                    request=create_req,
                    created_by=actor_id,
                )
                if item is None:
                    raise ValueError("create_item returned None")
                created_ids.append(item.id)

            except Exception as exc:
                msg = str(exc)
                item_errors.append(ImportItemResult(
                    index=idx, question=imp_item.question[:100], success=False, error=msg
                ))
                logger.warning("import item[%d] failed: %s", idx, msg)

                if not allow_partial:
                    self._conn.rollback()
                    return False, GoldenSetImportResult(
                        total_items=len(request.items),
                        successful_items=len(created_ids),
                        failed_items=len(request.items) - len(created_ids),
                        created_item_ids=created_ids,
                        errors=item_errors,
                    )

        success = len(item_errors) == 0
        result = GoldenSetImportResult(
            total_items=len(request.items),
            successful_items=len(created_ids),
            failed_items=len(item_errors),
            created_item_ids=created_ids,
            errors=item_errors,
        )
        logger.info(
            "import completed golden_set_id=%s %d/%d items success_rate=%.1f%%",
            golden_set_id, len(created_ids), len(request.items), result.success_rate * 100,
        )
        return success, result

    # ── Round-trip 검증 ──────────────────────────────────────────────────────

    def verify_round_trip(
        self, golden_set_id: str, scope_id: str
    ) -> tuple[bool, list[str]]:
        """Export → Pydantic 재파싱 → 항목 일치 확인 (실제 DB import 없음)."""
        errors: list[str] = []

        exported = self.export(golden_set_id, scope_id)
        if exported is None:
            return False, ["GoldenSet을 찾을 수 없습니다."]

        # export 결과를 import 스키마로 재파싱
        try:
            reimport = GoldenSetImportRequest(
                format_version=exported.format_version,
                name=exported.name,
                description=exported.description,
                domain=exported.domain,
                items=exported.items,
            )
        except Exception as exc:
            return False, [f"재파싱 실패: {exc}"]

        # 의미론 검증
        ok, errs = ImportValidator.validate(reimport)
        if not ok:
            errors += [f"재검증 실패: {e}" for e in errs]

        # 항목 수 일치
        if len(exported.items) != len(reimport.items):
            errors.append(
                f"항목 수 불일치: export={len(exported.items)}, reimport={len(reimport.items)}"
            )

        # 각 항목 필드 비교
        for i, (orig, reim) in enumerate(zip(exported.items, reimport.items)):
            if orig.question != reim.question:
                errors.append(f"index {i}: question 불일치")
            if orig.expected_answer != reim.expected_answer:
                errors.append(f"index {i}: expected_answer 불일치")
            if orig.notes != reim.notes:
                errors.append(f"index {i}: notes 불일치")
            if len(orig.expected_source_docs) != len(reim.expected_source_docs):
                errors.append(f"index {i}: expected_source_docs 개수 불일치")
            if len(orig.expected_citations) != len(reim.expected_citations):
                errors.append(f"index {i}: expected_citations 개수 불일치")

        return len(errors) == 0, errors
