#!/usr/bin/env python3
"""
check_evaluation_thresholds.py — CI 게이트 임계값 검사 CLI

Usage:
  python scripts/check_evaluation_thresholds.py \
    --report evaluation_results/report.json \
    [--config config/evaluation_thresholds.yaml] \
    [--environment dev|staging|prod] \
    [--closed-network] \
    [--pr-number 42] \
    [--commit-sha abc123] \
    [--output evaluation_results/check_result.json]

Exit codes: 0 = PASS, 1 = FAIL
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict

# Allow running from repo root or backend/
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from app.services.evaluation.ci_gate import (
    EvaluationThresholdChecker,
    ThresholdLoader,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def load_evaluation_results(report_path: str) -> Dict[str, float]:
    path = Path(report_path)
    if not path.exists():
        raise FileNotFoundError(f"Evaluation report not found: {report_path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if "metrics" in data:
        return data["metrics"]
    if "evaluation_results" in data and "metrics" in data["evaluation_results"]:
        return data["evaluation_results"]["metrics"]
    raise ValueError("Invalid evaluation report format")


def main() -> None:
    parser = argparse.ArgumentParser(description="CI 게이트: 평가 임계값 검사")
    parser.add_argument("--report", required=True, help="평가 보고서 JSON 경로")
    parser.add_argument(
        "--config",
        default=str(_HERE.parent / "config" / "evaluation_thresholds.yaml"),
        help="임계값 설정 파일 경로",
    )
    parser.add_argument(
        "--environment",
        default="prod",
        choices=["dev", "staging", "prod"],
        help="환경명 (기본: prod)",
    )
    parser.add_argument("--closed-network", action="store_true", help="폐쇄망 모드")
    parser.add_argument("--pr-number", type=int, help="PR 번호 (감사용)")
    parser.add_argument("--commit-sha", type=str, help="커밋 SHA (감사용)")
    parser.add_argument("--output", type=str, help="결과 JSON 저장 경로")
    args = parser.parse_args()

    try:
        logger.info("Loading evaluation report: %s", args.report)
        evaluation_results = load_evaluation_results(args.report)

        loader = ThresholdLoader(args.config)
        checker = EvaluationThresholdChecker(
            threshold_loader=loader,
            environment=args.environment,
            closed_network_mode=args.closed_network,
        )

        report = checker.check_metrics(
            evaluation_results=evaluation_results,
            pr_number=args.pr_number,
            commit_sha=args.commit_sha,
        )

        print(report.summary_message)
        print()
        print(report.detailed_message)

        if args.output:
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "overall_passed": report.overall_passed,
                "total_metrics": report.total_metrics,
                "passed_metrics": report.passed_metrics,
                "failed_metrics": report.failed_metrics,
                "metrics": [
                    {
                        "name": r.metric_name,
                        "actual": r.actual_value,
                        "threshold": r.threshold,
                        "passed": r.passed,
                    }
                    for r in report.metric_results
                ],
            }
            with open(out, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            logger.info("Result saved to %s", args.output)

        sys.exit(0 if report.overall_passed else 1)

    except Exception as exc:
        logger.error("Error during threshold check: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
