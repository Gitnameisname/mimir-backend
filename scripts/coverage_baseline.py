"""
S3 Phase 0 / FG 0-3 — 커버리지 베이스라인 생성/검증 스크립트.

역할:
  1. `pytest --cov=app --cov-report=xml:coverage.xml` 이 생성한 XML 을 파싱.
  2. 모듈/패키지 단위 line-rate 요약.
  3. Phase 0 FG 0-3 의 임계값 (전체 75% / services 80% / repositories 80%) 를 검증.
  4. 미달 모듈을 "커버 우선순위" 순으로 출력.

사용법:
  # 1) 측정
  cd backend
  pytest --cov=app --cov-report=xml:coverage.xml --cov-report=html:htmlcov

  # 2) 요약
  python scripts/coverage_baseline.py --xml coverage.xml

  # 3) 임계값 검증 (CI 에서 실패 종료 코드 사용)
  python scripts/coverage_baseline.py --xml coverage.xml --check

  # 4) FG0-3_베이스라인.md 본문 스케치 출력 (docs/... 로 파이핑)
  python scripts/coverage_baseline.py --xml coverage.xml --markdown > ../docs/개발문서/S3/phase0/산출물/FG0-3_베이스라인.md

주의:
  본 스크립트는 stdlib 만 사용한다 (xml.etree + argparse + dataclass).
  CI/로컬 양쪽에서 pip 추가 설치 없이 즉시 돌아간다.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# 임계값 (작업지시서 §5)
# ---------------------------------------------------------------------------


DEFAULT_THRESHOLDS: dict[str, float] = {
    "overall": 75.0,
    "services": 80.0,         # app/services 이하 (하위 패키지 포함)
    "repositories": 80.0,      # app/repositories 이하
}


@dataclass
class ModuleCoverage:
    name: str          # package name (e.g. 'services', 'repositories.extraction')
    files: int
    lines_valid: int
    lines_covered: int

    @property
    def rate(self) -> float:
        return (self.lines_covered / self.lines_valid * 100.0) if self.lines_valid else 0.0

    @property
    def gap_lines(self) -> int:
        return max(0, self.lines_valid - self.lines_covered)


# ---------------------------------------------------------------------------
# XML 파서
# ---------------------------------------------------------------------------


def _count_line_stats(cls_elem: ET.Element) -> tuple[int, int]:
    """<class> 의 line-rate 와 <line> 개수를 결합해 (valid, covered) 반환.

    coverage.py 가 라인 단위 attribute 로 valid/covered 를 제공하지 않는 경우가 있어,
    자식 <line> 요소를 카운트해 정확도 확보.
    """
    lines = list(cls_elem.iter("line"))
    valid = len(lines)
    covered = sum(1 for ln in lines if (ln.get("hits") or "0") != "0")
    return valid, covered


def parse_coverage(xml_path: Path) -> tuple[ModuleCoverage, list[ModuleCoverage], list[tuple[str, int, int, float]]]:
    """coverage.xml 을 파싱해 (전체, 패키지별, 파일별) 3단계 요약을 반환한다."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    pkg_summary: list[ModuleCoverage] = []
    file_summary: list[tuple[str, int, int, float]] = []
    overall_valid = 0
    overall_covered = 0

    for pkg in root.iter("package"):
        name = pkg.get("name") or "."
        valid = 0
        covered = 0
        files = 0
        for cls in pkg.iter("class"):
            files += 1
            v, c = _count_line_stats(cls)
            valid += v
            covered += c
            filename = cls.get("filename") or cls.get("name") or "?"
            # filename 을 package-relative 로 표기 (일반적으로 이미 파일명만 포함됨)
            file_summary.append((f"{name}/{filename}", v, c, (c / v * 100.0) if v else 0.0))
        pkg_summary.append(ModuleCoverage(name=name, files=files, lines_valid=valid, lines_covered=covered))
        overall_valid += valid
        overall_covered += covered

    overall = ModuleCoverage(name="overall", files=sum(p.files for p in pkg_summary),
                             lines_valid=overall_valid, lines_covered=overall_covered)
    return overall, pkg_summary, file_summary


# ---------------------------------------------------------------------------
# 집계 헬퍼 — services / repositories 이하 재귀 합산
# ---------------------------------------------------------------------------


