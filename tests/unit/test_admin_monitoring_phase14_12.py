"""Phase 14-12 — 모니터링 대시보드 검증 스크립트.

backend/frontend 실제 파일을 읽어 정적 검사만 수행 (DB/HTTP 스텁 불필요).
산출물: 각 체크 항목의 PASS/FAIL 카운트 + 실패 목록.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ADMIN_PY = (ROOT / "backend/app/api/v1/admin.py").read_text(encoding="utf-8")
MONITOR_PAGE = (ROOT / "frontend/src/features/admin/monitoring/AdminMonitoringPage.tsx").read_text(encoding="utf-8")
LINE_CHART = (ROOT / "frontend/src/features/admin/monitoring/LineChart.tsx").read_text(encoding="utf-8")
ADMIN_TS = (ROOT / "frontend/src/lib/api/admin.ts").read_text(encoding="utf-8")
TYPES_TS = (ROOT / "frontend/src/types/admin.ts").read_text(encoding="utf-8")
ROUTE_PAGE = (ROOT / "frontend/src/app/admin/monitoring/page.tsx").read_text(encoding="utf-8")

results: list[tuple[str, str, bool, str]] = []


def check(category: str, name: str, cond: bool, detail: str = "") -> None:
    results.append((category, name, bool(cond), detail))


# ─── API ────────────────────────────────────────────────────────────
check("API", "API-01 응답시간 라우트", '@router.get("/monitoring/response-times"' in ADMIN_PY)
check("API", "API-02 에러추이 라우트", '@router.get("/monitoring/error-trends"' in ADMIN_PY)
check("API", "API-03 컴포넌트 라우트", '@router.get("/monitoring/components"' in ADMIN_PY)
check("API", "API-04 period 검증", "_validate_period" in ADMIN_PY and "지원하지 않는 period" in ADMIN_PY)
check("API", "API-05 422 반환", "status_code=422" in ADMIN_PY)
check("API", "API-06 4개 period 지원",
      all(p in ADMIN_PY for p in ['"1h":', '"6h":', '"24h":', '"7d":']))
check("API", "API-07 percentile_cont 사용", "percentile_cont(0.5)" in ADMIN_PY and "percentile_cont(0.95)" in ADMIN_PY and "percentile_cont(0.99)" in ADMIN_PY)
check("API", "API-08 generate_series 버킷", "generate_series(" in ADMIN_PY)
check("API", "API-09 make_interval(secs =>)", "make_interval(secs =>" in ADMIN_PY)
check("API", "API-10 require_admin_access 가드",
      ADMIN_PY.count("Depends(require_admin_access)") >= 3)
check("API", "API-11 에러추이 4xx 분류", "action_result IN ('denied','conflict')" in ADMIN_PY)
check("API", "API-12 에러추이 5xx 분류", "action_result = 'failure'" in ADMIN_PY)
check("API", "API-13 job FAILED 반영", "status = 'FAILED'" in ADMIN_PY)
check("API", "API-14 components PostgreSQL", '"PostgreSQL"' in ADMIN_PY)
check("API", "API-15 components Valkey", '"Valkey"' in ADMIN_PY)
check("API", "API-16 components Vector DB", '"Vector DB' in ADMIN_PY)
check("API", "API-17 components Job Runner", '"Job Runner"' in ADMIN_PY or "job_meta" in ADMIN_PY)
check("API", "API-18 latency_ms 측정", "time.perf_counter()" in ADMIN_PY)
check("API", "API-19 에러 메시지 truncate", "str(exc)[:200]" in ADMIN_PY)
check("API", "API-20 Valkey ping", "client.ping()" in ADMIN_PY)

# ─── Security ──────────────────────────────────────────────────────
_MONITOR_SECTION = ADMIN_PY.split("Phase 14-12")[-1] if "Phase 14-12" in ADMIN_PY else ""
check("SEC", "SEC-01 SQL 문자열에 f-string 미사용 (monitoring 3종)",
      'cur.execute(\n                f"""' not in _MONITOR_SECTION
      and 'cur.execute(f"' not in _MONITOR_SECTION)
check("SEC", "SEC-02 period 파라미터 enum 제한",
      '"1h": (60, 60)' in ADMIN_PY or '_MONITORING_PERIODS[period]' in ADMIN_PY)
