"""Phase 14-13 — 알림 관리 시스템 검증 스크립트.

backend/frontend 실제 파일을 읽어 정적 검사만 수행 (DB/HTTP 스텁 불필요).
산출물: 각 체크 항목의 PASS/FAIL 카운트 + 실패 목록.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

ADMIN_PY = (ROOT / "backend/app/api/v1/admin.py").read_text(encoding="utf-8")
CONN_PY = (ROOT / "backend/app/db/connection.py").read_text(encoding="utf-8")
REPO_PY = (ROOT / "backend/app/repositories/alert_repository.py").read_text(encoding="utf-8")
EVAL_PY = (ROOT / "backend/app/services/alert_evaluator.py").read_text(encoding="utf-8")
NOTIFY_PY = (ROOT / "backend/app/services/alert_notifier.py").read_text(encoding="utf-8")

ALERTS_PAGE = (ROOT / "frontend/src/features/admin/alerts/AdminAlertsPage.tsx").read_text(encoding="utf-8")
ADMIN_TS = (ROOT / "frontend/src/lib/api/admin.ts").read_text(encoding="utf-8")
TYPES_TS = (ROOT / "frontend/src/types/admin.ts").read_text(encoding="utf-8")
ROUTE_PAGE = (ROOT / "frontend/src/app/admin/alerts/page.tsx").read_text(encoding="utf-8")

# Phase 14-13 전용 라우트 섹션만 추출 (다른 Phase 와 오탐 방지)
_ALERT_SECTION = ADMIN_PY.split("Phase 14-13:")[-1] if "Phase 14-13:" in ADMIN_PY else ""

results: list[tuple[str, str, bool, str]] = []


def check(category: str, name: str, cond: bool, detail: str = "") -> None:
    results.append((category, name, bool(cond), detail))


# ─── DDL ────────────────────────────────────────────────────────────
check("DDL", "DDL-01 alert_rules 테이블", "CREATE TABLE IF NOT EXISTS alert_rules" in CONN_PY)
check("DDL", "DDL-02 alert_history 테이블", "CREATE TABLE IF NOT EXISTS alert_history" in CONN_PY)
check("DDL", "DDL-03 알림 규칙 인덱스 (enabled, metric)",
      "idx_alert_rules_enabled_metric" in CONN_PY)
check("DDL", "DDL-04 이력 상태 인덱스",
      "idx_alert_history_status" in CONN_PY)
check("DDL", "DDL-05 firing 중복 방지 Unique 인덱스",
      "idx_alert_history_one_firing_per_rule" in CONN_PY and "WHERE status = 'firing'" in CONN_PY)
check("DDL", "DDL-06 rule_id FK + CASCADE",
      "REFERENCES alert_rules(id) ON DELETE CASCADE" in CONN_PY)
check("DDL", "DDL-07 init_db 등록",
      "_ALERT_RULES_DDL" in CONN_PY and "_ALERT_HISTORY_DDL" in CONN_PY)
check("DDL", "DDL-08 JSONB 컬럼 사용 (condition/channels)",
      "JSONB NOT NULL" in CONN_PY
      and "notified_channels JSONB" in CONN_PY
      and "channels     JSONB" in CONN_PY)

# ─── Repository ─────────────────────────────────────────────────────
check("REPO", "REPO-01 AlertRepository 클래스", "class AlertRepository" in REPO_PY)
check("REPO", "REPO-02 list_rules/get_rule/create_rule/update_rule/delete_rule",
      all(m in REPO_PY for m in ("def list_rules", "def get_rule", "def create_rule",
                                  "def update_rule", "def delete_rule")))
check("REPO", "REPO-03 list_history/get_firing_history/insert_firing/resolve_firing/acknowledge",
      all(m in REPO_PY for m in ("def list_history", "def get_firing_history",
                                  "def insert_firing", "def resolve_firing", "def acknowledge")))
check("REPO", "REPO-04 업데이트 필드 화이트리스트",
      "allowed = {" in REPO_PY and '"name"' in REPO_PY and '"enabled"' in REPO_PY)
check("REPO", "REPO-05 JSONB 캐스트 사용",
      "%s::jsonb" in REPO_PY)
check("REPO", "REPO-06 파라미터 바인딩 (%s)",
      REPO_PY.count("cur.execute(") >= 8 and "%s" in REPO_PY)
check("REPO", "REPO-07 f-string SQL 에 사용자 입력 미삽입",
      "f\"UPDATE alert_rules" not in REPO_PY  # f-string UPDATE 는 허용 컬럼명만
      and "f\"INSERT" not in REPO_PY)
check("REPO", "REPO-08 싱글턴 export", "alert_repository = AlertRepository()" in REPO_PY)
check("REPO", "REPO-09 json.dumps JSONB 직렬화",
      "json.dumps(condition)" in REPO_PY and "json.dumps(channels)" in REPO_PY)

# ─── Evaluator ─────────────────────────────────────────────────────
def _has_eval_or_exec_call(src: str) -> bool:
    """AST 기반 실제 eval()/exec() 호출 탐지 (주석·문자열 무시)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return True  # fail-safe
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("eval", "exec"):
                return True
    return False


