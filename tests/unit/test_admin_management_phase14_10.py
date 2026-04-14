"""
Phase 14-10 사용자/조직/역할 관리 UI — 검수 스크립트.

권한 매트릭스 API + UI 통합, 역할 필터, SUPER_ADMIN 삭제 가드, 반응형을 검증한다.
"""

import os

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
FRONTEND = os.path.join(ROOT, "frontend", "src")
BACKEND = os.path.join(ROOT, "backend", "app")


def read(base: str, rel: str) -> str:
    full = os.path.normpath(os.path.join(base, rel))
    with open(full, encoding="utf-8") as f:
        return f.read()


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))


# ═══════════════════════════════════════
# 1. 백엔드 — 권한 매트릭스 API
# ═══════════════════════════════════════

authz = read(BACKEND, "api/auth/authorization.py")
check("BE-01: get_permission_matrix 함수 정의", "def get_permission_matrix" in authz)
check("BE-02: 매트릭스 반환 타입 dict[str, list[str]]",
      "dict[str, list[str]]" in authz)
check("BE-03: 역할 순서 정의 (_ORDER)", "_ORDER" in authz)

auth_init = read(BACKEND, "api/auth/__init__.py")
check("BE-04: get_permission_matrix export", "get_permission_matrix" in auth_init)

admin_router = read(BACKEND, "api/v1/admin.py")
check("BE-05: /roles/permissions/matrix 엔드포인트",
      "/roles/permissions/matrix" in admin_router)
check("BE-06: get_role_permission_matrix 함수",
      "def get_role_permission_matrix" in admin_router)
check("BE-07: get_permission_matrix import",
      "get_permission_matrix" in admin_router)
check("BE-08: 역할 그룹화 로직 (groups)",
      'grouped' in admin_router and '"groups"' in admin_router)
check("BE-09: list_users role 필터",
      "role: Optional[str]" in admin_router and "role_name = %s" in admin_router)

# ═══════════════════════════════════════
# 2. 프론트엔드 — 타입 / API 클라이언트
# ═══════════════════════════════════════

types_admin = read(FRONTEND, "types/admin.ts")
check("TY-01: PermissionMatrix 타입", "interface PermissionMatrix" in types_admin)
check("TY-02: PermissionMatrixGroup 타입",
      "interface PermissionMatrixGroup" in types_admin)
check("TY-03: PermissionMatrixItem 타입",
      "interface PermissionMatrixItem" in types_admin)

api_admin = read(FRONTEND, "lib/api/admin.ts")
check("API-01: getPermissionMatrix 함수",
      "getPermissionMatrix" in api_admin)
check("API-02: PermissionMatrix import", "PermissionMatrix" in api_admin)
check("API-03: getUsers role 파라미터",
      "role?: string" in api_admin and "role: params.role" in api_admin)

# ═══════════════════════════════════════
# 3. 프론트엔드 — PermissionMatrix 컴포넌트
# ═══════════════════════════════════════

pm = read(FRONTEND, "features/admin/roles/PermissionMatrix.tsx")
check("PM-01: PermissionMatrix 컴포넌트 export",
      "export function PermissionMatrix" in pm)
check("PM-02: useQuery 사용", "useQuery" in pm)
check("PM-03: 그룹 접기/펼치기 (collapsed Set)",
      "Set<string>" in pm and "toggle" in pm)
check("PM-04: 로딩 스켈레톤", 'role="status"' in pm)
check("PM-05: 에러 fallback", 'role="alert"' in pm)
check("PM-06: 그룹 라벨 매핑 (GROUP_LABELS)",
      "GROUP_LABELS" in pm and '"문서"' in pm)
check("PM-07: aria-expanded 토글", "aria-expanded" in pm)
check("PM-08: aria-controls 연결", "aria-controls" in pm)
check("PM-09: 체크 아이콘 표시 (allowed)", "M5 13l4 4L19 7" in pm)
check("PM-10: scope='col' 헤더", 'scope="col"' in pm)
check("PM-11: scope='row' 헤더", 'scope="row"' in pm)
check("PM-12: 가로 스크롤 (overflow-x-auto)",
      "overflow-x-auto" in pm)
check("PM-13: focus-visible 링", "focus-visible:ring" in pm)

# ═══════════════════════════════════════
# 4. AdminRolesPage — 매트릭스 통합
# ═══════════════════════════════════════

roles_page = read(FRONTEND, "features/admin/roles/AdminRolesPage.tsx")
check("RP-01: PermissionMatrix import",
      "import { PermissionMatrix }" in roles_page)
check("RP-02: <PermissionMatrix /> 사용",
      "<PermissionMatrix" in roles_page)
check("RP-03: 반응형 패딩 (p-4 sm:p-6)",
      "p-4 sm:p-6" in roles_page)

# ═══════════════════════════════════════
# 5. AdminUsersPage — 역할 필터
# ═══════════════════════════════════════

