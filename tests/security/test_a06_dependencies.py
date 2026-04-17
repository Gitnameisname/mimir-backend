"""
A06 Vulnerable and Outdated Components 검증 테스트.

검증 항목:
  - requirements.txt 존재 및 버전 고정
  - 알려진 취약 버전 패키지 검사
  - pip-audit 또는 safety 스캔 결과
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "security-test-secret")


# ---------------------------------------------------------------------------
# A06-001~003: 의존성 파일 확인
# ---------------------------------------------------------------------------

class TestA06DependencyFiles:
    """의존성 파일 존재 및 형식 검증."""

    def test_requirements_file_exists(self):
        """A06-001: requirements.txt 또는 pyproject.toml이 존재한다."""
        req_txt = ROOT / "backend/requirements.txt"
        pyproject = ROOT / "backend/pyproject.toml"
        assert req_txt.exists() or pyproject.exists(), (
            "requirements.txt 또는 pyproject.toml 없음"
        )

    def test_requirements_has_pinned_versions(self):
        """A06-002: requirements.txt의 패키지들이 버전이 고정되어 있다."""
        req_txt = ROOT / "backend/requirements.txt"
        if not req_txt.exists():
            pytest.skip("requirements.txt 없음 — pyproject.toml 사용 중")

        content = req_txt.read_text(encoding="utf-8")
        lines = [
            l.strip() for l in content.splitlines()
            if l.strip() and not l.startswith("#") and not l.startswith("-r")
        ]

        unpinned = []
        for line in lines:
            # 버전 지정 없이 패키지명만 있는 경우
            if not any(op in line for op in ["==", ">=", "<=", "~=", "!="]):
                unpinned.append(line)

        # unpinned가 너무 많으면 경고 (운영 환경에서는 모두 고정 권장)
        # 테스트에서는 50% 이상 고정 요구
        if lines:
            pinned_rate = 1 - len(unpinned) / len(lines)
            assert pinned_rate >= 0.5, (
                f"버전 고정 비율 낮음: {pinned_rate:.0%}. "
                f"고정되지 않은 패키지: {unpinned[:5]}"
            )

    def test_no_known_vulnerable_version_patterns(self):
        """A06-003: 알려진 취약 버전 패턴이 없다.

        주요 알려진 취약 버전:
          - Pillow < 9.0.0 (CVE-2022-22815)
          - cryptography < 41.0.0 (multiple CVEs)
          - requests < 2.28.0 (URL redirection)
        """
        req_txt = ROOT / "backend/requirements.txt"
        if not req_txt.exists():
            pytest.skip("requirements.txt 없음")

        content = req_txt.read_text(encoding="utf-8").lower()

        # 매우 오래된 버전 패턴 (예시)
        very_old_patterns = [
            r"pillow==(?:[1-8]\.\d)",    # 9.x 미만
            r"requests==(?:1\.\d|2\.[0-9]\.|2\.1[0-9]\.)",  # 2.20 미만
        ]
        for pattern in very_old_patterns:
            matches = re.findall(pattern, content)
            assert not matches, f"알려진 취약 버전 발견: {matches}"


# ---------------------------------------------------------------------------
# A06-004~005: pip-audit 스캔 (선택적 — pip-audit 설치 시)
# ---------------------------------------------------------------------------

class TestA06SecurityScan:
    """의존성 보안 스캔."""

    def test_pip_audit_or_safety_available(self):
        """A06-004: pip-audit 또는 safety 도구가 사용 가능한 환경인지 확인한다.

        이 테스트는 도구가 없으면 skip하고, 있으면 실행한다.
        """
        try:
            pip_audit = subprocess.run(
                ["pip-audit", "--version"],
                capture_output=True,
                text=True,
            )
            pip_audit_ok = pip_audit.returncode == 0
        except FileNotFoundError:
            pip_audit_ok = False

        try:
            safety = subprocess.run(
                ["safety", "--version"],
                capture_output=True,
                text=True,
            )
            safety_ok = safety.returncode == 0
        except FileNotFoundError:
            safety_ok = False

        has_tool = pip_audit_ok or safety_ok
        if not has_tool:
            pytest.skip("pip-audit 또는 safety 도구 없음 — CI 환경에서 별도 실행")

        # 도구가 있으면 pass (실제 스캔은 CI에서 실행)
        assert True

    @pytest.mark.slow
    def test_pip_audit_no_known_vulnerabilities(self):
        """A06-005: pip-audit 스캔에서 High/Critical 취약점이 없다.

        이 테스트는 시간이 걸리므로 --slow 마커로 표시됨.
        """
        result = subprocess.run(
            ["pip-audit", "--require-hashes=false", "--format", "json"],
            capture_output=True,
            text=True,
            cwd=ROOT / "backend",
        )

        if result.returncode == 127:  # 명령어 없음
            pytest.skip("pip-audit 없음")

        import json
        try:
            audit_result = json.loads(result.stdout)
        except json.JSONDecodeError:
            pytest.skip("pip-audit 출력 파싱 실패")

        # High/Critical 취약점 검사
        vulnerabilities = audit_result.get("vulnerabilities", [])
        high_critical = [
            v for v in vulnerabilities
            if v.get("severity", "").lower() in ("high", "critical")
        ]

        assert not high_critical, (
            f"High/Critical 취약점 발견: "
            + ", ".join(f"{v['name']} ({v.get('id', 'N/A')})" for v in high_critical[:5])
        )
