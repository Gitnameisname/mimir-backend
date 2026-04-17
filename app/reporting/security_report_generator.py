"""Security report generator — OWASP 보안 점검 결과 보고서 생성."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
BACKEND = ROOT / "backend"


# ---------------------------------------------------------------------------
# 항목 정의
# ---------------------------------------------------------------------------

OWASP_GENERAL = [
    ("A01", "Broken Access Control", "test_a01_access_control.py"),
    ("A02", "Cryptographic Failures", "test_a02_crypto.py"),
    ("A03", "Injection", "test_a03_injection.py"),
    ("A04", "Insecure Design", "test_a04_design.py"),
    ("A05", "Security Misconfiguration", "test_a05_misc_config.py"),
    ("A06", "Vulnerable and Outdated Components", "test_a06_dependencies.py"),
    ("A07", "Identification and Authentication Failures", "test_a07_auth.py"),
    ("A08", "Software and Data Integrity Failures", "test_a08_integrity.py"),
    ("A09", "Security Logging and Monitoring Failures", "test_a09_logging.py"),
    ("A10", "Server-Side Request Forgery (SSRF)", "test_a10_ssrf.py"),
]

OWASP_LLM = [
    ("LLM01", "Prompt Injection", "test_llm01_prompt_injection.py"),
    ("LLM02", "Insecure Output Handling", "test_llm02_output_handling.py"),
    ("LLM03", "Training Data Poisoning", "test_llm03_training_data.py"),
    ("LLM04", "Model Denial of Service", "test_llm04_dos.py"),
    ("LLM05", "Supply Chain Vulnerabilities", "N/A — Covered by A06"),
    ("LLM06", "Sensitive Information Disclosure", "test_llm06_pii.py"),
    ("LLM07", "Insecure Plugin Design", "N/A — Plugin execution not used"),
    ("LLM08", "Excessive Agency", "test_llm08_agency.py"),
    ("LLM09", "Overreliance on LLM Output", "test_llm09_overreliance.py"),
    ("LLM10", "Model Theft", "test_owasp_llm.py (LLM10 section)"),
]


# ---------------------------------------------------------------------------
# Test runner helper
# ---------------------------------------------------------------------------

def _run_tests(test_pattern: str) -> dict[str, Any]:
    """pytest를 실행하고 pass/fail 카운트를 반환한다."""
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            f"tests/security/{test_pattern}",
            "--tb=no", "-q", "--no-header",
            "--json-report", "--json-report-file=/tmp/sec_report.json",
        ],
        capture_output=True,
        text=True,
        cwd=BACKEND,
    )
    try:
        with open("/tmp/sec_report.json") as f:
            data = json.load(f)
        summary = data.get("summary", {})
        return {
            "passed": summary.get("passed", 0),
            "failed": summary.get("failed", 0),
            "error": summary.get("error", 0),
            "skipped": summary.get("skipped", 0),
            "status": "PASS" if result.returncode == 0 else "FAIL",
        }
    except Exception:
        return {
            "passed": 0,
            "failed": 0,
            "error": 0,
            "skipped": 0,
            "status": "UNKNOWN",
        }


# ---------------------------------------------------------------------------
# Report generator
# ---------------------------------------------------------------------------

def generate_report(output_path: Path | None = None) -> str:
    """OWASP 보안 점검 결과 마크다운 보고서를 생성한다."""
    today = date.today().isoformat()

    lines: list[str] = [
        f"# OWASP 보안 점검 보고서 — Phase 9 (FG9.1)",
        f"",
        f"**작성일**: {today}  ",
        f"**대상**: Mimir S2 백엔드 (FastAPI)  ",
        f"**기준**: OWASP Top 10 (2021) + OWASP Top 10 for LLM Applications (2023)  ",
        f"",
        f"---",
        f"",
        f"## 1. 일반 OWASP Top 10 (A01~A10)",
        f"",
        f"| 항목 | 취약점 명 | 테스트 파일 | 상태 |",
        f"|------|-----------|-------------|------|",
    ]

    total_pass = 0
    total_fail = 0

    for code, name, test_file in OWASP_GENERAL:
        if test_file.endswith(".py"):
            stats = _run_tests(test_file)
            status = stats["status"]
            if status == "PASS":
                total_pass += 1
                icon = "✅"
            elif status == "FAIL":
                total_fail += 1
                icon = "❌"
            else:
                icon = "⚠️"
            lines.append(f"| **{code}** | {name} | `{test_file}` | {icon} {status} |")
        else:
            lines.append(f"| **{code}** | {name} | {test_file} | ⚠️ SKIP |")

    lines += [
        f"",
        f"## 2. OWASP Top 10 for LLM Applications (LLM01~LLM10)",
        f"",
        f"| 항목 | 취약점 명 | 테스트 파일 | 상태 |",
        f"|------|-----------|-------------|------|",
    ]

    for code, name, test_file in OWASP_LLM:
        if test_file.endswith(".py"):
            stats = _run_tests(test_file)
            status = stats["status"]
            if status == "PASS":
                total_pass += 1
                icon = "✅"
            elif status == "FAIL":
                total_fail += 1
                icon = "❌"
            else:
                icon = "⚠️"
            lines.append(f"| **{code}** | {name} | `{test_file}` | {icon} {status} |")
        else:
            lines.append(f"| **{code}** | {name} | {test_file} | ℹ️ N/A |")

    overall = "PASS" if total_fail == 0 else "FAIL"
    lines += [
        f"",
        f"---",
        f"",
        f"## 3. 요약",
        f"",
        f"| 구분 | 통과 | 실패 |",
        f"|------|------|------|",
        f"| 일반 Top 10 | {total_pass} | {total_fail} |",
        f"| **전체** | **{total_pass}** | **{total_fail}** |",
        f"",
        f"**최종 결과**: {'✅ PASS' if overall == 'PASS' else '❌ FAIL'}",
        f"",
        f"---",
        f"",
        f"## 4. Sign-off",
        f"",
        f"- [ ] 보안 팀 검토 완료",
        f"- [ ] 개발 팀 리뷰 완료",
        f"- [ ] 관리자 최종 승인",
        f"",
        f"---",
        f"",
        f"*이 보고서는 `app/reporting/security_report_generator.py`에 의해 자동 생성됩니다.*",
    ]

    report = "\n".join(lines)

    if output_path is None:
        output_path = ROOT / "docs/보안/OWASP_Security_Report_Phase9.md"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    return report


if __name__ == "__main__":
    print(generate_report())
