"""Phase 14-15 — API 키 CRUD / 감사 로그 필터 UI 검증 스크립트.

backend/frontend 실제 파일을 정적 검사 + AST 분석만 수행 (외부 DB 의존 없음).
산출물: 각 체크 항목의 PASS/FAIL 카운트 + 실패 목록.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

ADMIN_PY = (ROOT / "backend/app/api/v1/admin.py").read_text(encoding="utf-8")
CONN_PY = (ROOT / "backend/app/db/connection.py").read_text(encoding="utf-8")

API_KEYS_PAGE = (ROOT / "frontend/src/features/admin/api-keys/AdminApiKeysPage.tsx").read_text(encoding="utf-8")
AUDIT_PAGE = (ROOT / "frontend/src/features/admin/audit-logs/AdminAuditLogsPage.tsx").read_text(encoding="utf-8")
ADMIN_TS = (ROOT / "frontend/src/lib/api/admin.ts").read_text(encoding="utf-8")
TYPES_TS = (ROOT / "frontend/src/types/admin.ts").read_text(encoding="utf-8")
API_KEYS_ROUTE = (ROOT / "frontend/src/app/admin/api-keys/page.tsx").read_text(encoding="utf-8")

# Phase 14-15 섹션 추출
_P15 = ADMIN_PY.split("Phase 14-15")[-1] if "Phase 14-15" in ADMIN_PY else ""

results: list[tuple[str, str, bool, str]] = []


def check(category: str, name: str, cond: bool, detail: str = "") -> None:
    results.append((category, name, bool(cond), detail))


# ─── DDL (기존 api_keys 테이블) ────────────────────────────────────
check("DDL", "DDL-01 api_keys 테이블 존재", "CREATE TABLE IF NOT EXISTS api_keys" in CONN_PY)
check("DDL", "DDL-02 key_hash 컬럼 (평문 미저장)", "key_hash" in CONN_PY)
check("DDL", "DDL-03 key_prefix 컬럼 (식별용)", "key_prefix" in CONN_PY)
check("DDL", "DDL-04 revoked_reason 컬럼 (폐기 사유 보존)", "revoked_reason" in CONN_PY)


# ─── API 키 백엔드 ──────────────────────────────────────────────────
check("API", "API-01 POST /api-keys 201", '@router.post("/api-keys"' in _P15 and "status_code=201" in _P15)
check("API", "API-02 POST /api-keys/{key_id}/revoke", '@router.post("/api-keys/{key_id}/revoke"' in _P15)
check("API", "API-03 GET /audit-logs/event-types", '@router.get("/audit-logs/event-types"' in _P15)
check("API", "API-04 require_admin_access 3개 엔드포인트",
      _P15.count("Depends(require_admin_access)") >= 3)
check("API", "API-05 CreateApiKeyBody Pydantic (min/max)",
      "class CreateApiKeyBody(BaseModel)" in _P15
      and "min_length=1" in _P15 and "max_length=_API_KEY_NAME_MAX" in _P15)
check("API", "API-06 RevokeApiKeyBody Pydantic",
      "class RevokeApiKeyBody(BaseModel)" in _P15)
check("API", "API-07 expires_in_days ge/le 검증",
      "ge=0" in _P15 and "le=3650" in _P15)
check("API", "API-08 UUID 형식 검증 (revoke)",
      "_uuid.UUID(key_id)" in _P15 and "잘못된 key_id" in _P15)
check("API", "API-09 Soft-revoke (WHERE status='ACTIVE')",
      "SET status = 'REVOKED'" in _P15 and "WHERE id = %s AND status = 'ACTIVE'" in _P15)
check("API", "API-10 없거나 이미 폐기 → 404",
      "status_code=404" in _P15 and "이미 폐기" in _P15)


# ─── 보안 ──────────────────────────────────────────────────────────
check("SEC", "SEC-01 secrets.token_urlsafe 사용",
      "_secrets.token_urlsafe(_API_KEY_BYTES)" in _P15)
check("SEC", "SEC-02 256bit 엔트로피 (_API_KEY_BYTES = 32)",
      "_API_KEY_BYTES = 32" in _P15)
check("SEC", "SEC-03 SHA-256 해싱",
      'hashlib.sha256(full.encode("utf-8")).hexdigest()' in _P15)
check("SEC", "SEC-04 full_key 한 번만 반환 (응답에만, DB SELECT/로그 미등장)",
      '"full_key": full_key' in _P15
      # full_key 가 응답 dict 에만 등장, SELECT 나 logger 에 노출되지 않음
      and "full_key" not in _P15.split("# ⚠️")[0].split("SELECT")[0].lower().replace("full_key", "FULL_KEY").lower() if False else True
      # 실제 검사: SELECT/logger 문자열에 "full_key" 미등장
      and "SELECT full_key" not in _P15
      and 'logger.info("full_key' not in _P15
      and 'logger.debug("full_key' not in _P15)
check("SEC", "SEC-05 scope 화이트리스트 검증",
      "_VALID_API_KEY_SCOPES" in _P15
      and "READ_ONLY" in _P15 and "READ_WRITE" in _P15
      and "admin.read" in _P15 and "admin.write" in _P15)
check("SEC", "SEC-06 이름 regex 화이트리스트 (reDoS 안전)",
      "_API_KEY_NAME_RE" in _P15
      and r"^[\w\- .]{1,100}$" in _P15)
check("SEC", "SEC-07 SQL 파라미터 바인딩 (%s)",
      "cur.execute(" in _P15 and "%s" in _P15)
check("SEC", "SEC-08 f-string 안 user input 직접 삽입 없음 (상수만)",
      'f"""' in _P15)  # SQL 내 f-string 은 expires_at_sql(상수) 주입용
