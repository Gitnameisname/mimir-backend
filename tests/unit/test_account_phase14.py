"""
Task 14-7 Account API 검증.

FastAPI를 임포트하지 않고 핵심 로직을 독립적으로 검증한다.
"""

import os

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONTEND = os.path.join(ROOT, "..", "frontend", "src")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def run_checks() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        results.append((name, condition, detail))

    router_path = os.path.join(ROOT, "app", "api", "v1", "account_router.py")
    router_src = read(router_path)

    check("GET /profile 엔드포인트", '@router.get("/profile")' in router_src)
    check("PATCH /profile 엔드포인트", '@router.patch("/profile")' in router_src)
    check("POST /change-password 엔드포인트", '@router.post("/change-password")' in router_src)
    check("GET /oauth-accounts 엔드포인트", '@router.get("/oauth-accounts")' in router_src)
    check("POST /oauth-accounts/gitlab/link 엔드포인트", '@router.post("/oauth-accounts/gitlab/link")' in router_src)
    check("DELETE /oauth-accounts/gitlab/unlink 엔드포인트", '@router.delete("/oauth-accounts/gitlab/unlink")' in router_src)
    check("GET /sessions 엔드포인트", '@router.get("/sessions")' in router_src)
    check("DELETE /sessions/{session_id} 엔드포인트", '@router.delete("/sessions/{session_id}")' in router_src)
    check("인증 의존성 사용", "resolve_current_actor" in router_src)
    check("_require_auth 헬퍼 존재", "def _require_auth" in router_src)
    check("인증 미필요 시 401", 'status_code=401, detail="인증이 필요합니다"' in router_src)
    check("현재 비밀번호 검증", "verify_password(body.current_password" in router_src)
    check("비밀번호 복잡도 검증", "validate_password_strength(body.new_password)" in router_src)
    check("비밀번호 변경 후 RT 폐기", "revoke_all_user_tokens" in router_src)
    check("소셜 전용 계정 비밀번호 변경 차단", "not user.password_hash" in router_src)
    check("GitLab 해제 시 비밀번호 확인", "not user.password_hash" in router_src and "GitLab 계정을 해제할 수 없습니다" in router_src)
    check("현재 세션 삭제 방지", "현재 세션은 삭제할 수 없습니다" in router_src)
    check("본인 소유 세션만 접근", "user_id = %s AND revoked = FALSE" in router_src)
    check("프로필 수정 감사 이벤트", "user.profile_updated" in router_src)
    check("비밀번호 변경 감사 이벤트", "user.password_changed" in router_src)
    check("GitLab 해제 감사 이벤트", "user.oauth_unlinked" in router_src)
    check("세션 종료 감사 이벤트", "user.session_revoked" in router_src)

    v1_router_path = os.path.join(ROOT, "app", "api", "v1", "router.py")
    v1_src = read(v1_router_path)
    check("account_router import", "import account_router as account" in v1_src)
    check("account 라우터 등록", 'account.router, prefix="/account"' in v1_src)
    check("account 태그", 'tags=["account"]' in v1_src)

    oauth_path = os.path.join(ROOT, "app", "api", "auth", "oauth_service.py")
    oauth_src = read(oauth_path)
    check("create_authorization_url에 link_to_user_id 파라미터", "link_to_user_id" in oauth_src.split("def create_authorization_url")[1].split("def ")[0])
    check("state에 link_to_user_id 저장", '"link_to_user_id"' in oauth_src)
    check("_link_or_create_account에 link_to_user_id 파라미터", "link_to_user_id" in oauth_src.split("def _link_or_create_account")[1].split("def ")[0])
    check("명시적 계정 연결 로직", "link_to_user_id:" in oauth_src or "if link_to_user_id:" in oauth_src)

    frontend_files = [
        ("Account API 클라이언트", "lib/api/account.ts"),
        ("Account 레이아웃", "components/account/AccountLayout.tsx"),
        ("Account 루트 레이아웃", "app/account/layout.tsx"),
        ("Account 리다이렉트", "app/account/page.tsx"),
        ("프로필 페이지", "app/account/profile/page.tsx"),
        ("보안 페이지", "app/account/security/page.tsx"),
        ("세션 페이지", "app/account/sessions/page.tsx"),
    ]
    for name, rel_path in frontend_files:
        full_path = os.path.join(FRONTEND, rel_path)
        check(f"{name} 파일 존재", os.path.exists(full_path), f"경로: {full_path}")

    api_src = read(os.path.join(FRONTEND, "lib", "api", "account.ts"))
    check("getProfile 메서드", "getProfile" in api_src)
    check("updateProfile 메서드", "updateProfile" in api_src)
    check("changePassword 메서드", "changePassword" in api_src)
    check("getOAuthAccounts 메서드", "getOAuthAccounts" in api_src)
    check("linkGitLab 메서드", "linkGitLab" in api_src)
    check("unlinkGitLab 메서드", "unlinkGitLab" in api_src)
    check("getSessions 메서드", "getSessions" in api_src)
    check("revokeSession 메서드", "revokeSession" in api_src)
    check("Bearer 토큰 인증", 'Authorization: `Bearer ${at}`' in api_src or "Bearer" in api_src)
    check("credentials: include", 'credentials: "include"' in api_src)

    layout_src = read(os.path.join(FRONTEND, "components", "account", "AccountLayout.tsx"))
    check("사이드바 aria-label", 'aria-label="계정 메뉴"' in layout_src or 'aria-label="계정 설정 네비게이션"' in layout_src)
    check("aria-current 지원", 'aria-current={isActive ? "page" : undefined}' in layout_src)
    check("모바일 네비게이션 존재", "sm:hidden" in layout_src)
    check("데스크탑 사이드바 존재", "hidden sm:block" in layout_src)
    check("반응형 헤더", "sticky top-0" in layout_src)

    profile_src = read(os.path.join(FRONTEND, "app", "account", "profile", "page.tsx"))
    check("프로필: role=alert 에러", 'role="alert"' in profile_src)
    check("프로필: role=status 성공", 'role="status"' in profile_src)
    check("프로필: noValidate", "noValidate" in profile_src)
    check("프로필: semantic dl/dt/dd", "<dl" in profile_src and "<dt" in profile_src and "<dd" in profile_src)

    security_src = read(os.path.join(FRONTEND, "app", "account", "security", "page.tsx"))
    check("보안: role=alert 에러", 'role="alert"' in security_src)
    check("보안: 필수 항목 표시", "필수 항목" in security_src)
    check("보안: noValidate", "noValidate" in security_src)
    check("보안: 소셜 계정 비밀번호 안내", "소셜 로그인으로 가입한 계정" in security_src)

    sessions_src = read(os.path.join(FRONTEND, "app", "account", "sessions", "page.tsx"))
    check("세션: 현재 세션 배지", "현재 세션" in sessions_src)
    check("세션: role=listitem", 'role="listitem"' in sessions_src)
    check("세션: 빈 상태 처리", "활성 세션이 없습니다" in sessions_src)

    validation_src = read(os.path.join(FRONTEND, "lib", "validations", "auth.ts"))
    check("changePasswordSchema 존재", "changePasswordSchema" in validation_src)
    check("current_password 필드", "current_password" in validation_src)
    check("비밀번호 확인 리파인", "비밀번호가 일치하지 않습니다" in validation_src)

    pw_src = read(os.path.join(FRONTEND, "components", "auth", "PasswordInput.tsx"))
    check("required prop 지원", "required?" in pw_src or "required: boolean" in pw_src or "required?: boolean" in pw_src)
    check("aria-required prop 지원", "aria-required" in pw_src)

    return results


def test_phase14_account_verification() -> None:
    failures = [
        f"{name} - {detail}" if detail else name
        for name, ok, detail in run_checks()
        if not ok
    ]
    assert not failures, "Task 14-7 account verification failed:\n" + "\n".join(failures)
