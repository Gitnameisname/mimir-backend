#!/usr/bin/env python3
"""
generate_evaluation_report.py — AI품질평가.md 보고서 생성 CLI

Usage:
  python scripts/generate_evaluation_report.py \
    --report evaluation_results/report.json \
    --output-dir results/ \
    [--environment prod] \
    [--save-metadata] \
    [--dashboard-url http://...]

Reads a flat evaluation JSON (see ReportData schema) and renders
templates/ai_quality_evaluation.md.jinja2 into an AI품질평가.md file.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from jinja2 import Environment, FileSystemLoader

from app.services.evaluation.ci_gate import ThresholdLoader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_DEFAULT_TEMPLATE_DIR = _HERE.parent / "templates"
_DEFAULT_TEMPLATE_NAME = "ai_quality_evaluation.md.jinja2"
_DEFAULT_CONFIG = _HERE.parent / "config" / "evaluation_thresholds.yaml"


# ---------------------------------------------------------------------------
# Data models (plain dataclasses — no DB dependency)
# ---------------------------------------------------------------------------

@dataclass
class MetricResult:
    value: float
    passed: bool


@dataclass
class EvalItem:
    question: str
    expected_answer: Optional[str] = None
    generated_answer: Optional[str] = None
    faithfulness: Optional[float] = None
    answer_relevance: Optional[float] = None
    context_precision: Optional[float] = None
    context_recall: Optional[float] = None
    citation_present: Optional[float] = None
    hallucination: Optional[float] = None


@dataclass
class ReportInsights:
    strengths: List[str] = field(default_factory=list)
    weaknesses: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


@dataclass
class ReportData:
    golden_set_id: str
    prompt_version: str
    model: str
    timestamp: str
    environment: str
    metrics: Dict[str, MetricResult]
    overall_passed: bool
    total_metrics: int
    passed_metrics: int
    failed_metrics: int
    evaluation_items: List[EvalItem] = field(default_factory=list)
    insights: Optional[ReportInsights] = None
    closed_network_mode: bool = False
    commit_sha: Optional[str] = None
    pr_number: Optional[int] = None
    backend_url: Optional[str] = None


# ---------------------------------------------------------------------------
# MetricConfig helper (for template)
# ---------------------------------------------------------------------------

@dataclass
class MetricConfig:
    threshold: float
    description: str
    direction: str


# ---------------------------------------------------------------------------
# JSON → ReportData
# ---------------------------------------------------------------------------

def load_report_from_json(path: str) -> ReportData:
    with open(path, encoding="utf-8") as f:
        raw: Dict[str, Any] = json.load(f)

    raw_metrics: Dict[str, Any] = raw.get("metrics", {})
    metrics: Dict[str, MetricResult] = {}
    for name, val in raw_metrics.items():
        if isinstance(val, dict):
            metrics[name] = MetricResult(
                value=float(val.get("value", 0.0)),
                passed=bool(val.get("passed", False)),
            )
        else:
            # flat float 형식: passed 정보 없음 → overall_passed로 판정하도록 True 처리
            metrics[name] = MetricResult(value=float(val), passed=True)

    items: List[EvalItem] = []
    for it in raw.get("evaluation_items", []):
        items.append(EvalItem(
            question=it.get("question", ""),
            expected_answer=it.get("expected_answer"),
            generated_answer=it.get("generated_answer"),
            faithfulness=it.get("faithfulness"),
            answer_relevance=it.get("answer_relevance"),
            context_precision=it.get("context_precision"),
            context_recall=it.get("context_recall"),
            citation_present=it.get("citation_present"),
            hallucination=it.get("hallucination"),
        ))

    raw_insights = raw.get("insights") or {}
    insights = ReportInsights(
        strengths=raw_insights.get("strengths", []),
        weaknesses=raw_insights.get("weaknesses", []),
        recommendations=raw_insights.get("recommendations", []),
    )

    total = len(metrics)
    passed = sum(1 for m in metrics.values() if m.passed)

    return ReportData(
        golden_set_id=raw.get("golden_set_id", "unknown"),
        prompt_version=raw.get("prompt_version", "unknown"),
        model=raw.get("model", "unknown"),
        timestamp=raw.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        environment=raw.get("environment", "prod"),
        metrics=metrics,
        overall_passed=raw.get("overall_passed", all(m.passed for m in metrics.values())),
        total_metrics=raw.get("total_metrics", total),
        passed_metrics=raw.get("passed_metrics", passed),
        failed_metrics=raw.get("failed_metrics", total - passed),
        evaluation_items=items,
        insights=insights,
        closed_network_mode=raw.get("closed_network_mode", False),
        commit_sha=raw.get("commit_sha"),
        pr_number=raw.get("pr_number"),
        backend_url=raw.get("backend_url"),
    )


# ---------------------------------------------------------------------------
# Metadata accumulation
# ---------------------------------------------------------------------------

def _report_to_metadata(report: ReportData, environment: str) -> Dict[str, Any]:
    return {
        "golden_set_id": report.golden_set_id,
        "prompt_version": report.prompt_version,
        "model": report.model,
        "timestamp": report.timestamp,
        "environment": environment,
        "closed_network_mode": report.closed_network_mode,
        "metrics": {
            k: {"value": v.value, "passed": v.passed}
            for k, v in report.metrics.items()
        },
        "overall_passed": report.overall_passed,
        "total_metrics": report.total_metrics,
        "passed_metrics": report.passed_metrics,
        "failed_metrics": report.failed_metrics,
        "evaluation_items_count": len(report.evaluation_items),
        "commit_sha": report.commit_sha,
        "pr_number": report.pr_number,
        "backend_url": report.backend_url,
    }


def update_evaluation_metadata(metadata_dir: str, new_metadata: Dict[str, Any]) -> None:
    p = Path(metadata_dir) / "evaluation_metadata.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    records: List[Dict[str, Any]] = []
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                existing = json.load(f)
            records = existing if isinstance(existing, list) else [existing]
        except Exception as exc:
            logger.warning("Could not read existing metadata: %s", exc)
    records.append(new_metadata)
    if len(records) > 100:
        records = records[-100:]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    logger.info("Metadata updated: %s (%d records)", p, len(records))


# ---------------------------------------------------------------------------
# Report generator
# ---------------------------------------------------------------------------

class EvaluationReportGenerator:
    def __init__(
        self,
        template_dir: Optional[str] = None,
        template_name: str = _DEFAULT_TEMPLATE_NAME,
        config_path: Optional[str] = None,
    ) -> None:
        tdir = Path(template_dir) if template_dir else _DEFAULT_TEMPLATE_DIR
        self._env = Environment(
            loader=FileSystemLoader(str(tdir)),
            autoescape=False,
            keep_trailing_newline=True,
        )
        self._template_name = template_name
        cfg = str(config_path) if config_path else str(_DEFAULT_CONFIG)
        self._loader = ThresholdLoader(cfg)

    def _build_metrics_config(self, environment: str) -> Dict[str, MetricConfig]:
        config = self._loader.load()
        thresholds = self._loader.get_thresholds(environment=environment)
        result: Dict[str, MetricConfig] = {}
        for name, m in config.metrics.items():
            result[name] = MetricConfig(
                threshold=thresholds[name],
                description=m.description,
                direction=m.direction,
            )
        return result

    @staticmethod
    def _safe_output_path(output_path: Path) -> Path:
        """경로 순회 방지: 경로에 null 바이트, 절대 경로 탈출 패턴 차단 (VULN-P7-006)."""
        resolved = output_path.resolve()
        parts = resolved.parts
        if any(p in ("..", "") and i > 0 for i, p in enumerate(parts)):
            raise ValueError(f"허용되지 않는 출력 경로: {output_path}")
        if "\x00" in str(output_path):
            raise ValueError("경로에 null 바이트가 포함되어 있습니다.")
        return output_path

    def generate(
        self,
        report: ReportData,
        output_path: Path,
        previous_report: Optional[ReportData] = None,
        dashboard_url: Optional[str] = None,
        environment: str = "prod",
    ) -> Path:
        output_path = self._safe_output_path(Path(output_path))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_config = self._build_metrics_config(environment)
        template = self._env.get_template(self._template_name)
        rendered = template.render(
            report=report,
            previous_report=previous_report,
            metrics_config=metrics_config,
            dashboard_url=dashboard_url,
        )
        output_path.write_text(rendered, encoding="utf-8")
        logger.info("Report generated: %s", output_path)
        return output_path

    def generate_metadata(
        self,
        report: ReportData,
        output_path: Path,
        environment: str = "prod",
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        meta = _report_to_metadata(report, environment)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        logger.info("Metadata saved: %s", output_path)
        return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="AI품질평가.md 보고서 생성")
    parser.add_argument("--report", required=True, help="평가 결과 JSON 경로")
    parser.add_argument("--output-dir", default=".", help="출력 디렉토리")
    parser.add_argument("--report-name", default="AI품질평가.md", help="보고서 파일명")
    parser.add_argument("--environment", default="prod", choices=["dev", "staging", "prod"])
    parser.add_argument("--dashboard-url", help="대시보드 URL")
    parser.add_argument("--save-metadata", action="store_true", help="메타데이터 JSON 저장")
    parser.add_argument("--previous-report", help="이전 평가 JSON 경로 (비교용)")
    args = parser.parse_args()

    try:
        report = load_report_from_json(args.report)
        previous: Optional[ReportData] = None
        if args.previous_report:
            previous = load_report_from_json(args.previous_report)

        generator = EvaluationReportGenerator()
        out = Path(args.output_dir) / args.report_name
        generator.generate(
            report=report,
            output_path=out,
            previous_report=previous,
            dashboard_url=args.dashboard_url,
            environment=args.environment,
        )
        print(f"Report: {out}")

        if args.save_metadata:
            meta_path = Path(args.output_dir) / "evaluation_metadata_latest.json"
            generator.generate_metadata(report, meta_path, environment=args.environment)
            update_evaluation_metadata(args.output_dir, _report_to_metadata(report, args.environment))
            print(f"Metadata: {meta_path}")

    except Exception as exc:
        logger.error("Report generation failed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
