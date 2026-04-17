"""
CI 게이트 임계값 설정 및 검사 — Phase 7 FG7.3 (task7-8)

YAML 기반 임계값 로드, 환경별 오버라이드, Pydantic validation,
EvaluationThresholdChecker 로직을 제공한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

_REQUIRED_METRICS = frozenset({
    "faithfulness", "answer_relevance", "context_precision",
    "context_recall", "citation_present", "hallucination",
})


# ---------------------------------------------------------------------------
# Pydantic config models
# ---------------------------------------------------------------------------

class MetricThreshold(BaseModel):
    threshold: float = Field(..., ge=0.0, le=1.0)
    description: str
    direction: Literal["higher_is_better", "lower_is_better"]
    unit: str = "proportion"


class ClosedNetworkConfig(BaseModel):
    fallback_scoring_strategy: str = "heuristic"
    fallback_base_scores: Dict[str, float]

    @field_validator("fallback_base_scores")
    @classmethod
    def _validate_scores(cls, v: Dict[str, float]) -> Dict[str, float]:
        for metric, score in v.items():
            if not 0.0 <= score <= 1.0:
                raise ValueError(f"Score for {metric} must be 0–1, got {score}")
        return v


class EvaluationThresholdsConfig(BaseModel):
    metrics: Dict[str, MetricThreshold]
    environments: Optional[Dict[str, Any]] = None
    skip_conditions: Optional[Dict[str, Any]] = None
    closed_network_mode: Optional[ClosedNetworkConfig] = None
    timeouts: Optional[Dict[str, int]] = None

    @field_validator("metrics")
    @classmethod
    def _validate_required(cls, v: Dict[str, MetricThreshold]) -> Dict[str, MetricThreshold]:
        missing = _REQUIRED_METRICS - set(v.keys())
        if missing:
            raise ValueError(f"Missing required metrics: {missing}")
        return v


# ---------------------------------------------------------------------------
# ThresholdLoader
# ---------------------------------------------------------------------------

class ThresholdLoader:
    def __init__(self, config_path: Optional[str] = None) -> None:
        if config_path is None:
            config_path = str(Path(__file__).parent.parent.parent.parent / "config" / "evaluation_thresholds.yaml")
        self.config_path = Path(config_path)

    def load(self) -> EvaluationThresholdsConfig:
        import yaml
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config not found: {self.config_path}")
        with open(self.config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return EvaluationThresholdsConfig(**data)

    def get_thresholds(
        self,
        environment: str = "prod",
        closed_network_mode: bool = False,
    ) -> Dict[str, float]:
        config = self.load()
        # 폐쇄망 모드: YAML fallback_base_scores 사용 (S2 원칙 ⑦)
        if closed_network_mode and config.closed_network_mode:
            raw = config.closed_network_mode
            if isinstance(raw, dict):
                scores = raw.get("fallback_base_scores") or {}
            else:
                scores = getattr(raw, "fallback_base_scores", {}) or {}
            if scores:
                return {name: float(v) for name, v in scores.items()}
        thresholds = {name: m.threshold for name, m in config.metrics.items()}
        if config.environments and environment in config.environments:
            override = config.environments[environment].get("override") or {}
            thresholds.update(override)
        return thresholds

    def get_metric_direction(self, metric_name: str) -> Literal["higher_is_better", "lower_is_better"]:
        config = self.load()
        if metric_name not in config.metrics:
            raise ValueError(f"Unknown metric: {metric_name}")
        return config.metrics[metric_name].direction

    def should_skip_evaluation(
        self,
        pr_labels: List[str],
        changed_files: List[str],
    ) -> Tuple[bool, Optional[str]]:
        config = self.load()
        if not config.skip_conditions:
            return False, None

        override_labels: List[str] = config.skip_conditions.get("override_labels", [])
        for label in pr_labels:
            if label in override_labels:
                return True, f"Override label detected: {label}"

        skip_patterns: List[str] = config.skip_conditions.get("skip_evaluation_file_patterns", [])
        if skip_patterns and changed_files:
            all_skip = all(
                any(fnmatch(f, pattern) for pattern in skip_patterns)
                for f in changed_files
            )
            if all_skip:
                return True, "All changes are in skip-evaluation paths"

        return False, None


# ---------------------------------------------------------------------------
# Checker dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MetricCheckResult:
    metric_name: str
    actual_value: float
    threshold: float
    direction: str
    passed: bool
    message: str


@dataclass
class ThresholdCheckReport:
    overall_passed: bool
    total_metrics: int
    passed_metrics: int
    failed_metrics: int
    metric_results: List[MetricCheckResult]
    summary_message: str
    detailed_message: str


# ---------------------------------------------------------------------------
# EvaluationThresholdChecker
# ---------------------------------------------------------------------------

class EvaluationThresholdChecker:
    def __init__(
        self,
        threshold_loader: ThresholdLoader,
        environment: str = "prod",
        closed_network_mode: bool = False,
    ) -> None:
        self.loader = threshold_loader
        self.environment = environment
        self.closed_network_mode = closed_network_mode
        self.thresholds = threshold_loader.get_thresholds(
            environment=environment,
            closed_network_mode=closed_network_mode,
        )

    def check_metrics(
        self,
        evaluation_results: Dict[str, float],
        pr_number: Optional[int] = None,
        commit_sha: Optional[str] = None,
    ) -> ThresholdCheckReport:
        metric_results: List[MetricCheckResult] = []

        for metric_name, actual_value in evaluation_results.items():
            if metric_name not in self.thresholds:
                logger.warning("Unknown metric in results: %s", metric_name)
                continue

            threshold = self.thresholds[metric_name]
            direction = self.loader.get_metric_direction(metric_name)

            if direction == "higher_is_better":
                passed = actual_value >= threshold
                comparison = ">=" if passed else "<"
            else:
                passed = actual_value <= threshold
                comparison = "<=" if passed else ">"

            message = (
                f"{metric_name}: {actual_value:.4f} {comparison} {threshold:.4f} "
                f"({'PASS' if passed else 'FAIL'})"
            )
            metric_results.append(MetricCheckResult(
                metric_name=metric_name,
                actual_value=actual_value,
                threshold=threshold,
                direction=direction,
                passed=passed,
                message=message,
            ))

        passed_count = sum(1 for r in metric_results if r.passed)
        failed_count = len(metric_results) - passed_count
        overall_passed = failed_count == 0 and bool(metric_results)

        summary = self._summary(overall_passed, len(metric_results), passed_count, failed_count)
        detail = self._detail(metric_results)

        report = ThresholdCheckReport(
            overall_passed=overall_passed,
            total_metrics=len(metric_results),
            passed_metrics=passed_count,
            failed_metrics=failed_count,
            metric_results=metric_results,
            summary_message=summary,
            detailed_message=detail,
        )

        logger.info(
            "CI gate check: %s — %d/%d metrics passed (env=%s, closed_network=%s, pr=%s, sha=%s)",
            "PASS" if overall_passed else "FAIL",
            passed_count, len(metric_results),
            self.environment, self.closed_network_mode, pr_number, commit_sha,
        )
        return report

    @staticmethod
    def _summary(overall_passed: bool, total: int, passed: int, failed: int) -> str:
        status = "PASSED" if overall_passed else "FAILED"
        return f"{status} | {passed}/{total} metrics passed, {failed} failed"

    @staticmethod
    def _detail(results: List[MetricCheckResult]) -> str:
        lines = [
            "## AI Quality Evaluation Results",
            "",
            "| Metric | Actual | Threshold | Status |",
            "|--------|--------|-----------|--------|",
        ]
        for r in results:
            icon = "PASS" if r.passed else "FAIL"
            lines.append(
                f"| {r.metric_name} | {r.actual_value:.4f} | {r.threshold:.4f} | {icon} |"
            )
        return "\n".join(lines)