check("SEC", "SEC-03 SQL 파라미터 바인딩", "cur.execute(" in ADMIN_PY and ", (" in ADMIN_PY)
check("SEC", "SEC-04 에러 Info만 노출", "[:200]" in ADMIN_PY)
check("SEC", "SEC-05 period UI encodeURIComponent", "encodeURIComponent(period)" in ADMIN_TS)
check("SEC", "SEC-06 admin 헤더 적용",
      ADMIN_TS.count("adminHeaders()") >= 3 and "getMonitoringComponents" in ADMIN_TS)
check("SEC", "SEC-07 dangerouslySetInnerHTML 미사용",
      "dangerouslySetInnerHTML" not in MONITOR_PAGE and "dangerouslySetInnerHTML" not in LINE_CHART)
check("SEC", "SEC-08 URL 파라미터 민감 정보 없음",
      "password" not in MONITOR_PAGE.lower() and "token" not in MONITOR_PAGE.lower())

# ─── Frontend API Client ────────────────────────────────────────────
check("FE-API", "FE-API-01 getMonitoringComponents 정의", "getMonitoringComponents" in ADMIN_TS)
check("FE-API", "FE-API-02 getResponseTimeTrend 정의", "getResponseTimeTrend" in ADMIN_TS)
check("FE-API", "FE-API-03 getErrorTrend 정의", "getErrorTrend" in ADMIN_TS)
check("FE-API", "FE-API-04 period 기본값 24h",
      'period: string = "24h"' in ADMIN_TS)

# ─── Types ─────────────────────────────────────────────────────────
check("TYPE", "TYPE-01 ComponentStatus", "export interface ComponentStatus" in TYPES_TS)
check("TYPE", "TYPE-02 ResponseTimePoint", "export interface ResponseTimePoint" in TYPES_TS)
check("TYPE", "TYPE-03 ErrorTrendPoint", "export interface ErrorTrendPoint" in TYPES_TS)
check("TYPE", "TYPE-04 상태 literal union",
      'status: "HEALTHY" | "DOWN" | "UNKNOWN"' in TYPES_TS)
check("TYPE", "TYPE-05 P50/P95/P99 필드",
      "p50: number" in TYPES_TS and "p95: number" in TYPES_TS and "p99: number" in TYPES_TS)

# ─── LineChart Component ───────────────────────────────────────────
check("CHART", "CHART-01 SVG viewBox", "viewBox={" in LINE_CHART and "${width}" in LINE_CHART)
check("CHART", "CHART-02 role=img", 'role="img"' in LINE_CHART)
check("CHART", "CHART-03 aria-label", "aria-label={ariaLabel}" in LINE_CHART)
check("CHART", "CHART-04 범례 표시", "range" in LINE_CHART or "s.label" in LINE_CHART)
check("CHART", "CHART-05 hover 툴팁 aria-live", 'aria-live="polite"' in LINE_CHART)
check("CHART", "CHART-06 빈 데이터 처리", "isEmpty" in LINE_CHART and "데이터" in LINE_CHART)
check("CHART", "CHART-07 로컬 시간대", "toLocaleTimeString" in LINE_CHART)
check("CHART", "CHART-08 다중 시리즈 지원",
      "series.map" in LINE_CHART and "key: string" in LINE_CHART)
check("CHART", "CHART-09 외부 차트 라이브러리 미추가",
      "recharts" not in LINE_CHART and "chart.js" not in LINE_CHART.lower())

# ─── UI Component (AdminMonitoringPage) ────────────────────────────
check("UI", "UI-01 자동 갱신 옵션 4종",
      all(l in MONITOR_PAGE for l in ['10000', '30000', '60000']) and '"수동"' in MONITOR_PAGE)
check("UI", "UI-02 period 탭 4종",
      all(p in MONITOR_PAGE for p in ['"1h"', '"6h"', '"24h"', '"7d"']))
check("UI", "UI-03 컴포넌트 카드 그리드",
      "ComponentCard" in MONITOR_PAGE and "grid-cols" in MONITOR_PAGE)
check("UI", "UI-04 응답시간 차트 배치", "rtSeries" in MONITOR_PAGE and "LineChart" in MONITOR_PAGE)
check("UI", "UI-05 에러 차트 배치", "errSeries" in MONITOR_PAGE)
check("UI", "UI-06 최근 에러 테이블", "recentErrors" in MONITOR_PAGE)
check("UI", "UI-07 에러 배너", 'role="alert"' in MONITOR_PAGE)
check("UI", "UI-08 이전 데이터 유지 (placeholderData)",
      "placeholderData: (prev) => prev" in MONITOR_PAGE)