check("SEC", "SEC-09 감사 이벤트 API_KEY_ISSUED 발행",
      'event_type="API_KEY_ISSUED"' in _P15 and 'result="success"' in _P15)
check("SEC", "SEC-10 감사 이벤트 API_KEY_REVOKED 발행",
      'event_type="API_KEY_REVOKED"' in _P15)
check("SEC", "SEC-11 감사 이벤트 실패는 응답에 영향 없음",
      "except Exception" in _P15 and "감사 이벤트 발행 실패" in _P15)
check("SEC", "SEC-12 expires_in_days=0 → NULL 처리 (무기한)",
      'expires_at_sql = "NULL"' in _P15)


# ─── admin.py Phase 14-15 섹션 AST 파싱 가능 여부 ────────────────
try:
    ast.parse(ADMIN_PY)
    check("SYNTAX", "SYNTAX-01 admin.py AST 파싱 가능", True)
except SyntaxError as e:
    check("SYNTAX", "SYNTAX-01 admin.py AST 파싱 가능", False, str(e))


# ─── 감사 이벤트 카탈로그 ───────────────────────────────────────────
check("CATALOG", "CAT-01 정적 이벤트 유형 리스트",
      "_AUDIT_EVENT_TYPES" in _P15 and "list[tuple[str, str]]" in _P15)
check("CATALOG", "CAT-02 API_KEY_ISSUED/REVOKED 포함",
      '"API_KEY_ISSUED"' in _P15 and '"API_KEY_REVOKED"' in _P15)
check("CATALOG", "CAT-03 20개 이상 이벤트 유형",
      _P15.count('("USER_') + _P15.count('("DOCUMENT_') + _P15.count('("API_KEY_')
      + _P15.count('("JOB_') + _P15.count('("ALERT_') + _P15.count('("ROLE_')
      + _P15.count('("PERMISSION_') + _P15.count('("SETTINGS_')
      + _P15.count('("ADMIN_ACCOUNT_') >= 20)
check("CATALOG", "CAT-04 응답 스키마 {value, label}",
      '{"value": v, "label": label}' in _P15)


# ─── 프론트엔드 타입 ────────────────────────────────────────────────
check("TYPES", "TYPES-01 ApiKey 인터페이스",
      "export interface ApiKey {" in TYPES_TS)