check("EVAL", "EVAL-01 eval/exec 미사용",
      not _has_eval_or_exec_call(EVAL_PY))
check("EVAL", "EVAL-02 operator 화이트리스트 dict",
      "_OPS" in EVAL_PY and "operator.gt" in EVAL_PY and "operator.ge" in EVAL_PY)
check("EVAL", "EVAL-03 6개 연산자 지원",
      all(k in EVAL_PY for k in ('"gt"', '"gte"', '"lt"', '"lte"', '"eq"', '"ne"')))
check("EVAL", "EVAL-04 메트릭 화이트리스트",
      "_METRIC_LABELS" in EVAL_PY
      and "api.response_time_p95" in EVAL_PY
      and "api.error_rate_5xx" in EVAL_PY
      and "db.connection_pool_usage" in EVAL_PY)
check("EVAL", "EVAL-05 알 수 없는 메트릭 → None",
      "return None" in EVAL_PY)
check("EVAL", "EVAL-06 firing 중복 방지 체크",
      "get_firing_history" in EVAL_PY and "if existing:" in EVAL_PY)
check("EVAL", "EVAL-07 resolve 전이 로직",
      "_resolve_if_firing" in EVAL_PY and "resolve_firing" in EVAL_PY)
check("EVAL", "EVAL-08 통계 반환 (evaluated/fired/resolved/skipped)",
      all(s in EVAL_PY for s in ('"evaluated"', '"fired"', '"resolved"', '"skipped"')))
check("EVAL", "EVAL-09 메트릭 조회 파라미터 바인딩 (f-string 미사용)",
      'cur.execute(f"' not in EVAL_PY)
check("EVAL", "EVAL-10 예외 격리 (except Exception)",
      "except Exception" in EVAL_PY)
check("EVAL", "EVAL-11 싱글턴 export", "alert_evaluator = AlertEvaluator()" in EVAL_PY)

# ─── Notifier (SSRF 방어) ───────────────────────────────────────────
check("NOTIFY", "NOTIFY-01 AlertNotifier 클래스", "class AlertNotifier" in NOTIFY_PY)
check("NOTIFY", "NOTIFY-02 허용 스킴 화이트리스트 (http/https)",
      "_ALLOWED_WEBHOOK_SCHEMES" in NOTIFY_PY and '"https"' in NOTIFY_PY and '"http"' in NOTIFY_PY)
check("NOTIFY", "NOTIFY-03 내부 호스트 차단",
      "_BLOCKED_HOST_PREFIXES" in NOTIFY_PY
      and '"localhost"' in NOTIFY_PY and '"127."' in NOTIFY_PY
      and '"169.254."' in NOTIFY_PY and '"10."' in NOTIFY_PY
      and '"192.168."' in NOTIFY_PY)
check("NOTIFY", "NOTIFY-04 URL 안전 검증 함수",
      "_is_safe_webhook_url" in NOTIFY_PY)
