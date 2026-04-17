"""
CI 게이트 임계값 검사 단위 테스트 — Phase 7 FG7.3 (task7-8)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.evaluation.ci_gate import (
    EvaluationThresholdChecker,
    EvaluationThresholdsConfig,
    MetricCheckResult,
    ThresholdLoader,
)
from scripts.check_evaluation_thresholds import load_evaluation_results


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_YAML_CONTENT = """
metrics:
  faithfulness:
    threshold: 0.80
    description: "충실도"
    direction: "higher_is_better"
    unit: "proportion"
  answer_relevance:
    threshold: 0.75
    description: "관련성"
    direction: "higher_is_better"
    unit: "proportion"
  context_precision:
    threshold: 0.75
    description: "정밀도"
    direction: "higher_is_better"
    unit: "proportion"
  context_recall:
    threshold: 0.75
    description: "재현율"
    direction: "higher_is_better"
    unit: "proportion"
  citation_present:
    threshold: 0.90
    description: "인용 포함율"
    direction: "higher_is_better"
    unit: "proportion"
  hallucination:
    threshold: 0.10
    description: "환각률"
    direction: "lower_is_better"
    unit: "proportion"

environments:
  dev:
    override:
      faithfulness: 0.75
      answer_relevance: 0.70
  staging:
    override: {}
  prod:
    override: {}

skip_conditions:
  override_labels:
    - "evaluation-override"
    - "ci-override"
    - "hotfix"
  skip_evaluation_file_patterns:
    - "docs/**"
    - "*.md"
