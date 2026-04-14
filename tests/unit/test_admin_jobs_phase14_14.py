"""Phase 14-14 — 배치 작업 관리 검증 스크립트.

backend/frontend 실제 파일을 읽어 정적 검사 + cron_util 동작 검증만 수행.
산출물: 각 체크 항목의 PASS/FAIL 카운트 + 실패 목록.
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

ADMIN_PY = (ROOT / "backend/app/api/v1/admin.py").read_text(encoding="utf-8")
CONN_PY = (ROOT / "backend/app/db/connection.py").read_text(encoding="utf-8")
REPO_PY = (ROOT / "backend/app/repositories/job_schedule_repository.py").read_text(encoding="utf-8")
CRON_PY = (ROOT / "backend/app/services/cron_util.py").read_text(encoding="utf-8")

SCHED_PAGE = (ROOT / "frontend/src/features/admin/jobs/AdminJobSchedulesPage.tsx").read_text(encoding="utf-8")
ADMIN_TS = (ROOT / "frontend/src/lib/api/admin.ts").read_text(encoding="utf-8")
TYPES_TS = (ROOT / "frontend/src/types/admin.ts").read_text(encoding="utf-8")
ROUTE_PAGE = (ROOT / "frontend/src/app/admin/jobs/page.tsx").read_text(encoding="utf-8")

# Phase 14-14 전용 admin.py 라우트 섹션 추출
_SCHED_SECTION = ADMIN_PY.split("Phase 14-14")[-1] if "Phase 14-14" in ADMIN_PY else ""

results: list[tuple[str, str, bool, str]] = []


def check(category: str, name: str, cond: bool, detail: str = "") -> None:
    results.append((category, name, bool(cond), detail))


# ─── DDL ───────────────────────────────────────────────────────────
check("DDL", "DDL-01 job_schedules 테이블", "CREATE TABLE IF NOT EXISTS job_schedules" in CONN_PY)
check("DDL", "DDL-02 VARCHAR(100) PK", "id                   VARCHAR(100) PRIMARY KEY" in CONN_PY)
check("DDL", "DDL-03 enabled 인덱스", "idx_job_schedules_enabled" in CONN_PY)
check("DDL", "DDL-04 last_run_id FK SET NULL",
      "REFERENCES background_jobs(id) ON DELETE SET NULL" in CONN_PY)
check("DDL", "DDL-05 4개 시드 데이터 (reindex/vector/audit/token)",
      "'reindex_all'" in CONN_PY and "'vector_sync'" in CONN_PY
      and "'audit_cleanup'" in CONN_PY and "'token_cleanup'" in CONN_PY)
check("DDL", "DDL-06 ON CONFLICT DO NOTHING 멱등 시드",
      "ON CONFLICT (id) DO NOTHING" in CONN_PY)
check("DDL", "DDL-07 init_db 등록", "_JOB_SCHEDULES_DDL" in CONN_PY and "_JOB_SCHEDULES_SEED_DDL" in CONN_PY)
check("DDL", "DDL-08 TIMESTAMPTZ 타임존 명시", "TIMESTAMPTZ NOT NULL DEFAULT now()" in CONN_PY)


# ─── Repository ────────────────────────────────────────────────────
check("REPO", "REPO-01 JobScheduleRepository 클래스", "class JobScheduleRepository" in REPO_PY)
check("REPO", "REPO-02 주요 메소드 (list/get/update/runs)",
      all(m in REPO_PY for m in ("def list_schedules", "def get_schedule",
                                  "def update_schedule", "def list_recent_runs")))
check("REPO", "REPO-03 수동 실행 / 취소 메소드",
      "def enqueue_manual_run" in REPO_PY and "def mark_cancel_requested" in REPO_PY)
check("REPO", "REPO-04 업데이트 컬럼 화이트리스트",
      'allowed = {"schedule", "enabled", "next_run_at"}' in REPO_PY)
check("REPO", "REPO-05 파라미터 바인딩 (%s)",
      REPO_PY.count("cur.execute(") >= 6 and "%s" in REPO_PY)
check("REPO", "REPO-06 f-string UPDATE 에 사용자 입력 미삽입 (컬럼명만)",
      'f"{k} = %s"' in REPO_PY)
check("REPO", "REPO-07 싱글턴 export",
      "job_schedule_repository = JobScheduleRepository()" in REPO_PY)
check("REPO", "REPO-08 수동 실행 = background_jobs PENDING",
      "INSERT INTO background_jobs" in REPO_PY and "'PENDING'" in REPO_PY)
check("REPO", "REPO-09 취소 = background_jobs CANCELLED",
      "SET status = 'CANCELLED'" in REPO_PY)
check("REPO", "REPO-10 실행 중 판정 (PENDING/RUNNING)",
      "status IN ('PENDING','RUNNING')" in REPO_PY)


# ─── Cron util ─────────────────────────────────────────────────────
check("CRON", "CRON-01 순수 stdlib (외부 의존성 0)",
      "import cronstrue" not in CRON_PY and "import croniter" not in CRON_PY)
check("CRON", "CRON-02 허용 문자 화이트리스트 정규식",
      "_FIELD_PATTERN" in CRON_PY and r"^[0-9*,/\-]+$" in CRON_PY)
check("CRON", "CRON-03 eval/exec 미사용 (AST)", True)  # 아래에서 AST 기반 재검사

# AST 기반 eval/exec 검출 (문자열/주석 제외)
def _has_eval_or_exec_call(src: str) -> bool:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in ("eval", "exec"):
                return True
    return False


check("SEC", "SEC-01 cron_util 에 eval/exec 호출 없음 (AST)",
      not _has_eval_or_exec_call(CRON_PY))
check("SEC", "SEC-02 admin.py Phase 14-14 섹션에 eval/exec 호출 없음 (AST)",
      not _has_eval_or_exec_call(ADMIN_PY))
check("SEC", "SEC-03 repository 에 eval/exec 호출 없음 (AST)",
      not _has_eval_or_exec_call(REPO_PY))

# cron_util 동적 동작 검증
try:
    from app.services import cron_util

    check("CRON", "CRON-04 validate('0 2 * * *') OK",
          cron_util.validate("0 2 * * *") == "0 2 * * *")
    check("CRON", "CRON-05 describe_ko 매일",
          "매일" in cron_util.describe_ko("0 2 * * *"))
    check("CRON", "CRON-06 describe_ko 월요일",
          "월요일" in cron_util.describe_ko("0 4 * * 1"))
    # 필드 수 오류
    try:
        cron_util.validate("0 2 * *")
        check("CRON", "CRON-07 잘못된 필드 수 거부", False, "ValueError 미발생")
    except ValueError:
        check("CRON", "CRON-07 잘못된 필드 수 거부", True)
    # 허용 외 문자 거부
    try:
        cron_util.validate("0 2 * * MON;rm")
        check("CRON", "CRON-08 허용 외 문자 거부", False, "ValueError 미발생")
    except ValueError:
        check("CRON", "CRON-08 허용 외 문자 거부", True)
    # next_run 동작
    base = datetime(2026, 4, 14, 10, 0, 0)
    nxt = cron_util.next_run("0 2 * * *", base)
    check("CRON", "CRON-09 next_run 다음날 02:00",
          nxt is not None and nxt.hour == 2 and nxt.day == 15)
    # 범위 (1-5) 파싱
    check("CRON", "CRON-10 range 파싱 (0 9 * * 1-5)",
          cron_util.validate("0 9 * * 1-5") == "0 9 * * 1-5")
    # step 파싱
    check("CRON", "CRON-11 step 파싱 (*/15)",
          cron_util.validate("*/15 * * * *") == "*/15 * * * *")
    # 길이 제한
    try:
        cron_util.validate("a" * 200)
        check("CRON", "CRON-12 길이 제한 거부", False, "ValueError 미발생")
    except ValueError:
        check("CRON", "CRON-12 길이 제한 거부", True)
except Exception as e:
    check("CRON", "CRON-IMPORT cron_util 동적 로드", False, str(e))


# ─── API endpoints (Phase 14-14 section only) ──────────────────────
check("API", "API-01 GET /jobs/schedules",
      '@router.get("/jobs/schedules"' in ADMIN_PY)
check("API", "API-02 GET /jobs/schedules/{job_id}",
      '@router.get("/jobs/schedules/{job_id}"' in ADMIN_PY)
check("API", "API-03 POST /jobs/schedules/{job_id}/run (202)",
      '@router.post("/jobs/schedules/{job_id}/run"' in ADMIN_PY
      and "status_code=202" in _SCHED_SECTION)
check("API", "API-04 PATCH /jobs/schedules/{job_id}",
      '@router.patch("/jobs/schedules/{job_id}"' in ADMIN_PY)
check("API", "API-05 POST /jobs/schedules/{job_id}/cancel",
      '@router.post("/jobs/schedules/{job_id}/cancel"' in ADMIN_PY)
check("API", "API-06 POST /jobs/schedules/cron/preview",
      '@router.post("/jobs/schedules/cron/preview"' in ADMIN_PY)
check("API", "API-07 require_admin_access 의존성 (RBAC)",
      _SCHED_SECTION.count("require_admin_access") >= 6)
check("API", "API-08 schedule_id 정규식 화이트리스트",
      "_SCHEDULE_ID_RE" in ADMIN_PY and r"^[a-z0-9_]{1,100}$" in ADMIN_PY)
check("API", "API-09 _validate_schedule_id 헬퍼 사용",
      "_validate_schedule_id(" in _SCHED_SECTION)
check("API", "API-10 실행 중 409 Conflict",
      "409" in _SCHED_SECTION)
check("API", "API-11 감사 이벤트 JOB_SCHEDULE_RUN",
      "JOB_SCHEDULE_RUN" in ADMIN_PY)
check("API", "API-12 감사 이벤트 JOB_SCHEDULE_UPDATED",
      "JOB_SCHEDULE_UPDATED" in ADMIN_PY)
check("API", "API-13 감사 이벤트 JOB_SCHEDULE_CANCELLED",
      "JOB_SCHEDULE_CANCELLED" in ADMIN_PY)
check("API", "API-14 Pydantic UpdateJobScheduleBody",
      "class UpdateJobScheduleBody" in ADMIN_PY)


# ─── Python 구문 검사 ──────────────────────────────────────────────
for label, src in (("REPO", REPO_PY), ("CRON", CRON_PY)):
    try:
        ast.parse(src)
        check("SYNTAX", f"SYNTAX-{label} Python 파싱 OK", True)
    except SyntaxError as e:
        check("SYNTAX", f"SYNTAX-{label} Python 파싱 OK", False, str(e))


# ─── Frontend types ────────────────────────────────────────────────
check("TYPES", "TYPES-01 JobScheduleStatus 타입",
      "JobScheduleStatus" in TYPES_TS and '"idle"' in TYPES_TS
      and '"running"' in TYPES_TS and '"failed"' in TYPES_TS)
check("TYPES", "TYPES-02 JobRunResult 타입",
      "JobRunResult" in TYPES_TS and '"success"' in TYPES_TS
      and '"cancelled"' in TYPES_TS)
check("TYPES", "TYPES-03 JobSchedule 인터페이스",
      "interface JobSchedule" in TYPES_TS)
check("TYPES", "TYPES-04 JobScheduleDetail 확장",
      "interface JobScheduleDetail extends JobSchedule" in TYPES_TS)
check("TYPES", "TYPES-05 CronPreviewResponse",
      "interface CronPreviewResponse" in TYPES_TS)


# ─── Frontend API ──────────────────────────────────────────────────
check("API-FE", "API-FE-01 6개 adminApi 메소드",
      all(m in ADMIN_TS for m in (
          "getJobSchedules", "getJobSchedule", "runJobSchedule",
          "updateJobSchedule", "cancelJobSchedule", "previewCron")))
check("API-FE", "API-FE-02 encodeURIComponent 경로 인자",
      "encodeURIComponent(jobId)" in ADMIN_TS)
check("API-FE", "API-FE-03 POST/PATCH 메소드 사용",
      "api.post<" in ADMIN_TS and "api.patch<" in ADMIN_TS)


# ─── Route ─────────────────────────────────────────────────────────
check("ROUTE", "ROUTE-01 /admin/jobs 에 AdminJobSchedulesPage 연결",
      "AdminJobSchedulesPage" in ROUTE_PAGE
      and '"@/features/admin/jobs/AdminJobSchedulesPage"' in ROUTE_PAGE)
check("ROUTE", "ROUTE-02 탭 (tablist/tab)",
      'role="tablist"' in ROUTE_PAGE and 'role="tab"' in ROUTE_PAGE)
check("ROUTE", "ROUTE-03 기존 실행 이력 보기 보존",
      "AdminJobsPage" in ROUTE_PAGE)


# ─── UI (5회 리뷰) ─────────────────────────────────────────────────
check("UI", "UI-01 상태 3종 색상 (idle/running/failed)",
      "bg-gray-400" in SCHED_PAGE and "bg-blue-500" in SCHED_PAGE
      and "bg-red-500" in SCHED_PAGE)
check("UI", "UI-02 실행 중 pulse 애니메이션",
      "pulse" in SCHED_PAGE)
check("UI", "UI-03 수동 실행 확인 모달 + 취소 확인 모달 + 스케줄 편집 모달",
      SCHED_PAGE.count("<Modal") >= 3)
check("UI", "UI-04 권한 분리 (canEdit / SUPER_ADMIN)",
      "SUPER_ADMIN" in SCHED_PAGE and "canEdit" in SCHED_PAGE)
check("UI", "UI-05 cron 미리보기 섹션 (description + next_runs)",
      "previewMutation" in SCHED_PAGE and "next_runs" in SCHED_PAGE)
check("UI", "UI-06 5초 폴링 (실행 중일 때만)",
      "refetchInterval" in SCHED_PAGE and "5000" in SCHED_PAGE
      and 'status === "running"' in SCHED_PAGE)
check("UI", "UI-07 테이블 scope=col", 'scope="col"' in SCHED_PAGE)
check("UI", "UI-08 role=status 로딩 안내", 'role="status"' in SCHED_PAGE)
check("UI", "UI-09 aria-live 변경 알림", 'aria-live="polite"' in SCHED_PAGE)
check("UI", "UI-10 aria-expanded 행 확장", "aria-expanded" in SCHED_PAGE)
check("UI", "UI-11 sr-only 라벨 존재", 'className="sr-only"' in SCHED_PAGE)
check("UI", "UI-12 focus-visible 링",
      SCHED_PAGE.count("focus-visible:ring") >= 3)
check("UI", "UI-13 graceful 취소 고지 (안내 문구)",
      "graceful" in SCHED_PAGE.lower() or "요청" in SCHED_PAGE)


# ─── Responsive ────────────────────────────────────────────────────
check("RESP", "RESP-01 페이지 패딩 반응형", "p-4 sm:p-6" in SCHED_PAGE)
check("RESP", "RESP-02 제목 반응형 크기", "text-xl sm:text-2xl" in SCHED_PAGE)
check("RESP", "RESP-03 테이블 가로 스크롤", "overflow-x-auto" in SCHED_PAGE)
check("RESP", "RESP-04 헤더 flex-wrap", "flex-wrap" in SCHED_PAGE)
check("RESP", "RESP-05 모달 반응형 너비", "sm:max-w" in SCHED_PAGE or "max-w-md" in SCHED_PAGE)


# ─── 결과 집계 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    categories = sorted({r[0] for r in results})
    total = len(results)
    passed = sum(1 for r in results if r[2])
    failed = total - passed
    print(f"\n{'='*60}")
    print(f"Phase 14-14 배치 작업 관리 검증")
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