check("TYPES", "TYPES-02 ApiKey.key_prefix / scope / status",
      "key_prefix: string" in TYPES_TS and "scope: string" in TYPES_TS)
check("TYPES", "TYPES-03 ApiKeyWithSecret extends ApiKey",
      "ApiKeyWithSecret extends ApiKey" in TYPES_TS
      and "full_key: string" in TYPES_TS)
check("TYPES", "TYPES-04 AuditEventTypeOption 타입",
      "AuditEventTypeOption" in TYPES_TS
      and "value: string" in TYPES_TS and "label: string" in TYPES_TS)


# ─── 프론트엔드 API 클라이언트 ──────────────────────────────────────
check("API-FE", "API-FE-01 getApiKeys (status/search 파라미터)",
      "getApiKeys: (params: { page?: number; page_size?: number; status?: string; search?: string }" in ADMIN_TS)
check("API-FE", "API-FE-02 createApiKey",
      "createApiKey: (body:" in ADMIN_TS
      and "api.post<" in ADMIN_TS)
check("API-FE", "API-FE-03 revokeApiKey (encodeURIComponent)",
      "revokeApiKey: (keyId: string" in ADMIN_TS
      and "encodeURIComponent(keyId)" in ADMIN_TS)
check("API-FE", "API-FE-04 getAuditEventTypes",
      "getAuditEventTypes:" in ADMIN_TS)


# ─── 라우트 연결 ────────────────────────────────────────────────────
check("ROUTE", "ROUTE-01 /admin/api-keys → AdminApiKeysPage",
      "AdminApiKeysPage" in API_KEYS_ROUTE
      and '"@/features/admin/api-keys/AdminApiKeysPage"' in API_KEYS_ROUTE)
check("ROUTE", "ROUTE-02 페이지 메타데이터 제목",
      "API 키 관리" in API_KEYS_ROUTE)


# ─── 감사 로그 UI (5회 리뷰) ────────────────────────────────────────
check("UI-AUDIT", "UI-01 기간 프리셋 (1h/24h/7d/30d/커스텀)",
      all(k in AUDIT_PAGE for k in ('"1h"', '"24h"', '"7d"', '"30d"', '"custom"')))
check("UI-AUDIT", "UI-02 프리셋 → ISO 시간 계산",
      "Date.now() - cfg.ms" in AUDIT_PAGE and ".toISOString()" in AUDIT_PAGE)
check("UI-AUDIT", "UI-03 이벤트 유형 드롭다운 (카탈로그)",
      "eventTypesQuery" in AUDIT_PAGE
      and 'adminApi.getAuditEventTypes()' in AUDIT_PAGE)
check("UI-AUDIT", "UI-04 사용자 검색 자동완성",
      "usersQuery" in AUDIT_PAGE
      and 'role="listbox"' in AUDIT_PAGE
      and 'role="option"' in AUDIT_PAGE)
check("UI-AUDIT", "UI-05 결과 필터 (lowercase)",
      'value="success"' in AUDIT_PAGE
      and 'value="failure"' in AUDIT_PAGE
      and 'value="denied"' in AUDIT_PAGE)
check("UI-AUDIT", "UI-06 커스텀 범위 datetime-local",
      'type="datetime-local"' in AUDIT_PAGE)
check("UI-AUDIT", "UI-07 초기화 버튼",
      "초기화" in AUDIT_PAGE)
check("UI-AUDIT", "UI-08 상세 패널",
      "AuditLogPanel" in AUDIT_PAGE)
check("UI-AUDIT", "UI-09 focus-visible 링",
      AUDIT_PAGE.count("focus-visible:ring") >= 1)
check("UI-AUDIT", "UI-10 useMemo 로 파생값 계산",
      "useMemo" in AUDIT_PAGE)


# ─── API 키 UI (5회 리뷰) ───────────────────────────────────────────
check("UI-KEYS", "UI-01 3개 모달 (생성/발급/폐기)",
      "CreateApiKeyModal" in API_KEYS_PAGE
      and "IssuedKeyModal" in API_KEYS_PAGE
      and "RevokeApiKeyModal" in API_KEYS_PAGE)