def sum_prefix(packages: list[ModuleCoverage], prefix: str) -> ModuleCoverage:
    """`services`, `services.llm`, `services.evaluation.metrics` 등
    `prefix` 로 시작하는 모든 패키지를 합산."""
    valid = 0
    covered = 0
    files = 0
    for p in packages:
        if p.name == prefix or p.name.startswith(prefix + "."):
            valid += p.lines_valid
            covered += p.lines_covered
            files += p.files
    return ModuleCoverage(name=prefix, files=files, lines_valid=valid, lines_covered=covered)


# ---------------------------------------------------------------------------
# 출력 — 사람 친화 요약
# ---------------------------------------------------------------------------


def print_summary(overall: ModuleCoverage, packages: list[ModuleCoverage]) -> None:
    print(f"Overall: {overall.rate:6.2f}% ({overall.lines_covered}/{overall.lines_valid})")
    print()
    print(f"{'Package':40s} {'Rate':>8s}  {'Covered/Valid':>16s}  {'Files':>6s}")
    print("-" * 80)
    for p in sorted(packages, key=lambda m: m.rate):
        print(f"{p.name:40s} {p.rate:6.2f}%  {p.lines_covered:>6d}/{p.lines_valid:<6d}   {p.files:>6d}")


def print_priority(packages: list[ModuleCoverage], *, min_lines: int = 50, top: int = 20) -> None:
    """커버리지 80% 미만 중 gap_lines 가 큰 순으로 출력 — 작업 우선순위."""
    candidates = [p for p in packages if p.rate < 80.0 and p.lines_valid >= min_lines]
    print(f"\n[Priority] 커버리지 80% 미만 + valid ≥ {min_lines} 라인 패키지 — gap_lines 내림차순 Top {top}")
    print(f"{'Package':40s} {'Rate':>8s}  {'Gap':>6s}  {'Covered/Valid':>16s}")
    print("-" * 80)
    for p in sorted(candidates, key=lambda m: -m.gap_lines)[:top]:
        print(f"{p.name:40s} {p.rate:6.2f}%  {p.gap_lines:>6d}  {p.lines_covered:>6d}/{p.lines_valid:<6d}")


# ---------------------------------------------------------------------------
# 임계값 검증 — CI gate
# ---------------------------------------------------------------------------