users_page = read(FRONTEND, "features/admin/users/AdminUsersPage.tsx")
check("UP-01: role 상태 변수", 'const [role, setRole]' in users_page)
check("UP-02: role 쿼리 파라미터", "role: role || undefined" in users_page)
check("UP-03: queryKey에 role 포함",
      'queryKey: ["admin", "users", page, search, status, role]' in users_page)
check("UP-04: 역할 필터 select", 'id="filter-role"' in users_page)
check("UP-05: ROLES options 반복 렌더", "ROLES.map" in users_page)
check("UP-06: 초기화 버튼 role 포함",
      "search || status || role" in users_page and 'setRole("")' in users_page)
check("UP-07: 반응형 패딩", "p-4 sm:p-6" in users_page)
check("UP-08: 반응형 헤딩", "text-xl sm:text-2xl" in users_page)
check("UP-09: 사용자 추가 버튼 44px (min-h-[40px] 이상)",
      "min-h-[40px]" in users_page)

# ═══════════════════════════════════════
# 6. AdminUserDetailPage — SUPER_ADMIN 삭제 가드
# ═══════════════════════════════════════

user_detail = read(FRONTEND, "features/admin/users/AdminUserDetailPage.tsx")
check("UD-01: useAuth import", "useAuth" in user_detail)
check("UD-02: hasRole SUPER_ADMIN 체크",
      'hasRole?.("SUPER_ADMIN")' in user_detail)
check("UD-03: canDelete 변수", "canDelete" in user_detail)
check("UD-04: 삭제 버튼 조건부 렌더",
      "{canDelete && (" in user_detail)

# ═══════════════════════════════════════
# 7. AdminOrgsPage — SUPER_ADMIN 삭제 가드
# ═══════════════════════════════════════

orgs_page = read(FRONTEND, "features/admin/orgs/AdminOrgsPage.tsx")
check("OP-01: useAuth import", "useAuth" in orgs_page)
check("OP-02: hasRole SUPER_ADMIN 체크",
      'hasRole?.("SUPER_ADMIN")' in orgs_page)
check("OP-03: canDelete 조건부 렌더", "{canDelete && (" in orgs_page)
check("OP-04: 반응형 패딩", "p-4 sm:p-6" in orgs_page)

# ═══════════════════════════════════════
# 8. 보안 검증
# ═══════════════════════════════════════

# 매트릭스 API 보호
check("SEC-01: 매트릭스 API에 require_admin_access",
      "/roles/permissions/matrix" in admin_router
      and "Depends(require_admin_access)" in admin_router)
# 권한 매트릭스 자체가 노출되어도 안전 (코드 레벨 정보)
# SUPER_ADMIN 가드 (UI는 보조, 백엔드가 진짜 권한 검사)
check("SEC-02: 백엔드 admin.write SUPER_ADMIN 전용",
      'admin.write": frozenset({"SUPER_ADMIN"})' in authz)
# XSS — React 기본 이스케이프
check("SEC-03: PermissionMatrix dangerouslySetInnerHTML 미사용",
      "dangerouslySetInnerHTML" not in pm)
# 하드코딩 시크릿
for fname, c in [("pm", pm), ("users", users_page), ("orgs", orgs_page),
                 ("roles", roles_page)]:
    has_secret = any(s in c for s in ["password=\"", "secret=\"", "api_key=\""])
    check(f"SEC-{fname}-secret: 하드코딩 시크릿 없음", not has_secret)

# ═══════════════════════════════════════
# 9. 접근성 검증
# ═══════════════════════════════════════

check("A11Y-01: PermissionMatrix aria-label",
      'aria-label="역할-권한 매트릭스"' in pm)
check("A11Y-02: PermissionMatrix sr-only 로딩",
      "sr-only" in pm)
check("A11Y-03: 매트릭스 셀 aria-label",
      "aria-label={" in pm and "허용" in pm and "금지" in pm)
check("A11Y-04: UsersPage label htmlFor",
      'htmlFor="filter-role"' in users_page
      and 'htmlFor="filter-status"' in users_page)
check("A11Y-05: focus-visible 링 (UsersPage)",
      "focus-visible:ring" in users_page)
check("A11Y-06: focus-visible 링 (OrgsPage)",
      "focus-visible:ring" in orgs_page)

# ═══════════════════════════════════════
# 결과 출력
# ═══════════════════════════════════════

print("=" * 70)
print("Phase 14-10 사용자/조직/역할 관리 UI — 검수 결과")
print("=" * 70)

passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
total = len(results)

for name, ok, detail in results:
    status = "PASS" if ok else "FAIL"
    extra = f" - {detail}" if detail else ""
    print(f"  [{status}]  {name}{extra}")

print("=" * 70)
print(f"총 {total}개 검증 | PASS {passed} | FAIL {failed}")
print("=" * 70)

if failed > 0:
    exit(1)