check("NOTIFY", "NOTIFY-05 웹훅 타임아웃 10초",
      "_WEBHOOK_TIMEOUT_SECONDS = 10" in NOTIFY_PY)
check("NOTIFY", "NOTIFY-06 TLS context (ssl.create_default_context)",
      "ssl.create_default_context()" in NOTIFY_PY)
check("NOTIFY", "NOTIFY-07 이메일 수신자 상한 (DoS 방어)",
      "recipients[:10]" in NOTIFY_PY)
check("NOTIFY", "NOTIFY-08 실패 격리 (try/except)",
      "except Exception" in NOTIFY_PY)
check("NOTIFY", "NOTIFY-09 이메일 is_configured 체크",
      "self._email.is_configured" in NOTIFY_PY)
check("NOTIFY", "NOTIFY-10 싱글턴 export",
      "alert_notifier = AlertNotifier()" in NOTIFY_PY)
check("NOTIFY", "NOTIFY-11 외부 HTTP 라이브러리 미추가 (urllib 사용)",
      "import urllib.request" in NOTIFY_PY
      and "import httpx" not in NOTIFY_PY
      and "import requests" not in NOTIFY_PY)

# ─── API ────────────────────────────────────────────────────────────
check("API", "API-01 /alerts/metrics 라우트",
      '@router.get("/alerts/metrics"' in _ALERT_SECTION)
check("API", "API-02 /alerts/rules 목록",
      '@router.get("/alerts/rules"' in _ALERT_SECTION)
check("API", "API-03 /alerts/rules 생성",
      '@router.post("/alerts/rules"' in _ALERT_SECTION)
check("API", "API-04 /alerts/rules/{id} 상세",
      '@router.get("/alerts/rules/{rule_id}"' in _ALERT_SECTION)
check("API", "API-05 /alerts/rules/{id} 수정 (PATCH)",
      '@router.patch("/alerts/rules/{rule_id}"' in _ALERT_SECTION)
check("API", "API-06 /alerts/rules/{id} 삭제",
      '@router.delete("/alerts/rules/{rule_id}"' in _ALERT_SECTION)
check("API", "API-07 /alerts/history 조회",
      '@router.get("/alerts/history"' in _ALERT_SECTION)
check("API", "API-08 /alerts/history/{id}/acknowledge",
      '@router.post("/alerts/history/{history_id}/acknowledge"' in _ALERT_SECTION)
check("API", "API-09 /alerts/evaluate 수동 평가",
      '@router.post("/alerts/evaluate"' in _ALERT_SECTION)
check("API", "API-10 require_admin_access 가드 (9개)",
      _ALERT_SECTION.count("Depends(require_admin_access)") >= 9)
check("API", "API-11 UUID 형식 검증",
      "re.match(r\"^[0-9a-f\\-]{36}$\"" in _ALERT_SECTION)
check("API", "API-12 Pydantic Body 모델",
      "CreateAlertRuleBody" in _ALERT_SECTION and "UpdateAlertRuleBody" in _ALERT_SECTION)
check("API", "API-13 Severity/Channel/Status/Operator 화이트리스트",
      all(s in _ALERT_SECTION for s in (
          "_ALERT_SEVERITIES", "_ALERT_CHANNELS", "_ALERT_STATUSES", "_ALERT_OPERATORS")))
check("API", "API-14 422 반환 (검증 실패)", "status_code=422" in _ALERT_SECTION)
check("API", "API-15 404 반환 (리소스 없음)", "status_code=404" in _ALERT_SECTION)
check("API", "API-16 audit 이벤트 발행 (생성/수정/삭제/확인)",
      all(ev in _ALERT_SECTION for ev in (
          "ALERT_RULE_CREATED", "ALERT_RULE_UPDATED",
          "ALERT_RULE_DELETED", "ALERT_ACKNOWLEDGED")))
check("API", "API-17 페이지네이션 (page/page_size)",
      'Query(default=1, ge=1' in _ALERT_SECTION and 'Query(default=50' in _ALERT_SECTION)
