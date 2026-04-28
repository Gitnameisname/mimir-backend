"""
S3 Phase 4 FG 4-4 §2.1.6 — Manifest drift 게이트.

`scripts/dump_mcp_manifest.py` 의 출력 (`docs/.../MCP_도구_매니페스트.json`) 이
코드 상수와 동기화되어 있는지 자동 검증. CI 게이트로 등록 시 머지 차단.

본 테스트는 실 파일 시스템 비교 — 운영자가 매번 manifest 변경 시 dump 후 commit.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
MANIFEST_PATH = ROOT / "docs/개발문서/S3/phase4/산출물/MCP_도구_매니페스트.json"
DUMP_SCRIPT = ROOT / "backend/scripts/dump_mcp_manifest.py"


class TestManifestDrift:
    def test_manifest_file_exists(self):
        assert MANIFEST_PATH.exists(), (
            f"manifest JSON 미존재 — `python {DUMP_SCRIPT.relative_to(ROOT)}` 실행 후 commit 필요"
        )

    def test_dump_script_exists(self):
        assert DUMP_SCRIPT.exists()

    def test_manifest_in_sync_with_code(self):
        """`dump_mcp_manifest.py --check` 가 exit 0 (drift 없음)."""
        result = subprocess.run(
            [sys.executable, str(DUMP_SCRIPT), "--check"],
            cwd=str(ROOT / "backend"),
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"manifest drift 감지 — `python scripts/dump_mcp_manifest.py` 실행 후 결과를 commit 하세요.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_manifest_includes_all_exposed_tools(self):
        """현재 노출 도구 9개 모두 manifest 에 포함 (FG 4-6 save_draft 추가).

        도구 추가 시 본 expected set 갱신 의무 — manifest drift 게이트의 일부.
        """
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        names = {t["name"] for t in data["tools"]}
        expected = {
            "search_documents",
            "fetch_node",
            "verify_citation",
            "mimir.vectorization.status",
            "read_annotations",
            "search_nodes",
            "read_document_render",
            "resolve_document_reference",
            # FG 4-6 (2026-04-28): L2 write 도구
            "save_draft",
        }
        assert names == expected, f"manifest 도구 집합 불일치: {names ^ expected}"

    def test_manifest_no_l4_tools(self):
        """manifest 의 어떤 도구도 risk_tier=L4 가 아님 (R1)."""
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        l4 = [t for t in data["tools"] if t.get("risk_tier") == "L4"]
        assert l4 == [], f"L4 도구 manifest 등재 — R1 위반: {[t['name'] for t in l4]}"

    def test_manifest_all_mcp_exposed_true(self):
        """현재 manifest 의 모든 도구가 is_mcp_exposed=True (운영 정책)."""
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        not_exposed = [t for t in data["tools"] if not t.get("is_mcp_exposed", True)]
        assert not_exposed == [], (
            f"manifest 에 비노출 도구 등재 — TOOL_SCHEMAS 정책 위반: {[t['name'] for t in not_exposed]}"
        )
