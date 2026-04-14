"""
Seed 스크립트 — 초기 관리자(admin) 및 테스트용 사용자 계정을 생성한다.

사용법:
    cd backend
    python -m scripts.seed_users

특징:
  - 멱등성(idempotent): 이미 존재하는 계정은 건너뛴다.
  - 환경변수로 비밀번호를 재정의할 수 있다.
      SEED_ADMIN_EMAIL, SEED_ADMIN_PASSWORD, SEED_ADMIN_NAME
      SEED_USER_EMAIL,  SEED_USER_PASSWORD,  SEED_USER_NAME, SEED_USER_ROLE
  - 비밀번호는 bcrypt (app.api.auth.password.hash_password)로 해시.
  - email_verified=True 로 생성하여 로그인 즉시 사용 가능.

⚠️ 보안 주의:
  - 개발/테스트 환경 전용. 운영 환경에서는 환경변수로 반드시 강력한
    비밀번호를 주입해서 사용하고, 실행 후 기본 비밀번호를 즉시 교체할 것.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Optional

# app 패키지 import 가능하도록 backend/ 루트를 path 에 포함
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.dirname(_HERE)
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.api.auth.password import hash_password  # noqa: E402
from app.db.connection import get_db  # noqa: E402
from app.repositories.users_repository import UsersRepository  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("seed_users")


@dataclass(frozen=True)
class SeedAccount:
    email: str
    username: str
    display_name: str
    password: str
    role_name: str  # VIEWER / AUTHOR / REVIEWER / APPROVER / ORG_ADMIN / SUPER_ADMIN


def _build_seed_accounts() -> list[SeedAccount]:
    """환경변수 기반으로 시드 계정 목록을 생성한다."""
    admin = SeedAccount(
        email=os.environ.get("SEED_ADMIN_EMAIL", "admin@mimir.local"),
        # 'admin' 은 validate_username 예약어이므로 기본값은 'administrator' 사용
        username=os.environ.get("SEED_ADMIN_USERNAME", "administrator"),
        display_name=os.environ.get("SEED_ADMIN_NAME", "관리자"),
        password=os.environ.get("SEED_ADMIN_PASSWORD", "Admin!2345"),
        role_name="SUPER_ADMIN",
    )
    test_user = SeedAccount(
        email=os.environ.get("SEED_USER_EMAIL", "user@mimir.local"),
        username=os.environ.get("SEED_USER_USERNAME", "testuser"),
        display_name=os.environ.get("SEED_USER_NAME", "테스트 사용자"),
        password=os.environ.get("SEED_USER_PASSWORD", "User!2345"),
        role_name=os.environ.get("SEED_USER_ROLE", "AUTHOR"),
    )
    return [admin, test_user]


def _upsert(repo: UsersRepository, conn, account: SeedAccount) -> str:
    """
    email 기준으로 존재 여부를 확인하고, 없으면 생성.
    이미 존재하는 경우:
      - role_name, email_verified 만 필요 시 업데이트
      - 비밀번호는 덮어쓰지 않음(사용자 직접 변경 가능성 보호)
    반환: "created" | "updated" | "skipped"
    """
    existing = repo.get_by_email(conn, account.email)
    if existing is None:
        # username 충돌 방지: 다른 사용자가 이미 username 을 선점한 경우 빈 값으로 생성
        username = account.username
        if username and repo.get_by_username(conn, username) is not None:
            logger.warning(
                "username '%s' already taken by another user — seeding without username",
                username,
            )
            username = None
        repo.create(
            conn,
            email=account.email,
            display_name=account.display_name,
            role_name=account.role_name,
            status="ACTIVE",
            password_hash=hash_password(account.password),
            auth_provider="local",
            email_verified=True,
            username=username,
        )
        return "created"

    # 이미 있으면 role / email_verified / username 만 보정
    changes: dict = {}
    if existing.role_name != account.role_name:
        changes["role_name"] = account.role_name
    if not existing.email_verified:
        changes["email_verified"] = True
    if account.username and not existing.username:
        # 타 사용자와 충돌하지 않을 때만 세팅
        holder = repo.get_by_username(conn, account.username)
        if holder is None or holder.id == existing.id:
            changes["username"] = account.username

    if changes:
        repo.update(conn, existing.id, **changes)
        return "updated"
    return "skipped"


def main() -> int:
    accounts = _build_seed_accounts()
    repo = UsersRepository()

    results: list[tuple[SeedAccount, str]] = []
    try:
        with get_db() as conn:
            for acc in accounts:
                action = _upsert(repo, conn, acc)
                results.append((acc, action))
    except Exception as e:
        logger.exception("seed_users failed: %s", e)
        return 1

    # 결과 출력 (비밀번호는 생성된 계정에 대해서만 출력)
    print("\n=== Seed Users 결과 ===")
    for acc, action in results:
        if action == "created":
            print(
                f"[CREATED ] email={acc.email}  username={acc.username}  "
                f"role={acc.role_name}  password={acc.password}"
            )
        elif action == "updated":
            print(
                f"[UPDATED ] email={acc.email}  username={acc.username}  "
                f"role={acc.role_name}  (비밀번호 변경 없음)"
            )
        else:
            print(f"[SKIPPED ] email={acc.email}  username={acc.username}  (이미 최신 상태)")
    print("=======================\n")
    print("⚠️  기본 비밀번호가 출력된 경우, 로그인 후 즉시 변경하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