check("API", "API-18 수동 평가 시 통계 반환",
      "alert_evaluator.evaluate_all()" in _ALERT_SECTION)

# ─── Security ──────────────────────────────────────────────────────
check("SEC", "SEC-01 eval/exec 금지",
      not _has_eval_or_exec_call(EVAL_PY)
      and not _has_eval_or_exec_call(REPO_PY)
      and not _has_eval_or_exec_call(NOTIFY_PY))
check("SEC", "SEC-02 메트릭 화이트리스트 검증 (422)",
      "_METRIC_LABELS" in _ALERT_SECTION and "지원하지 않는 메트릭" in _ALERT_SECTION)
check("SEC", "SEC-03 연산자 화이트리스트 검증",
      "지원하지 않는 연산자" in _ALERT_SECTION)
check("SEC", "SEC-04 채널 화이트리스트 검증",
      "지원하지 않는 채널" in _ALERT_SECTION)
check("SEC", "SEC-05 심각도 화이트리스트 검증",
      "지원하지 않는 심각도" in _ALERT_SECTION)
check("SEC", "SEC-06 UUID 경로 파라미터 2중 검증 (Pydantic + 정규식)",
      "re.match" in _ALERT_SECTION and "유효하지 않은" in _ALERT_SECTION)
check("SEC", "SEC-07 SSRF 방어 적용",
      "_is_safe_webhook_url" in NOTIFY_PY)
check("SEC", "SEC-08 SQL 파라미터 바인딩 (repo)",
      "cur.execute(\n" in REPO_PY and REPO_PY.count("%s") >= 15)
check("SEC", "SEC-09 JSON 직렬화 시 json.dumps 경유 (stored XSS 방어)",
      "json.dumps" in REPO_PY)
check("SEC", "SEC-10 API Key 스코프 분리 (admin.read / admin.write)",
      "require_admin_access" in _ALERT_SECTION)
check("SEC", "SEC-11 웹훅 타임아웃 (DoS 방어)",
      "timeout=_WEBHOOK_TIMEOUT_SECONDS" in NOTIFY_PY)
check("SEC", "SEC-12 이메일 수신자 상한 (DoS 방어)",
      "recipients[:10]" in NOTIFY_PY)
check("SEC", "SEC-13 UI encodeURIComponent 경로 파라미터",
      "encodeURIComponent(ruleId)" in ADMIN_TS and "encodeURIComponent(historyId)" in ADMIN_TS)

# ─── Frontend types ────────────────────────────────────────────────
check("TYPES", "TYPES-01 AlertSeverity 타입",
      "AlertSeverity" in TYPES_TS and '"info"' in TYPES_TS and '"warning"' in TYPES_TS and '"critical"' in TYPES_TS)
check("TYPES", "TYPES-02 AlertStatus/Channel/Operator",
      all(t in TYPES_TS for t in ("AlertStatus", "AlertChannel", "AlertOperator")))
check("TYPES", "TYPES-03 AlertRule/AlertCondition 인터페이스",
      "interface AlertRule" in TYPES_TS and "interface AlertCondition" in TYPES_TS)
check("TYPES", "TYPES-04 AlertHistoryItem/Response",
      "interface AlertHistoryItem" in TYPES_TS and "interface AlertHistoryResponse" in TYPES_TS)
check("TYPES", "TYPES-05 AlertMetricsResponse/EvaluateStats",
      "AlertMetricsResponse" in TYPES_TS and "AlertEvaluateStats" in TYPES_TS)

# ─── Frontend API ──────────────────────────────────────────────────
check("API-FE", "API-FE-01 9개 adminApi 메소드",
      all(m in ADMIN_TS for m in (
          "getAlertMetrics", "getAlertRules", "getAlertRule",
          "createAlertRule", "updateAlertRule", "deleteAlertRule",
          "getAlertHistory", "acknowledgeAlert", "evaluateAlertsNow")))
check("API-FE", "API-FE-02 생성/수정 POST/PATCH 메소드",
      "api.post<" in ADMIN_TS and "api.patch<" in ADMIN_TS)