def check_thresholds(
    overall: ModuleCoverage,
    packages: list[ModuleCoverage],
    thresholds: dict[str, float] | None = None,
) -> list[str]:
    """미달 항목의 사람 친화 메시지 리스트를 반환. 빈 리스트면 합격."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    failures: list[str] = []
    if overall.rate < t["overall"]:
        failures.append(f"overall {overall.rate:.2f}% < {t['overall']:.2f}%")

    services = sum_prefix(packages, "services")
    if services.lines_valid > 0 and services.rate < t["services"]:
        failures.append(f"app/services/ {services.rate:.2f}% < {t['services']:.2f}% "
                        f"(covered={services.lines_covered}/valid={services.lines_valid})")

    repos = sum_prefix(packages, "repositories")
    if repos.lines_valid > 0 and repos.rate < t["repositories"]:
        failures.append(f"app/repositories/ {repos.rate:.2f}% < {t['repositories']:.2f}% "
                        f"(covered={repos.lines_covered}/valid={repos.lines_valid})")

    return failures


# ---------------------------------------------------------------------------
# Markdown 출력 — FG0-3_베이스라인.md 본문
# ---------------------------------------------------------------------------


def emit_markdown(overall: ModuleCoverage, packages: list[ModuleCoverage]) -> str:
    services = sum_prefix(packages, "services")
    repositories = sum_prefix(packages, "repositories")

    lines: list[str] = []
    lines.append("# FG 0-3 베이스라인 — 백엔드 테스트 커버리지")
    lines.append("")
    lines.append(f"- 측정 시점: `coverage.xml` 파일 기준 (본 스크립트가 자동 생성)")
    lines.append(f"- 측정 주체: `backend/scripts/coverage_baseline.py --markdown`")
    lines.append("")
    lines.append("## 1. 총계")
    lines.append("")
    lines.append(f"| 범위 | 커버리지 | Covered / Valid | Files |")
    lines.append(f"|------|---------|-----------------|-------|")
    lines.append(f"| 전체 | **{overall.rate:.2f}%** | {overall.lines_covered} / {overall.lines_valid} | {overall.files} |")
    lines.append(f"| `app/services/` | **{services.rate:.2f}%** | {services.lines_covered} / {services.lines_valid} | {services.files} |")
    lines.append(f"| `app/repositories/` | **{repositories.rate:.2f}%** | {repositories.lines_covered} / {repositories.lines_valid} | {repositories.files} |")
    lines.append("")
    lines.append("## 2. 패키지별 세부 (rate 오름차순)")
    lines.append("")
    lines.append("| Package | Rate | Covered / Valid | Gap (라인) | Files |")
    lines.append("|---------|------|-----------------|-----------|-------|")
    for p in sorted(packages, key=lambda m: m.rate):
        lines.append(
            f"| `{p.name}` | {p.rate:.2f}% | {p.lines_covered} / {p.lines_valid} | {p.gap_lines} | {p.files} |"
        )
    lines.append("")
    lines.append("## 3. 임계값 대비 게이트")
    lines.append("")
    lines.append("| 항목 | 임계값 | 실측 | 결과 |")
    lines.append("|------|-------|------|------|")
    lines.append(f"| 전체 | 75% | {overall.rate:.2f}% | {'✅' if overall.rate >= 75.0 else '❌'} |")
    lines.append(f"| `app/services/` | 80% | {services.rate:.2f}% | {'✅' if services.rate >= 80.0 else '❌'} |")
    lines.append(f"| `app/repositories/` | 80% | {repositories.rate:.2f}% | {'✅' if repositories.rate >= 80.0 else '❌'} |")
    lines.append("")
    lines.append("## 4. 작업 우선순위 (gap_lines 내림차순, rate < 80% + valid ≥ 50)")
    lines.append("")
    lines.append("| Package | Rate | Gap 라인 |")
    lines.append("|---------|------|----------|")
    priority = [p for p in packages if p.rate < 80.0 and p.lines_valid >= 50]
    for p in sorted(priority, key=lambda m: -m.gap_lines)[:30]:
        lines.append(f"| `{p.name}` | {p.rate:.2f}% | {p.gap_lines} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*자동 생성 — `backend/scripts/coverage_baseline.py --markdown` 재실행으로 갱신 가능*")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FG 0-3 coverage baseline 분석 도구")
    parser.add_argument("--xml", type=Path, default=Path("coverage.xml"),
                        help="coverage.xml 경로 (기본: ./coverage.xml)")
    parser.add_argument("--check", action="store_true",
                        help="임계값 검증 모드 — 미달 시 exit 1")
    parser.add_argument("--markdown", action="store_true",
                        help="FG0-3_베이스라인.md 본문을 stdout 으로 출력")
    parser.add_argument("--threshold-overall", type=float, default=None,
                        help="전체 임계값 (default 75)")
    parser.add_argument("--threshold-services", type=float, default=None,
                        help="services 임계값 (default 80)")
    parser.add_argument("--threshold-repos", type=float, default=None,
                        help="repositories 임계값 (default 80)")
    args = parser.parse_args(argv)

    if not args.xml.exists():
        print(f"[ERR] coverage.xml not found: {args.xml}", file=sys.stderr)
        print(f"      pytest --cov=app --cov-report=xml:{args.xml} 를 먼저 실행하세요.", file=sys.stderr)
        return 2

    overall, packages, _files = parse_coverage(args.xml)

    if args.markdown:
        print(emit_markdown(overall, packages))
        return 0

    print_summary(overall, packages)
    print_priority(packages)

    if args.check:
        thresholds = DEFAULT_THRESHOLDS.copy()
        if args.threshold_overall is not None:
            thresholds["overall"] = args.threshold_overall
        if args.threshold_services is not None:
            thresholds["services"] = args.threshold_services
        if args.threshold_repos is not None:
            thresholds["repositories"] = args.threshold_repos

        failures = check_thresholds(overall, packages, thresholds)
        if failures:
            print()
            print("[FAIL] 커버리지 임계값 미달:", file=sys.stderr)
            for f in failures:
                print(f"  - {f}", file=sys.stderr)
            return 1
        print()
        print("[OK] 모든 임계값 충족")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
