"""
Phase 14-8 프론트엔드 인증 상태 관리 검증.

AuthContext, API 인터셉터, AuthGuard, 기존 페이지 통합을 검증한다.
"""

import os
import re

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "src")


def read(rel_path: str) -> str:
    full = os.path.normpath(os.path.join(FRONTEND, rel_path))
    with open(full, encoding="utf-8") as f:
        return f.read()


def run_checks() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    ctx = read("contexts/AuthContext.tsx")

    check("CTX-01: AuthContext 파일 존재", "AuthContext" in ctx)
    check("CTX-02: AuthUser 인터페이스", "interface AuthUser" in ctx)
    check("CTX-03: AuthState 인터페이스", "interface AuthState" in ctx)
    check("CTX-04: useReducer 사용", "useReducer" in ctx)
    check("CTX-05: LOGIN_SUCCESS 액션", "LOGIN_SUCCESS" in ctx)
    check("CTX-06: LOGOUT 액션", '"LOGOUT"' in ctx)
    check("CTX-07: TOKEN_REFRESHED 액션", "TOKEN_REFRESHED" in ctx)
    check("CTX-08: SET_LOADING 액션", "SET_LOADING" in ctx)
    check("CTX-09: 모듈 수준 _accessToken", "let _accessToken" in ctx)
    check("CTX-10: getAccessToken 내보내기", "export function getAccessToken" in ctx)
    check("CTX-11: setAccessToken 함수", "function setAccessToken" in ctx)
    check(
        "CTX-12: localStorage/sessionStorage API 미사용",
        "localStorage.setItem" not in ctx
        and "sessionStorage.setItem" not in ctx
        and "localStorage.getItem" not in ctx
        and "sessionStorage.getItem" not in ctx,
    )
    check("CTX-13: silent refresh POST /auth/refresh", "/api/v1/auth/refresh" in ctx)
    check("CTX-14: credentials include", 'credentials: "include"' in ctx)
    check("CTX-15: refresh 큐잉 (_refreshPromise)", "_refreshPromise" in ctx)
    check("CTX-16: 토큰 갱신 스케줄링 (5분 전)", "5 * 60 * 1000" in ctx)
    check("CTX-17: JWT exp 디코딩", "payload.exp" in ctx)
    check("CTX-18: ROLE_HIERARCHY 정의", "ROLE_HIERARCHY" in ctx)
    check("CTX-19: VIEWER→SUPER_ADMIN 순서", ctx.index('"VIEWER"') < ctx.index('"SUPER_ADMIN"'))
    check("CTX-20: hasMinimumRole 함수", "hasMinimumRole" in ctx)
    check("CTX-21: login 함수", "const login = useCallback" in ctx)
    check("CTX-22: loginWithGitLab 함수", "loginWithGitLab" in ctx)
    check("CTX-23: logout 함수", "const logout = useCallback" in ctx)
    check("CTX-24: handleOAuthCallback 함수", "handleOAuthCallback" in ctx)
    check("CTX-25: useAuth 훅 내보내기", "export function useAuth" in ctx)
    check("CTX-26: AuthProvider 내보내기", "export function AuthProvider" in ctx)
    check(
        "CTX-27: attemptSilentRefreshForInterceptor 내보내기",
        "export async function attemptSilentRefreshForInterceptor" in ctx,
    )
    check("CTX-28: fetchProfile 호출", "fetchProfile" in ctx)
    check("CTX-29: 레거시 window.__mimir_at 제거", "__mimir_at" not in ctx)
    check("CTX-30: context value memoized", "useMemo<AuthContextValue>" in ctx)

    client = read("lib/api/client.ts")

    check("CLI-01: getAccessToken import", "getAccessToken" in client)
    check("CLI-02: attemptSilentRefreshForInterceptor import", "attemptSilentRefreshForInterceptor" in client)
    check("CLI-03: Bearer Token 자동 첨부", "Bearer" in client)
    check("CLI-04: 401 시 silent refresh", "res.status === 401" in client)
    check("CLI-05: 재시도 1회 제한 (_retry)", "_retry" in client)
    check("CLI-06: 공개 페이지 리다이렉트 제외", "publicPaths" in client)
    check("CLI-07: /login 공개 경로", '"/login"' in client)
    check("CLI-08: /register 공개 경로", '"/register"' in client)
    check("CLI-09: /auth/callback 공개 경로", '"/auth/callback"' in client)
    check("CLI-10: 개발 환경 fallback (X-Actor-Id)", "X-Actor-Id" in client)
    check("CLI-11: credentials include", 'credentials: "include"' in client)
    check("CLI-12: ApiError 클래스", "class ApiError" in client)
    check("CLI-13: 204 No Content 처리", "res.status === 204" in client)
    check("CLI-14: getApiErrorMessage 내보내기", "export function getApiErrorMessage" in client)
    check("CLI-15: 로그인 리다이렉트 encodeURIComponent", "encodeURIComponent" in client)

    guard = read("components/auth/AuthGuard.tsx")

    check("GRD-01: AuthGuard 파일 존재", "AuthGuard" in guard)
    check("GRD-02: useAuth import", "useAuth" in guard)
    check("GRD-03: isLoading 상태 처리", "isLoading" in guard)
    check("GRD-04: SkeletonLayout import", "SkeletonLayout" in guard)
    check("GRD-05: ForbiddenContent import", "ForbiddenContent" in guard)
    check("GRD-06: requiredRole prop", "requiredRole" in guard)
    check("GRD-07: hasMinimumRole 체크", "hasMinimumRole" in guard)
    check("GRD-08: 미인증 시 /login 리다이렉트", "/login" in guard)
    check("GRD-09: redirect 쿼리 파라미터", "redirect=" in guard)
    check("GRD-10: useRouter 사용", "useRouter" in guard)
    check("GRD-11: usePathname 사용", "usePathname" in guard)

    skel = read("components/auth/SkeletonLayout.tsx")

    check("SKL-01: SkeletonLayout 파일 존재", "SkeletonLayout" in skel)
    check("SKL-02: animate-pulse 사용", "animate-pulse" in skel)
    check("SKL-03: role=status", 'role="status"' in skel)
    check("SKL-04: aria-live", "aria-live" in skel)
    check("SKL-05: sr-only 텍스트", "sr-only" in skel)

    forb = read("components/auth/ForbiddenContent.tsx")

    check("FRB-01: ForbiddenContent 파일 존재", "ForbiddenContent" in forb)
    check("FRB-02: 403 관련 텍스트", "접근 권한" in forb)
    check("FRB-03: 홈 링크", 'href="/"' in forb)
    check("FRB-04: 로그인 링크", 'href="/login"' in forb)
    check("FRB-05: aria-hidden 데코레이티브 아이콘", "aria-hidden" in forb)

    fpage = read("app/forbidden/page.tsx")

    check("FPG-01: forbidden 페이지 존재", "ForbiddenPage" in fpage or "ForbiddenContent" in fpage)
    check("FPG-02: ForbiddenContent import", "ForbiddenContent" in fpage)

    prov = read("lib/providers.tsx")

    check("PRV-01: AuthProvider import", "AuthProvider" in prov)
    check("PRV-02: AuthProvider 감싸기", "<AuthProvider>" in prov)
    check("PRV-03: QueryClientProvider 존재", "QueryClientProvider" in prov)
    check("PRV-04: AuthProvider가 QueryClientProvider 안에", prov.index("<AuthProvider>") > prov.index("<QueryClientProvider"))

    login = read("app/login/page.tsx")

    check("LGN-01: useAuth import", "useAuth" in login)
    check("LGN-02: authApi import 제거", "authApi" not in login)
    check("LGN-03: window.__mimir_at 제거", "__mimir_at" not in login)
    check("LGN-04: useAuth().login 호출", "await login(" in login)
    check("LGN-05: loginWithGitLab 호출", "loginWithGitLab" in login)
    check("LGN-06: ?redirect= 지원", 'searchParams.get("redirect")' in login)
    check("LGN-07: 이미 인증된 사용자 리다이렉트", "isAuthenticated" in login)
    check("LGN-08: Suspense 래핑 (useSearchParams)", "<Suspense" in login)
    check("LGN-09: LoginContent 내부 컴포넌트", "function LoginContent" in login)
    check("LGN-10: export default LoginPage", "export default function LoginPage" in login)

    callback = read("app/auth/callback/page.tsx")

    check("CBK-01: useAuth import", "useAuth" in callback)
    check("CBK-02: window.__mimir_at 제거", "__mimir_at" not in callback)
    check("CBK-03: handleOAuthCallback 호출", "handleOAuthCallback" in callback)
    check("CBK-04: URL fragment AT 추출", "window.location.hash" in callback)
    check("CBK-05: 에러 처리", "setAsyncError" in callback or "urlError" in callback)
    check("CBK-06: Suspense 래핑", "<Suspense" in callback)
    check("CBK-07: history.replaceState 호출", "history.replaceState" in callback)

    acc = read("lib/api/account.ts")

    check("ACC-01: getAccessToken import", "getAccessToken" in acc)
    check("ACC-02: window.__mimir_at 제거", "__mimir_at" not in acc)
    check("ACC-03: getAccessToken() 호출", "getAccessToken()" in acc)
    check("ACC-04: Bearer 헤더", "Bearer" in acc)

    alayout = read("app/account/layout.tsx")

    check("ALY-01: AuthGuard import", "AuthGuard" in alayout)
    check("ALY-02: AuthGuard 감싸기", "<AuthGuard>" in alayout)
    check("ALY-03: AccountLayout 내부", "<AccountLayout>" in alayout)

    all_files: list[str] = []
    for root, _, files in os.walk(FRONTEND):
        for filename in files:
            if filename.endswith((".ts", ".tsx")):
                all_files.append(os.path.join(root, filename))

    at_in_storage = False
    for fpath in all_files:
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        if re.search(r"(localStorage|sessionStorage)\.(set|get)Item.*access_token", content):
            at_in_storage = True
            break

    check("SEC-01: AT가 localStorage/sessionStorage에 저장되지 않음", not at_in_storage)

    mimir_at_outside_ctx: list[str] = []
    for fpath in all_files:
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        if "__mimir_at" in content:
            rel = os.path.relpath(fpath, FRONTEND)
            if "AuthContext" not in rel:
                mimir_at_outside_ctx.append(rel)

    check(
        "SEC-02: __mimir_at가 AuthContext 외부에서 미사용",
        len(mimir_at_outside_ctx) == 0,
        f"발견: {mimir_at_outside_ctx}" if mimir_at_outside_ctx else "",
    )
    check("SEC-03: AuthContext에서 credentials include", 'credentials: "include"' in ctx)
    check("SEC-04: API client에서 credentials include", 'credentials: "include"' in client)

    check("A11Y-01: SkeletonLayout sr-only", "sr-only" in skel)
    check("A11Y-02: ForbiddenContent aria-hidden 아이콘", "aria-hidden" in forb)
    check("A11Y-03: 로그인 페이지 role=alert", 'role="alert"' in login)
    check("A11Y-04: 콜백 페이지 role=alert", 'role="alert"' in callback)
    check("A11Y-05: 콜백 페이지 role=status (로딩)", 'role="status"' in callback)

    return results


def test_phase14_8_auth_state_verification() -> None:
    failures = [
        f"{name} - {detail}" if detail else name
        for name, ok, detail in run_checks()
        if not ok
    ]
    assert not failures, "Phase 14-8 auth state verification failed:\n" + "\n".join(failures)
