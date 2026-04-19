"""
Phase 14-11 시스템 설정 관리 검증.

system_settings 테이블 + 시드, settings_repository, 3종 API,
타입 검증, 감사 로그, Valkey 캐시, SUPER_ADMIN 가드, UI 통합, 접근성을 검증한다.
"""

import os

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
FRONTEND = os.path.join(ROOT, "frontend", "src")
BACKEND = os.path.join(ROOT, "backend", "app")


def read(base: str, rel: str) -> str:
    full = os.path.normpath(os.path.join(base, rel))
    with open(full, encoding="utf-8") as f:
        return f.read()


def run_checks() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    conn_py = read(BACKEND, "db/connection.py")
    check("DDL-01: system_settings 테이블 정의", "_SYSTEM_SETTINGS_DDL" in conn_py and "CREATE TABLE IF NOT EXISTS system_settings" in conn_py)
    check("DDL-02: UNIQUE (category, key) 제약", "UNIQUE (category, key)" in conn_py)
    check("DDL-03: idx_system_settings_category 인덱스", "idx_system_settings_category" in conn_py)
    check("DDL-04: JSONB value 컬럼", "value       JSONB NOT NULL" in conn_py)
    check("DDL-05: updated_by FK users(id)", "updated_by  UUID REFERENCES users(id)" in conn_py)
    check(
        "DDL-06: 초기 시드 데이터 (12건)",
        "_SYSTEM_SETTINGS_SEED_DDL" in conn_py
        and "ON CONFLICT (category, key) DO NOTHING" in conn_py
        and conn_py.count("'auth',") >= 5
        and conn_py.count("'system',") >= 3
        and conn_py.count("'notification',") >= 2
        and conn_py.count("'security',") >= 2,
    )
    check("DDL-07: init_db에서 system_settings 생성 호출", "_SYSTEM_SETTINGS_DDL)" in conn_py and "_SYSTEM_SETTINGS_SEED_DDL)" in conn_py)

    repo = read(BACKEND, "repositories/settings_repository.py")
    check("REPO-01: SettingsRepository 클래스", "class SettingsRepository" in repo)
    check("REPO-02: list_all 메서드", "def list_all(" in repo)
    check("REPO-03: list_by_category 메서드", "def list_by_category(" in repo)
    check("REPO-04: get_one 메서드", "def get_one(" in repo)
    check("REPO-05: update_value 메서드", "def update_value(" in repo)
    check("REPO-06: SQL 파라미터 바인딩 (%s)", "%s" in repo and "json.dumps" in repo)
    check("REPO-07: settings_repository 싱글톤 export", "settings_repository = SettingsRepository()" in repo)
    check("REPO-08: RETURNING 절 사용 (업데이트 결과 반환)", "RETURNING" in repo)

    admin_py = read(BACKEND, "api/v1/admin.py")
    check("API-01: GET /settings 엔드포인트", '@router.get("/settings"' in admin_py and "def get_all_settings" in admin_py)
    check("API-02: GET /settings/{category} 엔드포인트", '@router.get("/settings/{category}"' in admin_py and "def get_settings_by_category" in admin_py)
    check("API-03: PATCH /settings/{category}/{key} 엔드포인트", '@router.patch("/settings/{category}/{key}"' in admin_py and "def update_setting" in admin_py)
    check("API-04: 카테고리별 그룹화 응답", '"categories"' in admin_py and "_SETTINGS_CATEGORY_LABELS" in admin_py)
    check("API-05: UpdateSettingBody Pydantic 모델", "class UpdateSettingBody" in admin_py and "value: Any" in admin_py)
    check("API-06: 카테고리 형식 검증 (regex)", "^[a-z][a-z0-9_]{0,99}$" in admin_py and "유효하지 않은 카테고리 형식" in admin_py)
    check("API-07: 키 형식 검증 (regex)", "^[a-z][a-z0-9_]{0,254}$" in admin_py and "유효하지 않은 키 형식" in admin_py)
    check("API-08: 타입 일치 검증 (_type_signature)", "_type_signature" in admin_py and 'isinstance(v, bool)' in admin_py)
    check("API-09: bool != int 구분 (boolean을 int로 오인 방지)", 'if isinstance(v, bool):\n                return "bool"' in admin_py)
    check("API-10: 404 — 키/카테고리 없음", "status_code=404" in admin_py and "찾을 수 없습니다" in admin_py)
    check("API-11: 422 — 타입 불일치", "status_code=422" in admin_py and "값 타입이 일치하지 않습니다" in admin_py)

    check(
        "SEC-01: 모든 settings 엔드포인트에 require_admin_access",
        admin_py.count("Depends(require_admin_access)") >= 30
        and "def get_all_settings(_=Depends(require_admin_access))" in admin_py
        and "def get_settings_by_category(category: str, _=Depends(require_admin_access))" in admin_py,
    )
    check(
        "SEC-02: PATCH는 admin.write → SUPER_ADMIN 전용 (require_admin_access의 메서드 분기)",
        'request.method in ("POST", "PATCH", "DELETE", "PUT")' in admin_py and '"admin.write"' in admin_py,
    )
    check("SEC-03: SQL 파라미터 바인딩 (settings 쿼리)", "WHERE category = %s" in repo and "WHERE category = %s AND key = %s" in repo)
    check("SEC-04: 감사 이벤트 SETTING_CHANGED", 'event_type="SETTING_CHANGED"' in admin_py)
    check("SEC-05: old/new value 감사 metadata", '"old_value": old_value' in admin_py and '"new_value": new_value' in admin_py)
    check("SEC-06: previous_state/new_state 길이 제한 (DoS 방어)", "[:500]" in admin_py and "previous_state=json_lib.dumps(old_value" in admin_py)
    check("SEC-07: 감사 실패가 응답 차단하지 않음 (try/except)", "감사 이벤트 기록 실패" in admin_py)
    check("CACHE-01: Valkey 캐시 키 정의", "_SETTINGS_CACHE_KEY_ALL" in admin_py)
    check("CACHE-02: 캐시 TTL 5분 (300초)", "_SETTINGS_CACHE_TTL = 300" in admin_py)
    check("CACHE-03: GET 시 캐시 조회 (setex/get)", "get_valkey().setex" in admin_py and "get_valkey().get(_SETTINGS_CACHE_KEY_ALL)" in admin_py)
    check("CACHE-04: PATCH 시 캐시 무효화", "_invalidate_settings_cache()" in admin_py and "delete(_SETTINGS_CACHE_KEY_ALL)" in admin_py)
    check("CACHE-05: 캐시 실패 시 DB 폴백 (try/except)", "캐시 조회 실패 (DB로 폴백)" in admin_py)

    types_admin = read(FRONTEND, "types/admin.ts")
    check("TY-01: SettingValue 타입", "export type SettingValue" in types_admin)
    check("TY-02: SettingItem 인터페이스", "interface SettingItem" in types_admin)
    check("TY-03: SettingCategory 인터페이스", "interface SettingCategory" in types_admin)
    check("TY-04: AllSettingsResponse 인터페이스", "interface AllSettingsResponse" in types_admin)

    api_admin = read(FRONTEND, "lib/api/admin.ts")
    check("API-CL-01: getAllSettings 메서드", "getAllSettings:" in api_admin)
    check("API-CL-02: getSettingsByCategory 메서드", "getSettingsByCategory:" in api_admin)
    check("API-CL-03: updateSetting 메서드 (PATCH)", "updateSetting:" in api_admin and "api.patch<" in api_admin)
    check("API-CL-04: encodeURIComponent 사용 (URL injection 방어)", "encodeURIComponent(category)" in api_admin and "encodeURIComponent(key)" in api_admin)

    page = read(FRONTEND, "features/admin/settings/AdminSettingsPage.tsx")
    check("UI-01: AdminSettingsPage export", "export function AdminSettingsPage" in page)
    check("UI-02: useQuery로 설정 fetch", "useQuery" in page and 'queryKey: ["admin", "settings"]' in page)
    check("UI-03: useAuth().hasRole(SUPER_ADMIN) 가드", 'hasRole?.("SUPER_ADMIN")' in page and "canEdit" in page)
    check("UI-04: 카테고리 탭 (role=tablist/tab/tabpanel)", 'role="tablist"' in page and 'role="tab"' in page and 'role="tabpanel"' in page)
    check("UI-05: 토글 (boolean) 입력 컴포넌트", "function ToggleInput" in page and 'role="switch"' in page and "aria-checked" in page)
    check("UI-06: 숫자 입력 (type=number)", 'type="number"' in page)
    check("UI-07: 텍스트 입력 (type=text)", 'type="text"' in page)
    check("UI-08: 변경 항목 dirty 추적 (drafts state)", "const [drafts, setDrafts]" in page and "dirtyChanges" in page)
    check("UI-09: 변경 확인 다이얼로그 (Modal)", "ConfirmModal" in page and "설정을 변경하시겠습니까" in page)
    check("UI-10: 변경 카운트 배지", "변경 {dirtyCount}건" in page)
    check("UI-11: 되돌리기 버튼 (resetAll)", "resetAll" in page and "되돌리기" in page)
    check("UI-12: 저장 버튼 disabled (canEdit 가드)", "disabled={!canEdit || dirtyCount === 0" in page)
    check("UI-13: useMutationWithToast 사용", "useMutationWithToast" in page and 'successMessage: "설정이 저장되었습니다."' in page)
    check("UI-14: 부분 실패 보고 (성공/실패 카운트)", "successes" in page and "failures" in page and "건 성공" in page and "건 실패" in page)
    check("UI-15: 캐시 무효화 (invalidateQueries)", 'invalidateKeys: [["admin", "settings"]]' in page and "queryClient.invalidateQueries" in page)
    check("UI-16: 타입 검증 즉시 피드백 (UI invalid)", "tOrig !== tDraft" in page and "aria-invalid" in page)

    check("RESP-01: 반응형 패딩 (p-4 sm:p-6)", "p-4 sm:p-6" in page)
    check("RESP-02: 반응형 헤딩 (text-xl sm:text-2xl)", "text-xl sm:text-2xl" in page)
    check("RESP-03: 가로 스크롤 (overflow-x-auto)", "overflow-x-auto" in page)
    check("RESP-04: flex-wrap 헤더", "flex-wrap" in page)
    check("RESP-05: 최소 터치 타겟 (min-h-[40px])", "min-h-[40px]" in page)
    check("A11Y-01: htmlFor 라벨 연결", "htmlFor={fieldId}" in page)
    check("A11Y-02: aria-controls 탭 연결", "aria-controls={`tabpanel-" in page)
    check("A11Y-03: aria-selected 탭 상태", "aria-selected={active}" in page)
    check("A11Y-04: aria-live 변경 카운트 알림", 'aria-live="polite"' in page)
    check("A11Y-05: role=alert 에러 메시지", 'role="alert"' in page)
    check("A11Y-06: role=status 로딩 알림", 'role="status"' in page)
    check("A11Y-07: focus-visible 포커스 링", "focus-visible:ring" in page)
    check("A11Y-08: sr-only 보조 텍스트", "sr-only" in page)

    route_page = read(FRONTEND, "app/admin/settings/page.tsx")
    check("ROUTE-01: app/admin/settings/page.tsx → AdminSettingsPage", "AdminSettingsPage" in route_page and "@/features/admin/settings/AdminSettingsPage" in route_page)
    check("ROUTE-02: page metadata 설정", 'title: "시스템 설정' in route_page)

    sidebar = read(FRONTEND, "components/admin/layout/AdminSidebar.tsx")
    check("ROUTE-03: 사이드바에 /admin/settings 항목", '"/admin/settings"' in sidebar and "시스템 설정" in sidebar)

    check("XSS-01: dangerouslySetInnerHTML 미사용 (page)", "dangerouslySetInnerHTML" not in page)
    check("XSS-02: dangerouslySetInnerHTML 미사용 (api)", "dangerouslySetInnerHTML" not in api_admin)
    for fname, content in [("page", page), ("api", api_admin), ("repo", repo), ("admin_py", admin_py)]:
        has_secret = any(token in content for token in ['password="', 'secret="', 'api_key="', "AKIA"])
        check(f"SEC-secret-{fname}: 하드코딩 시크릿 없음", not has_secret)

    return results


def test_phase14_11_admin_settings_verification() -> None:
    failures = [
        f"{name} - {detail}" if detail else name
        for name, ok, detail in run_checks()
        if not ok
    ]
    assert not failures, "Phase 14-11 admin settings verification failed:\n" + "\n".join(failures)