"""


@pytest.fixture
def config_file(tmp_path) -> str:
    p = tmp_path / "thresholds.yaml"
    p.write_text(_YAML_CONTENT)
    return str(p)


@pytest.fixture
def loader(config_file) -> ThresholdLoader:
    return ThresholdLoader(config_file)


@pytest.fixture
def passing_results():
    return {
        "faithfulness": 0.85,
        "answer_relevance": 0.80,
        "context_precision": 0.78,
        "context_recall": 0.79,
        "citation_present": 0.95,
        "hallucination": 0.05,
    }


@pytest.fixture
def failing_results():
    return {
        "faithfulness": 0.75,      # < 0.80  FAIL
        "answer_relevance": 0.80,
        "context_precision": 0.72, # < 0.75  FAIL
        "context_recall": 0.79,
        "citation_present": 0.88,  # < 0.90  FAIL
        "hallucination": 0.12,     # > 0.10  FAIL
    }


# ---------------------------------------------------------------------------
# ThresholdLoader tests
# ---------------------------------------------------------------------------

class TestThresholdLoader:
    def test_load_returns_config(self, loader):
        cfg = loader.load()
        assert isinstance(cfg, EvaluationThresholdsConfig)
        assert "faithfulness" in cfg.metrics
        assert cfg.metrics["faithfulness"].threshold == 0.80

    def test_get_thresholds_prod(self, loader):
        t = loader.get_thresholds(environment="prod")
        assert t["faithfulness"] == 0.80
        assert t["hallucination"] == 0.10

    def test_get_thresholds_dev_override(self, loader):
        t = loader.get_thresholds(environment="dev")
        assert t["faithfulness"] == 0.75      # overridden
        assert t["answer_relevance"] == 0.70  # overridden
        assert t["hallucination"] == 0.10     # not overridden

    def test_get_metric_direction_higher(self, loader):
        assert loader.get_metric_direction("faithfulness") == "higher_is_better"

    def test_get_metric_direction_lower(self, loader):
        assert loader.get_metric_direction("hallucination") == "lower_is_better"

    def test_get_metric_direction_unknown_raises(self, loader):
        with pytest.raises(ValueError, match="Unknown metric"):
            loader.get_metric_direction("nonexistent")

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            ThresholdLoader("/nonexistent/path.yaml").load()

    def test_should_skip_override_label(self, loader):
        skip, reason = loader.should_skip_evaluation(["evaluation-override"], [])
        assert skip is True
        assert "Override label" in reason

    def test_should_skip_hotfix_label(self, loader):
        skip, _ = loader.should_skip_evaluation(["hotfix", "bugfix"], [])
        assert skip is True

    def test_should_not_skip_no_labels(self, loader):
        skip, _ = loader.should_skip_evaluation([], ["backend/app/foo.py"])
        assert skip is False

    def test_should_skip_docs_only_changes(self, loader):
        skip, reason = loader.should_skip_evaluation([], ["docs/guide.md", "README.md"])
        assert skip is True

    def test_should_not_skip_mixed_changes(self, loader):
        skip, _ = loader.should_skip_evaluation([], ["docs/guide.md", "backend/app/foo.py"])
        assert skip is False


# ---------------------------------------------------------------------------
# EvaluationThresholdChecker tests
# ---------------------------------------------------------------------------

class TestEvaluationThresholdChecker:
    def test_all_pass(self, loader, passing_results):
        checker = EvaluationThresholdChecker(loader, environment="prod")
        report = checker.check_metrics(passing_results)
        assert report.overall_passed is True
        assert report.passed_metrics == 6
        assert report.failed_metrics == 0

    def test_some_fail(self, loader, failing_results):
        checker = EvaluationThresholdChecker(loader, environment="prod")
        report = checker.check_metrics(failing_results)
        assert report.overall_passed is False
        assert report.failed_metrics >= 1
        failed_names = {r.metric_name for r in report.metric_results if not r.passed}
        assert "faithfulness" in failed_names
        assert "context_precision" in failed_names
        assert "citation_present" in failed_names
        assert "hallucination" in failed_names

    def test_boundary_values_pass(self, loader):
        checker = EvaluationThresholdChecker(loader, environment="prod")
        boundary = {
            "faithfulness": 0.80,       # == threshold
            "answer_relevance": 0.75,
            "context_precision": 0.75,
            "context_recall": 0.75,
            "citation_present": 0.90,
            "hallucination": 0.10,      # == threshold (lower_is_better)
        }
        report = checker.check_metrics(boundary)
        assert report.overall_passed is True

    def test_dev_environment_override(self, loader, failing_results):
        failing_results["faithfulness"] = 0.77  # passes dev threshold (0.75) but not prod (0.80)
        checker = EvaluationThresholdChecker(loader, environment="dev")
        report = checker.check_metrics(failing_results)
        faith = next(r for r in report.metric_results if r.metric_name == "faithfulness")
        assert faith.passed is True

    def test_closed_network_mode_does_not_crash(self, loader, passing_results):
        checker = EvaluationThresholdChecker(loader, environment="prod", closed_network_mode=True)
        report = checker.check_metrics(passing_results)
        assert report.overall_passed is True

    def test_unknown_metric_is_skipped(self, loader, passing_results):
        passing_results["unknown_metric"] = 0.99
        checker = EvaluationThresholdChecker(loader)
        report = checker.check_metrics(passing_results)
        # unknown_metric should not appear in results
        names = {r.metric_name for r in report.metric_results}
        assert "unknown_metric" not in names

    def test_summary_message_pass(self, loader, passing_results):
        checker = EvaluationThresholdChecker(loader)
        report = checker.check_metrics(passing_results)
        assert "PASSED" in report.summary_message

    def test_summary_message_fail(self, loader, failing_results):
        checker = EvaluationThresholdChecker(loader)
        report = checker.check_metrics(failing_results)
        assert "FAILED" in report.summary_message

    def test_detailed_message_format(self, loader, passing_results):
        checker = EvaluationThresholdChecker(loader)
        report = checker.check_metrics(passing_results)
        assert "AI Quality Evaluation Results" in report.detailed_message
        assert "|" in report.detailed_message
        assert "faithfulness" in report.detailed_message

    def test_empty_results_fails(self, loader):
        checker = EvaluationThresholdChecker(loader)
        report = checker.check_metrics({})
        assert report.overall_passed is False
        assert report.total_metrics == 0


# ---------------------------------------------------------------------------
# load_evaluation_results tests
# ---------------------------------------------------------------------------

class TestLoadEvaluationResults:
    def test_flat_metrics_key(self, tmp_path):
        data = {"metrics": {"faithfulness": 0.85, "answer_relevance": 0.80}}
        p = tmp_path / "report.json"
        p.write_text(json.dumps(data))
        results = load_evaluation_results(str(p))
        assert results["faithfulness"] == 0.85

    def test_nested_evaluation_results(self, tmp_path):
        data = {"evaluation_results": {"metrics": {"faithfulness": 0.85}}}
        p = tmp_path / "report.json"
        p.write_text(json.dumps(data))
        results = load_evaluation_results(str(p))
        assert results["faithfulness"] == 0.85

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_evaluation_results("/nonexistent/report.json")

    def test_invalid_format_raises(self, tmp_path):
        p = tmp_path / "report.json"
        p.write_text(json.dumps({"bad_key": {}}))
        with pytest.raises(ValueError, match="Invalid evaluation report format"):
            load_evaluation_results(str(p))
