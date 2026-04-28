"""
S3 Phase 4 FG 4-0 §2.1.6.e: ScopeProfile.allowed_tools 시드 / 일회성 마이그레이션.

Alembic revision `s3_p4_scope_profile_allow_tools` 가 컬럼을 추가하고 기존 행을
빈 배열로 backfill 한다 (default-deny). 본 스크립트는 그 후 운영자 선택으로
실행하는 **일회성 부트스트랩** 도구다.

옵션
----

--dry-run (기본):
    변경 없이 영향 받을 ScopeProfile 수만 출력.

--bootstrap-with-l0:
    risk_tier == "L0" 도구를 모든 *기존* ScopeProfile (적용 시점 존재) 에 등록.
    신규 ScopeProfile 에는 적용하지 않음 — 운영자가 명시 등록 필요.
    보안 트레이드오프: 즉시 운영 재개 가능하나 default-deny 보안 보장 약화.

--bootstrap-deny:
    명시적으로 빈 배열 [] 로 다시 reset (default-deny 보장 — backfill 검증용).

--profile <id>:
    특정 ScopeProfile 만 대상.

사용법
------

    cd backend
    python scripts/migrate_scope_profile_allowed_tools.py --dry-run
    python scripts/migrate_scope_profile_allowed_tools.py --bootstrap-with-l0
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.db.connection import get_db  # noqa: E402
from app.repositories.scope_profile_repository import ScopeProfileRepository  # noqa: E402
from app.schemas.mcp import TOOL_SCHEMAS, is_tool_mcp_exposed  # noqa: E402


def _l0_tools() -> list[str]:
    """risk_tier=L0 + manifest 노출 도구 이름."""
    return sorted(
        s["name"]
        for s in TOOL_SCHEMAS
        if s.get("risk_tier") == "L0" and is_tool_mcp_exposed(s)
    )


def _select_target_profile_ids(conn, profile_id: Optional[str]) -> list[str]:
    """대상 프로파일 id 목록."""
    if profile_id:
        return [profile_id]
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM scope_profiles ORDER BY created_at")
        return [str(r["id"]) for r in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description="ScopeProfile.allowed_tools 시드")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="변경 없이 영향 출력 (기본)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bootstrap-with-l0", action="store_true",
                       help="모든 기존 ScopeProfile 에 L0 도구 등록")
    group.add_argument("--bootstrap-deny", action="store_true",
                       help="모든 기존 ScopeProfile 의 allowed_tools 를 [] 로 reset")
    parser.add_argument("--profile", type=str, default=None,
                        help="특정 ScopeProfile id 만 대상 (옵션)")
    parser.add_argument("--apply", action="store_true",
                        help="실제 적용 — --bootstrap-* 와 함께 사용")
    args = parser.parse_args()

    apply = args.apply and (args.bootstrap_with_l0 or args.bootstrap_deny)

    target_tools: Optional[list[str]] = None
    mode_label = "DRY-RUN"
    if args.bootstrap_with_l0:
        target_tools = _l0_tools()
        mode_label = "BOOTSTRAP-L0"
    elif args.bootstrap_deny:
        target_tools = []
        mode_label = "BOOTSTRAP-DENY"

    print(f"[mode] {mode_label} {'(apply)' if apply else '(dry-run)'}")
    if target_tools is not None:
        print(f"[target_tools] {target_tools}")

    with get_db() as conn:
        ids = _select_target_profile_ids(conn, args.profile)
        print(f"[targets] {len(ids)} ScopeProfile(s)")

        repo = ScopeProfileRepository(conn)
        modified = 0
        for pid in ids:
            existing = repo.get_by_id(pid)
            if existing is None:
                print(f"  - {pid} : (없음, skip)")
                continue
            current = sorted(existing.allowed_tools or [])
            if target_tools is None:
                # dry-run only — print 현재 상태
                print(f"  - {pid} : current={current}")
                continue
            new_tools = sorted(target_tools)
            if current == new_tools:
                print(f"  - {pid} : same — skip")
                continue
            print(f"  - {pid} : {current} → {new_tools}")
            if apply:
                repo.update(pid, allowed_tools=new_tools)
                modified += 1

        if apply:
            print(f"[modified] {modified} profile(s)")
            return 0
        if target_tools is not None:
            print("[hint] --apply 를 함께 사용해야 실제 적용됩니다.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
