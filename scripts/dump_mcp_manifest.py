"""
S3 Phase 4 FG 4-0 §2.1.5: MCP 도구 manifest 추출 스크립트.

`app.schemas.mcp.TOOL_SCHEMAS` 를 읽어 manifest JSON 을
`docs/개발문서/S3/phase4/산출물/MCP_도구_매니페스트.json` 으로 출력한다.

CI 에서 이 파일을 commit 상태와 비교해 drift 감지 (FG 4-4 에서 통합).

본 스크립트의 출력은 코드 상수만 의존 — 환경 무관 (DB / 외부 서비스 무관).
환경별 차이가 발견되면 즉시 정책 위반.

사용법
------

    cd backend
    python scripts/dump_mcp_manifest.py [--out PATH]

기본 출력 경로는 docs/개발문서/S3/phase4/산출물/MCP_도구_매니페스트.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 프로젝트 루트 — backend/ 의 상위
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.schemas.mcp import (  # noqa: E402
    MIMIR_EXTENSIONS,
    TOOL_SCHEMAS,
    is_tool_mcp_exposed,
    known_tool_names,
)


_DEFAULT_OUT = ROOT / "docs/개발문서/S3/phase4/산출물/MCP_도구_매니페스트.json"


def build_manifest() -> dict:
    """TOOL_SCHEMAS + manifest 메타를 구조화한 dict 반환.

    Drift 감지 용이하도록 도구는 name 기준 정렬.
    """
    sorted_tools = sorted(TOOL_SCHEMAS, key=lambda s: s["name"])
    return {
        "schema_version": "1.1",  # FG 4-5: 5 신규 필드 추가
        "generated_by": "backend/scripts/dump_mcp_manifest.py",
        "fg": "S3 Phase 4 FG 4-0 §2.1.5 + FG 4-5",
        "tools": [
            {
                "name": s["name"],
                "description": s.get("description", ""),
                "risk_tier": s.get("risk_tier"),
                "maturity": s.get("maturity"),
                "status": s.get("status"),
                "exposure_policy": s.get("exposure_policy"),
                # FG 4-5 (2026-04-28): capability manifest 확장
                "default_enabled": s.get("default_enabled"),
                "requires": s.get("requires", []),
                "preferred_use": s.get("preferred_use"),
                "policy_profile": s.get("policy_profile"),
                "streaming_supported": s.get("streaming_supported"),
                "is_mcp_exposed": is_tool_mcp_exposed(s),
                "authentication": s.get("authentication", {}),
            }
            for s in sorted_tools
        ],
        "extensions": MIMIR_EXTENSIONS,
        "totals": {
            "all": len(TOOL_SCHEMAS),
            "mcp_exposed": sum(1 for s in TOOL_SCHEMAS if is_tool_mcp_exposed(s)),
            "by_risk_tier": _count_by_key(TOOL_SCHEMAS, "risk_tier"),
            "by_maturity": _count_by_key(TOOL_SCHEMAS, "maturity"),
            "by_status": _count_by_key(TOOL_SCHEMAS, "status"),
            "by_policy_profile": _count_by_key(TOOL_SCHEMAS, "policy_profile"),
            "default_enabled_count": sum(
                1 for s in TOOL_SCHEMAS if s.get("default_enabled", False)
            ),
        },
        "known_tool_names": sorted(known_tool_names()),
    }


def _count_by_key(items: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in items:
        v = s.get(key)
        if v is None:
            continue
        counts[v] = counts.get(v, 0) + 1
    return dict(sorted(counts.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP 도구 manifest JSON 추출")
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help=f"출력 경로 (기본: {_DEFAULT_OUT.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "기존 파일과 차이 검사 (drift 감지). "
            "차이 발견 시 종료 코드 1, 동일 시 0. CI 게이트 용도."
        ),
    )
    args = parser.parse_args()

    manifest = build_manifest()
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"

    if args.check:
        if not args.out.exists():
            print(f"[FAIL] 비교 대상 파일이 없습니다: {args.out}", file=sys.stderr)
            return 1
        existing = args.out.read_text(encoding="utf-8")
        if existing != rendered:
            print(
                f"[FAIL] manifest drift 감지 — 코드와 {args.out.relative_to(ROOT)} 가 다릅니다. "
                f"`python scripts/dump_mcp_manifest.py` 를 다시 실행하고 결과를 commit 하세요.",
                file=sys.stderr,
            )
            return 1
        print(f"[OK] manifest 정합 — {args.out.relative_to(ROOT)}")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"[OK] {args.out.relative_to(ROOT)} 작성 — 도구 {manifest['totals']['all']}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