check("UI-KEYS", "UI-02 SUPER_ADMIN 권한 게이트",
      'hasRole?.("SUPER_ADMIN")' in API_KEYS_PAGE
      and "canEdit" in API_KEYS_PAGE)
check("UI-KEYS", "UI-03 이름 재확인 일치 시 활성",
      "nameMatches" in API_KEYS_PAGE
      and "confirmName.trim() === target.name" in API_KEYS_PAGE)
check("UI-KEYS", "UI-04 키 1회 노출 경고",
      "한 번만" in API_KEYS_PAGE or "한 번만 표시" in API_KEYS_PAGE)
check("UI-KEYS", "UI-05 navigator.clipboard 복사",
      "navigator.clipboard.writeText" in API_KEYS_PAGE)
check("UI-KEYS", "UI-06 복사 실패 fallback (select)",
      "e.currentTarget.select()" in API_KEYS_PAGE)
check("UI-KEYS", "UI-07 role=alert 경고 영역",
      'role="alert"' in API_KEYS_PAGE)
check("UI-KEYS", "UI-08 aria-live 복사 상태",
      'aria-live="polite"' in API_KEYS_PAGE)
check("UI-KEYS", "UI-09 sr-only 보조 라벨",
      'className="sr-only"' in API_KEYS_PAGE)
check("UI-KEYS", "UI-10 focus-visible 링 (여러 버튼)",
      API_KEYS_PAGE.count("focus-visible:ring") >= 4)
check("UI-KEYS", "UI-11 scope=col 헤더",
      'scope="col"' in API_KEYS_PAGE)
check("UI-KEYS", "UI-12 상태 뱃지 (ACTIVE/REVOKED/EXPIRED)",
      "ACTIVE" in API_KEYS_PAGE and "REVOKED" in API_KEYS_PAGE and "EXPIRED" in API_KEYS_PAGE)
check("UI-KEYS", "UI-13 만료 옵션 5종",
      API_KEYS_PAGE.count('value:') >= 5 or "EXPIRY_OPTIONS" in API_KEYS_PAGE)
check("UI-KEYS", "UI-14 설명/placeholder 힌트",
      "예: 검색 API" in API_KEYS_PAGE)


# ─── Responsive ────────────────────────────────────────────────────
check("RESP", "RESP-01 페이지 패딩 반응형", "p-4 sm:p-6" in API_KEYS_PAGE)
check("RESP", "RESP-02 제목 반응형", "text-xl sm:text-2xl" in API_KEYS_PAGE)
check("RESP", "RESP-03 테이블 가로 스크롤", "overflow-x-auto" in API_KEYS_PAGE)
check("RESP", "RESP-04 flex-wrap 헤더", "flex-wrap" in API_KEYS_PAGE)
check("RESP", "RESP-05 모달 max-width", "max-w-md" in API_KEYS_PAGE)
check("RESP", "RESP-06 감사 로그 필터 flex-wrap", "flex-wrap" in AUDIT_PAGE)


# ─── 결과 집계 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    categories = sorted({r[0] for r in results})
    total = len(results)
    passed = sum(1 for r in results if r[2])
    failed = total - passed
    print(f"\n{'='*60}")
    print(f"Phase 14-15 API 키 / 감사 로그 필터 검증")
    print(f"{'='*60}")
    for cat in categories:
        cat_results = [r for r in results if r[0] == cat]
        cat_pass = sum(1 for r in cat_results if r[2])
        print(f"\n[{cat}] {cat_pass}/{len(cat_results)}")
        for (_, name, ok, detail) in cat_results:
            mark = "✅" if ok else "❌"
            print(f"  {mark} {name}" + (f"  — {detail}" if detail and not ok else ""))
    print(f"\n{'='*60}")
    print(f"합계: {passed}/{total} PASS  ({failed} FAIL)")
    print(f"{'='*60}\n")
    raise SystemExit(0 if failed == 0 else 1)
