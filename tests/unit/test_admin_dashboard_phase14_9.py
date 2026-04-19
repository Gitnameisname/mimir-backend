"""
Phase 14-9 통합 관리 대시보드 UI 검증.

AdminLayout, AdminHeader, AdminSidebar, AdminDashboardPage,
그리고 AuthGuard / 플레이스홀더 페이지를 검증한다.
"""

import os

FRONTEND = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "src")
)


def read(rel_path: str) -> str:
    full = os.path.normpath(os.path.join(FRONTEND, rel_path))
    with open(full, encoding="utf-8") as f:
        return f.read()


def run_checks() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    sb = read("components/admin/layout/AdminSidebar.tsx")

    check("SB-01: ADMIN_NAV_ITEMS config 정의", "ADMIN_NAV_ITEMS" in sb)
    check("SB-02: NavItem 인터페이스", "interface NavItem" in sb)
    check("SB-03: 대시보드 메뉴", '"/admin/dashboard"' in sb)
    check("SB-04: 사용자 관리 메뉴", '"/admin/users"' in sb)
    check("SB-05: 조직 관리 메뉴", '"/admin/organizations"' in sb)
    check("SB-06: 역할/권한 관리 메뉴", '"/admin/roles"' in sb)
    check("SB-07: 시스템 설정 메뉴", '"/admin/settings"' in sb)
    check("SB-08: 모니터링 메뉴", '"/admin/monitoring"' in sb)
    check("SB-09: 알림 관리 메뉴", '"/admin/alerts"' in sb)
    check("SB-10: 배치 작업 메뉴", '"/admin/jobs"' in sb)
    check("SB-11: 감사 로그 메뉴", '"/admin/audit-logs"' in sb)
    check("SB-12: API 키 메뉴", '"/admin/api-keys"' in sb)
    check("SB-13: 그룹 'S1 관리'", '"S1 관리"' in sb)
    check("SB-14: 그룹 'AI 플랫폼'", '"AI 플랫폼"' in sb)
    check("SB-15: 그룹 '에이전트·Scope'", '"에이전트·Scope"' in sb)
    check("SB-16: 그룹 '평가·추출'", '"평가·추출"' in sb)
    check("SB-17: collapsed prop 지원", "collapsed" in sb)
    check("SB-18: onClose prop 지원 (모바일)", "onClose" in sb)
    check("SB-19: usePathname 사용", "usePathname" in sb)
    check("SB-20: aria-current 속성", "aria-current" in sb)
    check("SB-21: aria-label 속성", "aria-label" in sb)
    check("SB-22: 44px 최소 터치 영역", "min-h-[44px]" in sb)
    check("SB-23: transition 효과", "transition-all" in sb)
    check("SB-24: focus ring 접근성", "focus:ring" in sb)
    check("SB-25: '일반 화면으로 이동' 링크", "일반 화면으로 이동" in sb)

    hd = read("components/admin/layout/AdminHeader.tsx")

    check("HD-01: useAuth 통합", "useAuth" in hd)
    check("HD-02: user, logout, isAuthenticated 구조분해", "logout" in hd and "isAuthenticated" in hd)
    check("HD-03: displayName 우선순위", "display_name" in hd and "email" in hd)
    check("HD-04: 로그아웃 버튼", "로그아웃" in hd)
    check("HD-05: aria-label on 로그아웃", 'aria-label="계정에서 로그아웃"' in hd)
    check("HD-06: 모바일 햄버거 버튼", "관리자 메뉴 열기" in hd)
    check("HD-07: onToggleMobile prop", "onToggleMobile" in hd)
    check("HD-08: onToggleCollapse prop", "onToggleCollapse" in hd)
    check("HD-09: collapsed prop", "collapsed" in hd)
    check("HD-10: aria-pressed on 접기 버튼", "aria-pressed" in hd)
    check("HD-11: Admin 배지", "Admin" in hd)
    check("HD-12: 아바타(initial)", "initial" in hd)
    check("HD-13: role='banner'", 'role="banner"' in hd)
    check("HD-14: Mimir 로고 링크", "/admin/dashboard" in hd)

    lay = read("components/admin/layout/AdminLayout.tsx")

    check("LY-01: AuthGuard import", "AuthGuard" in lay)
    check("LY-02: requiredRole='ORG_ADMIN'", 'requiredRole="ORG_ADMIN"' in lay)
    check("LY-03: AdminHeader 사용", "AdminHeader" in lay)
    check("LY-04: AdminSidebar 사용", "AdminSidebar" in lay)
    check("LY-05: mobileOpen 상태", "mobileOpen" in lay)
    check("LY-06: collapsed 상태", "collapsed" in lay)
    check("LY-07: ESC 키 핸들러", '"Escape"' in lay)
    check("LY-08: body overflow 잠금", "document.body.style.overflow" in lay)
    check("LY-09: 모바일 드로어 (fixed inset-y-0)", "fixed inset-y-0" in lay)
    check("LY-10: 모바일 배경 딤", "bg-black/40" in lay)
    check("LY-11: 데스크탑 사이드바 lg:block", "lg:block" in lay)
    check("LY-12: ToastContainer", "ToastContainer" in lay)
    check("LY-13: role='main'", 'role="main"' in lay)

    dash = read("features/admin/dashboard/AdminDashboardPage.tsx")

    check("DS-01: useQuery 사용", "useQuery" in dash)
    check("DS-02: 30초 자동 갱신", "refetchInterval: 30_000" in dash or "refetchInterval: 30000" in dash)
    check("DS-03: MetricCard 컴포넌트", "function MetricCard" in dash)
    check("DS-04: HealthDot 컴포넌트", "function HealthDot" in dash)
    check("DS-05: HealthLabel 컴포넌트", "function HealthLabel" in dash)
    check("DS-06: ErrorFallback 컴포넌트", "function ErrorFallback" in dash)
    check("DS-07: SkeletonCards 로딩", "function SkeletonCards" in dash)
    check("DS-08: SkeletonRows 로딩", "function SkeletonRows" in dash)
    check(
        "DS-09: 반응형 그리드 (sm/xl)",
        "sm:grid-cols-2" in dash and "xl:grid-cols-4" in dash,
    )
    check("DS-10: adminApi 사용", "adminApi" in dash)
    check("DS-11: ko-KR 로케일 숫자 포맷", "toLocaleString" in dash or "ko-KR" in dash)
    check("DS-12: article 시맨틱 태그", "<article" in dash)
    check(
        "DS-13: metrics/health/errors/audit 쿼리",
        "metricsQ" in dash and "healthQ" in dash and "errorsQ" in dash and "auditQ" in dash,
    )
    check("DS-14: 재시도 버튼 (onRetry)", "onRetry" in dash)

    for page, page_title in [
        ("settings", "시스템 설정"),
        ("monitoring", "모니터링"),
        ("alerts", "알림 관리"),
        ("api-keys", "API 키 관리"),
    ]:
        path = f"app/admin/{page}/page.tsx"
        try:
            content = read(path)
            check(f"PH-{page}: 페이지 존재", True)
            check(f"PH-{page}: 페이지 제목 메타데이터", page_title in content)
        except FileNotFoundError:
            check(f"PH-{page}: 페이지 존재", False, f"{path} 없음")

    app_layout = read("app/admin/layout.tsx")
    check("APP-01: 'use client' 지시어", '"use client"' in app_layout)
    check("APP-02: AdminLayout 사용", "AdminLayout" in app_layout)

    check("SEC-01: AdminLayout에 AuthGuard 적용", "<AuthGuard" in lay)
    check("SEC-02: ORG_ADMIN 이상 제한", "ORG_ADMIN" in lay)

    for fname, content in [("sidebar", sb), ("header", hd), ("layout", lay), ("dashboard", dash)]:
        has_secret = any(token in content for token in ["password=", "secret=", 'api_key="', 'token="'])
        check(f"SEC-{fname}: 하드코딩 시크릿 없음", not has_secret)

    check("A11Y-01: Sidebar aria-label", "aria-label" in sb)
    check("A11Y-02: Header aria-label (로그아웃)", 'aria-label="계정에서 로그아웃"' in hd)
    check("A11Y-03: Header role=banner", 'role="banner"' in hd)
    check("A11Y-04: Layout role=main", 'role="main"' in lay)
    check("A11Y-05: Sidebar focus ring", "focus:ring" in sb)
    check("A11Y-06: Header focus ring", "focus:ring" in hd)
    check("A11Y-07: 44px 터치 타겟 (sidebar)", "min-h-[44px]" in sb)
    check("A11Y-08: 44px 터치 타겟 (header)", "min-h-[44px]" in hd)
    check("A11Y-09: SVG aria-hidden", 'aria-hidden="true"' in sb and 'aria-hidden="true"' in hd)

    return results


def test_phase14_9_dashboard_ui_verification() -> None:
    failures = [
        f"{name} - {detail}" if detail else name
        for name, ok, detail in run_checks()
        if not ok
    ]
    assert not failures, "Phase 14-9 dashboard verification failed:\n" + "\n".join(failures)
