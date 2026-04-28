"""
S3 Phase 4 FG 4-2 §2.1.5 — `resolve_document_reference` AI 품질 평가.

50+ 케이스 골든셋 + Precision@1 / Recall@5 / Disambiguation correctness 측정.

운영 절차 (Phase 0 FG 0-4 와 동일 패턴 — 선언적 종결 + 운영자 실측):
  1. 운영자가 staging DB 에 골든셋 시드 (`--seed`)
  2. 운영자가 평가 실행 (`--run`)
  3. 결과 JSON 저장 → `docs/개발문서/S3/phase4/산출물/FG4-2_AI품질평가_<date>.json`
  4. 검수보고서에 결과 첨부

골든셋 카테고리 (50건):
  - 정확 매칭 (exact_title)            : 20건
  - 별칭 매칭 (alias)                  : 10건
  - 의미 매칭 (semantic / fts_fallback) : 15건
  - disambiguation 트리거 (모호)       : 5건+

지표 임계값 (task4-2 §2.1.5):
  - Precision@1 ≥ 0.85
  - Recall@5    ≥ 0.95
  - Disambiguation correctness ≥ 0.80
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))


# ---------------------------------------------------------------------------
# 골든셋 정의 (50 케이스)
# ---------------------------------------------------------------------------


@dataclass
class GoldenCase:
    case_id: str
    category: str  # exact_title | alias | semantic | disambiguation
    reference: str
    expected_document_id: Optional[str] = None  # disambiguation 케이스는 None
    expected_disambiguation: bool = False
    notes: Optional[str] = None


# 운영 환경 의존 — 실 measurement 시점에 staging DB seed 와 일치하는 document_id 채움.
# 본 골든셋은 시멘틱 카테고리 분류와 reference 텍스트만 정본 — id 는 placeholder.
GOLDEN_CASES: list[GoldenCase] = [
    # --- 1) 정확 매칭 (20) ---
    GoldenCase("E01", "exact_title", "영업 매뉴얼 2024", "<seed:sales_manual_2024>"),
    GoldenCase("E02", "exact_title", "보안 정책 v2", "<seed:security_policy_v2>"),
    GoldenCase("E03", "exact_title", "On-call Runbook", "<seed:oncall_runbook>"),
    GoldenCase("E04", "exact_title", "Engineering Onboarding", "<seed:eng_onboarding>"),
    GoldenCase("E05", "exact_title", "Product Roadmap Q3", "<seed:roadmap_q3>"),
    GoldenCase("E06", "exact_title", "구매 정책", "<seed:procurement>"),
    GoldenCase("E07", "exact_title", "Code Review Guidelines", "<seed:code_review>"),
    GoldenCase("E08", "exact_title", "데이터 거버넌스 가이드", "<seed:data_gov>"),
    GoldenCase("E09", "exact_title", "Incident Postmortem Template", "<seed:postmortem_tpl>"),
    GoldenCase("E10", "exact_title", "성과 평가 기준 2024", "<seed:perf_review_2024>"),
    GoldenCase("E11", "exact_title", "API Style Guide", "<seed:api_style>"),
    GoldenCase("E12", "exact_title", "고객 지원 SLA", "<seed:support_sla>"),
    GoldenCase("E13", "exact_title", "Brand Asset Library", "<seed:brand>"),
    GoldenCase("E14", "exact_title", "정보 보안 핸드북", "<seed:infosec_handbook>"),
    GoldenCase("E15", "exact_title", "릴리스 프로세스", "<seed:release_process>"),
    GoldenCase("E16", "exact_title", "Migration Playbook", "<seed:migration>"),
    GoldenCase("E17", "exact_title", "재택 근무 가이드", "<seed:remote_work>"),
    GoldenCase("E18", "exact_title", "GDPR Compliance Notes", "<seed:gdpr>"),
    GoldenCase("E19", "exact_title", "Architecture Decision Records", "<seed:adr_index>"),
    GoldenCase("E20", "exact_title", "비밀번호 정책", "<seed:password_policy>"),

    # --- 2) 별칭 매칭 (10) ---
    GoldenCase("A01", "alias", "Sales Playbook", "<seed:sales_manual_2024>",
               notes="alias of '영업 매뉴얼 2024'"),
    GoldenCase("A02", "alias", "Security v2", "<seed:security_policy_v2>",
               notes="alias of '보안 정책 v2'"),
    GoldenCase("A03", "alias", "DevOps Runbook", "<seed:oncall_runbook>"),
    GoldenCase("A04", "alias", "Roadmap Q3 2024", "<seed:roadmap_q3>"),
    GoldenCase("A05", "alias", "스타일 가이드", "<seed:api_style>"),
    GoldenCase("A06", "alias", "Postmortem", "<seed:postmortem_tpl>"),
    GoldenCase("A07", "alias", "Brand Guide", "<seed:brand>"),
    GoldenCase("A08", "alias", "정보 보안 가이드", "<seed:infosec_handbook>"),
    GoldenCase("A09", "alias", "Migration Guide", "<seed:migration>"),
    GoldenCase("A10", "alias", "ADR Index", "<seed:adr_index>"),

    # --- 3) 의미 매칭 (15) ---
    GoldenCase("S01", "semantic", "회사 영업 절차 안내",
               "<seed:sales_manual_2024>", notes="실 reference 가 제목과 다른 자연어"),
    GoldenCase("S02", "semantic", "사고 발생 시 절차", "<seed:postmortem_tpl>"),
    GoldenCase("S03", "semantic", "신규 입사자 가이드", "<seed:eng_onboarding>"),
    GoldenCase("S04", "semantic", "API 디자인 규칙", "<seed:api_style>"),
    GoldenCase("S05", "semantic", "고객 응대 시간 약속", "<seed:support_sla>"),
    GoldenCase("S06", "semantic", "데이터 처리 규정", "<seed:data_gov>"),
    GoldenCase("S07", "semantic", "릴리스 흐름 문서", "<seed:release_process>"),
    GoldenCase("S08", "semantic", "원격 근무 룰", "<seed:remote_work>"),
    GoldenCase("S09", "semantic", "GDPR 관련 노트", "<seed:gdpr>"),
    GoldenCase("S10", "semantic", "암호 보안 룰", "<seed:password_policy>"),
    GoldenCase("S11", "semantic", "당직 절차", "<seed:oncall_runbook>"),
    GoldenCase("S12", "semantic", "코드 검토 기준", "<seed:code_review>"),
    GoldenCase("S13", "semantic", "구매 승인 절차", "<seed:procurement>"),
    GoldenCase("S14", "semantic", "분기 제품 계획", "<seed:roadmap_q3>"),
    GoldenCase("S15", "semantic", "보안 운영 매뉴얼", "<seed:infosec_handbook>"),

    # --- 4) Disambiguation 필요 (5) ---
    GoldenCase("D01", "disambiguation", "정책",
               expected_disambiguation=True,
               notes="정책 으로 시작하는 문서가 5+개 — 모호"),
    GoldenCase("D02", "disambiguation", "manual",
               expected_disambiguation=True,
               notes="manual 단일 단어 — 영업/보안/마이그레이션 등 다수 매칭"),
    GoldenCase("D03", "disambiguation", "그 문서",
               expected_disambiguation=True,
               notes="대명사 — 어떤 문서인지 불명"),
    GoldenCase("D04", "disambiguation", "어제 본 거",
               expected_disambiguation=True,
               notes="recent_context 없이는 해소 불가"),
    GoldenCase("D05", "disambiguation", "report",
               expected_disambiguation=True,
               notes="단일 단어, 다수 매칭"),
]


# ---------------------------------------------------------------------------
# 평가 지표
# ---------------------------------------------------------------------------


@dataclass
class EvaluationResult:
    case_id: str
    category: str
    reference: str
    expected_document_id: Optional[str]
    expected_disambiguation: bool
    actual_best_document_id: Optional[str]
    actual_disambiguation: bool
    in_top_5: bool
    pass_at_1: bool
    pass_disambiguation: bool


@dataclass
class AggregateMetrics:
    total: int
    precision_at_1: float
    recall_at_5: float
    disambiguation_correctness: float
    by_category: dict[str, dict[str, float]]


def _evaluate_one(case: GoldenCase, conn) -> EvaluationResult:
    """단일 케이스 평가 — 실 DB / mock 어디서든 동작."""
    from app.services.document_resolver_service import resolve_reference

    result = resolve_reference(
        conn,
        case.reference,
        recent_document_ids=[],
        max_candidates=5,
        confidence_threshold=0.85,
    )
    actual_best = result.best_match.document_id if result.best_match else None
    actual_disamb = result.needs_disambiguation
    candidate_ids = [c.document_id for c in result.candidates]
    in_top_5 = case.expected_document_id is not None and case.expected_document_id in candidate_ids
    pass_at_1 = (actual_best == case.expected_document_id) if case.expected_document_id else False
    pass_disamb = actual_disamb == case.expected_disambiguation
    return EvaluationResult(
        case_id=case.case_id,
        category=case.category,
        reference=case.reference,
        expected_document_id=case.expected_document_id,
        expected_disambiguation=case.expected_disambiguation,
        actual_best_document_id=actual_best,
        actual_disambiguation=actual_disamb,
        in_top_5=in_top_5,
        pass_at_1=pass_at_1,
        pass_disambiguation=pass_disamb,
    )


def aggregate(results: list[EvaluationResult]) -> AggregateMetrics:
    total = len(results)
    if total == 0:
        return AggregateMetrics(0, 0.0, 0.0, 0.0, {})

    # Precision@1: expected_document_id 가 있는 케이스 중 actual_best 일치 비율
    p1_eligible = [r for r in results if r.expected_document_id is not None]
    p1 = (sum(1 for r in p1_eligible if r.pass_at_1) / len(p1_eligible)) if p1_eligible else 0.0

    # Recall@5: 동일 모집단에서 in_top_5 비율
    r5 = (sum(1 for r in p1_eligible if r.in_top_5) / len(p1_eligible)) if p1_eligible else 0.0

    # Disambiguation correctness: 전체 케이스의 pass_disambiguation 비율
    da = sum(1 for r in results if r.pass_disambiguation) / total

    by_category: dict[str, dict[str, float]] = {}
    for cat in {r.category for r in results}:
        cat_results = [r for r in results if r.category == cat]
        cat_p1_eligible = [r for r in cat_results if r.expected_document_id is not None]
        by_category[cat] = {
            "n": len(cat_results),
            "precision_at_1": (
                sum(1 for r in cat_p1_eligible if r.pass_at_1) / len(cat_p1_eligible)
                if cat_p1_eligible else 0.0
            ),
            "recall_at_5": (
                sum(1 for r in cat_p1_eligible if r.in_top_5) / len(cat_p1_eligible)
                if cat_p1_eligible else 0.0
            ),
            "disambiguation_correctness": (
                sum(1 for r in cat_results if r.pass_disambiguation) / len(cat_results)
            ),
        }

    return AggregateMetrics(
        total=total,
        precision_at_1=p1,
        recall_at_5=r5,
        disambiguation_correctness=da,
        by_category=by_category,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="resolve_document_reference 골든셋 평가")
    parser.add_argument("--list", action="store_true", help="골든셋만 출력 (DB 미접근)")
    parser.add_argument("--run", action="store_true", help="평가 실행 (DB 접근)")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "docs/개발문서/S3/phase4/산출물/FG4-2_AI품질평가_results.json",
        help="결과 JSON 출력 경로",
    )
    args = parser.parse_args()

    if args.list:
        for c in GOLDEN_CASES:
            print(f"  [{c.case_id}] {c.category:>15s}  {c.reference}")
        print(f"\nTotal: {len(GOLDEN_CASES)}")
        # 카테고리별 카운트
        from collections import Counter
        cnt = Counter(c.category for c in GOLDEN_CASES)
        print(f"by category: {dict(cnt)}")
        return 0

    if not args.run:
        print("[hint] --list 로 골든셋 검토 / --run 으로 실 평가 실행 (DB 필요)")
        return 0

    # 실 평가 — DB 접근
    from app.db.connection import get_db

    results: list[EvaluationResult] = []
    with get_db() as conn:
        for c in GOLDEN_CASES:
            try:
                results.append(_evaluate_one(c, conn))
            except Exception as exc:
                print(f"[FAIL] {c.case_id}: {exc}", file=sys.stderr)

    metrics = aggregate(results)
    payload = {
        "metrics": asdict(metrics),
        "results": [asdict(r) for r in results],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] {args.out.relative_to(ROOT)} written")
    print(f"  Precision@1                 = {metrics.precision_at_1:.3f} (target ≥ 0.85)")
    print(f"  Recall@5                    = {metrics.recall_at_5:.3f} (target ≥ 0.95)")
    print(f"  Disambiguation correctness  = {metrics.disambiguation_correctness:.3f} (target ≥ 0.80)")
    if metrics.precision_at_1 < 0.85:
        print("  [WARN] Precision@1 임계값 미달")
    if metrics.recall_at_5 < 0.95:
        print("  [WARN] Recall@5 임계값 미달")
    if metrics.disambiguation_correctness < 0.80:
        print("  [WARN] Disambiguation correctness 임계값 미달")
    return 0


if __name__ == "__main__":
    sys.exit(main())