# ─── Route ─────────────────────────────────────────────────────────
check("ROUTE", "ROUTE-01 /admin/alerts 라우트 연결",
      "AdminAlertsPage" in ROUTE_PAGE and '"@/features/admin/alerts' in ROUTE_PAGE)
check("ROUTE", "ROUTE-02 metadata 존재", "export const metadata" in ROUTE_PAGE)
check("ROUTE", "ROUTE-03 placeholder 제거",
      "Task 14-13에서 구현 예정" not in ROUTE_PAGE)

# ─── UI 디자인 (5회 리뷰) ──────────────────────────────────────────
check("UI", "UI-01 탭 role=tablist / role=tab",
      'role="tablist"' in ALERTS_PAGE and 'role="tab"' in ALERTS_PAGE)
check("UI", "UI-02 aria-selected 탭 상태", "aria-selected={" in ALERTS_PAGE)
check("UI", "UI-03 switch 토글 (role=switch + aria-checked)",
      'role="switch"' in ALERTS_PAGE and "aria-checked={" in ALERTS_PAGE)
check("UI", "UI-04 규칙 편집 모달 + 삭제 확인 모달",
      ALERTS_PAGE.count("<Modal") >= 2)
check("UI", "UI-05 destructive 확인", "destructive" in ALERTS_PAGE)
check("UI", "UI-06 SSRF 경고 문구 노출", "SSRF 방어" in ALERTS_PAGE)
check("UI", "UI-07 권한 분리 (canEdit)",
      'hasRole?.("SUPER_ADMIN")' in ALERTS_PAGE or "canEdit" in ALERTS_PAGE)
check("UI", "UI-08 aria-live 카운트 피드백",
      'aria-live="polite"' in ALERTS_PAGE)
check("UI", "UI-09 Severity 3종 색상 코드",
      "bg-blue-100" in ALERTS_PAGE and "bg-amber-100" in ALERTS_PAGE and "bg-red-100" in ALERTS_PAGE)
check("UI", "UI-10 테이블 scope=col", 'scope="col"' in ALERTS_PAGE)
check("UI", "UI-11 role=status 로딩 안내", 'role="status"' in ALERTS_PAGE)
check("UI", "UI-12 role=alert 에러 안내", 'role="alert"' in ALERTS_PAGE)
check("UI", "UI-13 sr-only 라벨", 'className="sr-only"' in ALERTS_PAGE)
check("UI", "UI-14 focus-visible 링", ALERTS_PAGE.count("focus-visible:ring") >= 10)

# ─── Responsive ────────────────────────────────────────────────────
check("RESP", "RESP-01 페이지 패딩 반응형", "p-4 sm:p-6" in ALERTS_PAGE)
check("RESP", "RESP-02 제목 반응형 크기", "text-xl sm:text-2xl" in ALERTS_PAGE)
check("RESP", "RESP-03 모달 grid 반응형", "sm:grid-cols-2" in ALERTS_PAGE)
check("RESP", "RESP-04 테이블 가로 스크롤", "overflow-x-auto" in ALERTS_PAGE)
check("RESP", "RESP-05 헤더 flex-wrap", "flex-wrap" in ALERTS_PAGE)
check("RESP", "RESP-06 최소 터치 타깃", "min-h-[36px]" in ALERTS_PAGE)

# ─── Python 구문 검사 ──────────────────────────────────────────────
for label, src in (("REPO", REPO_PY), ("EVAL", EVAL_PY), ("NOTIFY", NOTIFY_PY)):
    try:
        ast.parse(src)
        check("SYNTAX", f"SYNTAX-{label} Python 파싱 OK", True)
    except SyntaxError as e:
        check("SYNTAX", f"SYNTAX-{label} Python 파싱 OK", False, str(e))


# ─── 결과 집계 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    categories = sorted({r[0] for r in results})
    total = len(results)
    passed = sum(1 for r in results if r[2])
    failed = total - passed
    print(f"\n{'='*60}")
    print(f"Phase 14-13 알림 관리 시스템 검증")
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