check("UI", "UI-09 refetchInterval 연결",
      "refetchInterval: refreshInterval" in MONITOR_PAGE)
check("UI", "UI-10 수동 새로고침 버튼", "refetchAll" in MONITOR_PAGE)
check("UI", "UI-11 갱신 카운트다운", "secondsToNext" in MONITOR_PAGE)
check("UI", "UI-12 상태 뱃지 색상 (녹/적/회)",
      "bg-green-500" in MONITOR_PAGE and "bg-red-500" in MONITOR_PAGE and "bg-gray-400" in MONITOR_PAGE)
check("UI", "UI-13 P50/P95/P99 색상",
      '"#22c55e"' in MONITOR_PAGE and '"#f59e0b"' in MONITOR_PAGE and '"#ef4444"' in MONITOR_PAGE)
check("UI", "UI-14 period 탭 aria-pressed",
      'aria-pressed={period === o.value}' in MONITOR_PAGE)

# ─── Accessibility ─────────────────────────────────────────────────
check("A11Y", "A11Y-01 section aria-labelledby",
      MONITOR_PAGE.count("aria-labelledby=") >= 3)
check("A11Y", "A11Y-02 sr-only 라벨", 'className="sr-only"' in MONITOR_PAGE)
check("A11Y", "A11Y-03 aria-live 갱신 안내",
      'aria-live="polite"' in MONITOR_PAGE)
check("A11Y", "A11Y-04 focus-visible 링",
      "focus-visible:ring-2" in MONITOR_PAGE)
check("A11Y", "A11Y-05 scope=col 테이블 헤더",
      'scope="col"' in MONITOR_PAGE)
check("A11Y", "A11Y-06 최소 터치 타깃 40px",
      "min-h-[40px]" in MONITOR_PAGE)
check("A11Y", "A11Y-07 로딩 상태 role=status",
      'role="status"' in MONITOR_PAGE)
check("A11Y", "A11Y-08 컴포넌트 카드 role=group",
      'role="group"' in MONITOR_PAGE)

# ─── Responsive ────────────────────────────────────────────────────
check("RESP", "RESP-01 페이지 패딩 반응형", "p-4 sm:p-6" in MONITOR_PAGE)
check("RESP", "RESP-02 컴포넌트 그리드 반응형",
      "grid-cols-1 sm:grid-cols-2 lg:grid-cols-4" in MONITOR_PAGE)
check("RESP", "RESP-03 차트 2열 → 1열",
      "grid-cols-1 lg:grid-cols-2" in MONITOR_PAGE)
check("RESP", "RESP-04 헤더 flex-wrap", "flex-wrap" in MONITOR_PAGE)
check("RESP", "RESP-05 제목 반응형 크기",
      "text-xl sm:text-2xl" in MONITOR_PAGE)
check("RESP", "RESP-06 테이블 가로 스크롤", "overflow-x-auto" in MONITOR_PAGE)

# ─── Route Wiring ──────────────────────────────────────────────────
check("ROUTE", "ROUTE-01 /admin/monitoring 라우트 연결",
      "AdminMonitoringPage" in ROUTE_PAGE and 'from "@/features/admin/monitoring' in ROUTE_PAGE)
check("ROUTE", "ROUTE-02 metadata 존재", "export const metadata" in ROUTE_PAGE)
check("ROUTE", "ROUTE-03 placeholder 제거",
      "Task 14-12에서 구현 예정" not in ROUTE_PAGE)

# ─── Design (CLAUDE.md: UI 리뷰 5회) ───────────────────────────────
check("DESIGN", "DESIGN-01 신규 외부 라이브러리 미추가 (취약점 차단)",
      "recharts" not in (ROOT / "frontend/package.json").read_text(encoding="utf-8")
      and "chart.js" not in (ROOT / "frontend/package.json").read_text(encoding="utf-8"))


# ─── 결과 집계 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    categories = sorted({r[0] for r in results})
    total = len(results)
    passed = sum(1 for r in results if r[2])
    failed = total - passed
    print(f"\n{'='*60}")
    print(f"Phase 14-12 모니터링 대시보드 검증")
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
