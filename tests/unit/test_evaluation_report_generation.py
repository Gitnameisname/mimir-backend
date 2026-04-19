"""
AI품질평가 보고서 생성 단위 테스트 — Phase 7 FG7.3 (task7-9)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.generate_evaluation_report import (
    EvalItem,
    EvaluationReportGenerator,
    MetricResult,
    ReportData,
    ReportInsights,
    _report_to_metadata,
    load_report_from_json,
    update_evaluation_metadata,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc).isoformat()

_PASSING_METRICS = {
    "faithfulness": MetricResult(value=0.85, passed=True),
    "answer_relevance": MetricResult(value=0.80, passed=True),
    "context_precision": MetricResult(value=0.78, passed=True),
    "context_recall": MetricResult(value=0.79, passed=True),
    "citation_present": MetricResult(value=0.95, passed=True),
    "hallucination": MetricResult(value=0.05, passed=True),
}

_FAILING_METRICS = {
    "faithfulness": MetricResult(value=0.75, passed=False),
    "answer_relevance": MetricResult(value=0.80, passed=True),
    "context_precision": MetricResult(value=0.70, passed=False),
    "context_recall": MetricResult(value=0.79, passed=True),
    "citation_present": MetricResult(value=0.88, passed=False),
    "hallucination": MetricResult(value=0.12, passed=False),
}


def _make_report(metrics=None, *, overall_passed=True, items=None, insights=None) -> ReportData:
    m = metrics or _PASSING_METRICS
    total = len(m)
    passed = sum(1 for v in m.values() if v.passed)
    return ReportData(
        golden_set_id="test-set",
        prompt_version="v1.0",
        model="gpt-4o",
        timestamp=_NOW,
        environment="prod",
        metrics=m,
        overall_passed=overall_passed,
        total_metrics=total,
        passed_metrics=passed,
        failed_metrics=total - passed,
        evaluation_items=items or [],
        insights=insights or ReportInsights(),
        closed_network_mode=False,
        commit_sha="abc123",
        pr_number=42,
        backend_url="http://localhost:8050",
    )


# ---------------------------------------------------------------------------
# load_report_from_json
# ---------------------------------------------------------------------------

class TestLoadReportFromJson:
    def test_loads_flat_metrics(self, tmp_path):
        data = {
            "golden_set_id": "gs1",
            "prompt_version": "v1",
            "model": "gpt-4",
            "metrics": {
                "faithfulness": {"value": 0.85, "passed": True},
                "answer_relevance": {"value": 0.80, "passed": True},
                "context_precision": {"value": 0.78, "passed": True},
                "context_recall": {"value": 0.79, "passed": True},
                "citation_present": {"value": 0.95, "passed": True},
                "hallucination": {"value": 0.05, "passed": True},
            },
        }
        p = tmp_path / "r.json"
        p.write_text(json.dumps(data))
        report = load_report_from_json(str(p))
        assert report.golden_set_id == "gs1"
        assert report.metrics["faithfulness"].value == 0.85
        assert report.metrics["faithfulness"].passed is True

    def test_loads_evaluation_items(self, tmp_path):
        data = {
            "metrics": {
                "faithfulness": {"value": 0.8, "passed": True},
                "answer_relevance": {"value": 0.76, "passed": True},
                "context_precision": {"value": 0.75, "passed": True},
                "context_recall": {"value": 0.75, "passed": True},
                "citation_present": {"value": 0.90, "passed": True},
                "hallucination": {"value": 0.10, "passed": True},
            },
            "evaluation_items": [
                {
                    "question": "What is Python?",
                    "expected_answer": "A programming language.",
                    "generated_answer": "Python is a language.",
                    "faithfulness": 0.9,
                }
            ],
        }
        p = tmp_path / "r.json"
        p.write_text(json.dumps(data))
        report = load_report_from_json(str(p))
        assert len(report.evaluation_items) == 1
        assert report.evaluation_items[0].question == "What is Python?"
        assert report.evaluation_items[0].faithfulness == 0.9

    def test_missing_fields_use_defaults(self, tmp_path):
        data = {"metrics": {
            "faithfulness": {"value": 0.8, "passed": True},
            "answer_relevance": {"value": 0.76, "passed": True},
            "context_precision": {"value": 0.75, "passed": True},
            "context_recall": {"value": 0.75, "passed": True},
            "citation_present": {"value": 0.90, "passed": True},
            "hallucination": {"value": 0.10, "passed": True},
        }}
        p = tmp_path / "r.json"
        p.write_text(json.dumps(data))
        report = load_report_from_json(str(p))
        assert report.golden_set_id == "unknown"
        assert report.commit_sha is None

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_report_from_json("/nonexistent/report.json")


# ---------------------------------------------------------------------------
# EvaluationReportGenerator
# ---------------------------------------------------------------------------

class TestEvaluationReportGenerator:
    @pytest.fixture
    def generator(self, tmp_path):
        # Use a minimal inline template to avoid depending on the real template file
        tdir = tmp_path / "templates"
        tdir.mkdir()
        (tdir / "ai_quality_evaluation.md.jinja2").write_text(
            "# Report\n"
            "GoldenSet: {{ report.golden_set_id }}\n"
            "Passed: {{ report.overall_passed }}\n"
            "{% for mn, mr in report.metrics.items() %}"
            "- {{ mn }}: {{ '%.4f'|format(mr.value) }}\n"
            "{% endfor %}"
            "{% if previous_report %}Prev: {{ previous_report.golden_set_id }}{% endif %}\n"
            "{% if dashboard_url %}Dashboard: {{ dashboard_url }}{% endif %}\n",
            encoding="utf-8",
        )
        from app.services.evaluation.ci_gate import ThresholdLoader
        # Use real config file
        config_path = str(
            Path(__file__).parent.parent.parent / "config" / "evaluation_thresholds.yaml"
        )
        gen = EvaluationReportGenerator(
            template_dir=str(tdir),
            config_path=config_path,
        )
        return gen

    def test_generate_creates_file(self, generator, tmp_path):
        report = _make_report()
        out = tmp_path / "AI품질평가.md"
        result = generator.generate(report, out)
        assert result.exists()

    def test_generate_contains_golden_set_id(self, generator, tmp_path):
        report = _make_report()
        out = tmp_path / "report.md"
        generator.generate(report, out)
        content = out.read_text(encoding="utf-8")
        assert "test-set" in content

    def test_generate_contains_all_metrics(self, generator, tmp_path):
        report = _make_report()
        out = tmp_path / "report.md"
        generator.generate(report, out)
        content = out.read_text(encoding="utf-8")
        for name in _PASSING_METRICS:
            assert name in content

    def test_generate_with_previous_report(self, generator, tmp_path):
        curr = _make_report()
        prev = _make_report(metrics={
            "faithfulness": MetricResult(value=0.80, passed=True),
            "answer_relevance": MetricResult(value=0.75, passed=True),
            "context_precision": MetricResult(value=0.75, passed=True),
            "context_recall": MetricResult(value=0.75, passed=True),
            "citation_present": MetricResult(value=0.91, passed=True),
            "hallucination": MetricResult(value=0.09, passed=True),
        })
        out = tmp_path / "report.md"
        generator.generate(curr, out, previous_report=prev)
        content = out.read_text(encoding="utf-8")
        assert "Prev:" in content

    def test_generate_with_dashboard_url(self, generator, tmp_path):
        report = _make_report()
        out = tmp_path / "report.md"
        generator.generate(report, out, dashboard_url="http://dashboard.example.com")
        content = out.read_text(encoding="utf-8")
        assert "http://dashboard.example.com" in content

    def test_generate_metadata(self, generator, tmp_path):
        report = _make_report()
        out = tmp_path / "meta.json"
        result = generator.generate_metadata(report, out)
        assert result.exists()
        meta = json.loads(out.read_text(encoding="utf-8"))
        assert meta["golden_set_id"] == "test-set"
        assert meta["overall_passed"] is True
        assert "faithfulness" in meta["metrics"]
        assert meta["metrics"]["faithfulness"]["value"] == 0.85

    def test_generate_failing_report(self, generator, tmp_path):
        report = _make_report(metrics=_FAILING_METRICS, overall_passed=False)
        out = tmp_path / "report.md"
        generator.generate(report, out)
        content = out.read_text(encoding="utf-8")
        assert "Passed: False" in content

    def test_report_with_evaluation_items(self, generator, tmp_path):
        items = [
            EvalItem(
                question="What is Docker?",
                expected_answer="A container platform.",
                generated_answer="Docker is for containers.",
                faithfulness=0.9,
            )
        ]
        report = _make_report(items=items)
        out = tmp_path / "report.md"
        generator.generate(report, out)
        content = out.read_text(encoding="utf-8")
        assert "test-set" in content  # report renders without error


# ---------------------------------------------------------------------------
# update_evaluation_metadata
# ---------------------------------------------------------------------------

class TestUpdateEvaluationMetadata:
    def test_creates_new_file(self, tmp_path):
        meta = {"golden_set_id": "gs1", "overall_passed": True, "timestamp": _NOW}
        update_evaluation_metadata(str(tmp_path), meta)
        p = tmp_path / "evaluation_metadata.json"
        assert p.exists()
        data = json.loads(p.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["golden_set_id"] == "gs1"

    def test_appends_to_existing(self, tmp_path):
        for i in range(3):
            update_evaluation_metadata(str(tmp_path), {"run": i, "timestamp": _NOW})
        p = tmp_path / "evaluation_metadata.json"
        data = json.loads(p.read_text())
        assert len(data) == 3

    def test_caps_at_100(self, tmp_path):
        for i in range(105):
            update_evaluation_metadata(str(tmp_path), {"run": i})
        p = tmp_path / "evaluation_metadata.json"
        data = json.loads(p.read_text())
        assert len(data) == 100
        assert data[0]["run"] == 5  # oldest 5 dropped

    def test_handles_corrupt_file(self, tmp_path):
        p = tmp_path / "evaluation_metadata.json"
        p.write_text("not valid json")
        # Should not raise — just start fresh
        update_evaluation_metadata(str(tmp_path), {"run": 0})
        data = json.loads(p.read_text())
        assert len(data) == 1


# ---------------------------------------------------------------------------
# _report_to_metadata
# ---------------------------------------------------------------------------

class TestReportToMetadata:
    def test_all_fields_present(self):
        report = _make_report()
        meta = _report_to_metadata(report, "prod")
        for key in ("golden_set_id", "prompt_version", "model", "timestamp",
                    "environment", "closed_network_mode", "metrics",
                    "overall_passed", "total_metrics", "passed_metrics",
                    "failed_metrics", "evaluation_items_count",
                    "commit_sha", "pr_number", "backend_url"):
            assert key in meta, f"Missing key: {key}"

    def test_metric_structure(self):
        report = _make_report()
        meta = _report_to_metadata(report, "prod")
        faith = meta["metrics"]["faithfulness"]
        assert faith["value"] == 0.85
        assert faith["passed"] is True

    def test_environment_is_set(self):
        report = _make_report()
        meta = _report_to_metadata(report, "dev")
        assert meta["environment"] == "dev"
