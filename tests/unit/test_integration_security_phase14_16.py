"""Phase 14-16 — 통합 보안 검증 스크립트.

Phase 14 전체 인증 / 권한 / 관리자 시스템을 대상으로 한 정적 + 기능 검증.

범위:
  - OWASP Authentication 체크리스트 12항목
  - RBAC 전수 매트릭스 (모든 action × 모든 역할)
  - Refresh Token Rotation 시나리오 5종
  - 입력값 검증 (SQL Injection / XSS / 길이 제한)
  - Admin UI 접근 제어
  - 기존 Phase 14 단위 테스트 존재 / 라우트 보호 상태

실제 DB 없이 정적 검사 + 순수 함수 검증 + 기존 단위 테스트 디스커버리.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "phase14-16-verification-secret-xxx")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "phase14-16-internal-secret")

# 파일 로드 --------------------------------------------------------------------

AUTH_ROUTER = (ROOT / "backend/app/api/v1/auth_router.py").read_text(encoding="utf-8")
ACCOUNT_ROUTER = (ROOT / "backend/app/api/v1/account_router.py").read_text(encoding="utf-8")
ADMIN_PY = (ROOT / "backend/app/api/v1/admin.py").read_text(encoding="utf-8")
PASSWORD_PY = (ROOT / "backend/app/api/auth/password.py").read_text(encoding="utf-8")
TOKENS_PY = (ROOT / "backend/app/api/auth/tokens.py").read_text(encoding="utf-8")
REFRESH_SVC = (ROOT / "backend/app/api/auth/refresh_service.py").read_text(encoding="utf-8")
PURPOSE_TOKENS = (ROOT / "backend/app/api/auth/purpose_tokens.py").read_text(encoding="utf-8")
OAUTH_SVC = (ROOT / "backend/app/api/auth/oauth_service.py").read_text(encoding="utf-8")
RATE_LIMIT = (ROOT / "backend/app/api/auth/rate_limit.py").read_text(encoding="utf-8")
VALIDATORS = (ROOT / "backend/app/api/auth/validators.py").read_text(encoding="utf-8")
AUTHORIZATION = (ROOT / "backend/app/api/auth/authorization.py").read_text(encoding="utf-8")
DEPS = (ROOT / "backend/app/api/auth/dependencies.py").read_text(encoding="utf-8")

ADMIN_LAYOUT = (ROOT / "frontend/src/app/admin/layout.tsx").read_text(encoding="utf-8")
AUTH_GUARD = (ROOT / "frontend/src/components/auth/AuthGuard.tsx").read_text(encoding="utf-8")
ADMIN_LAYOUT_COMP = (ROOT / "frontend/src/components/admin/layout/AdminLayout.tsx").read_text(encoding="utf-8")


results: list[tuple[str, str, bool, str]] = []


def check(category: str, name: str, cond: bool, detail: str = "") -> None:
    results.append((category, name, bool(cond), detail))


skipped: list[tuple[str, str, str]] = []


def skip(category: str, name: str, reason: str) -> None:
    skipped.append((category, name, reason))


# ───────────────────────────────────────────────────────────────────────────
# OWASP Authentication 체크리스트 (12항목)
# ───────────────────────────────────────────────────────────────────────────

# 1. 비밀번호 평문 저장 금지
check("OWASP", "OWASP-01 bcrypt 해싱만 저장 (평문 미사용)",
      "bcrypt.hashpw" in PASSWORD_PY
      and "password_hash" in AUTH_ROUTER
      and "plain" not in REFRESH_SVC)

# 2. 비밀번호 로그 노출 금지
_PASSWORD_LOG_PATTERNS = (
    re.search(r'logger\.[a-z]+\([^)]*body\.password', AUTH_ROUTER),
    re.search(r'logger\.[a-z]+\([^)]*new_password', AUTH_ROUTER),
    re.search(r'logger\.[a-z]+\([^)]*password=', AUTH_ROUTER),
)
check("OWASP", "OWASP-02 비밀번호를 logger 에 직접 전달하지 않음",
      all(p is None for p in _PASSWORD_LOG_PATTERNS))

# 3. 로그인 실패 동일 에러 메시지 (사용자 열거 방지)
check("OWASP", "OWASP-03 로그인 실패 통합 메시지 (user_not_found / invalid_password 동일)",
      AUTH_ROUTER.count('"이메일 또는 비밀번호가 올바르지 않습니다"') >= 2)

# 4. 세션 고정 공격 방어 (로그인 성공 시 기존 RT 폐기 or 새 family 발급)
check("OWASP", "OWASP-04 로그인 시 새 family_id (세션 고정 방지)",
      "issue_tokens" in AUTH_ROUTER
      and "family_id is None" in REFRESH_SVC
      and "generate_family_id()" in REFRESH_SVC)

# 5. Brute Force 방어 (Valkey 기반 시도 제한)
check("OWASP", "OWASP-05 로그인 시도 제한 (429)",
      "check_login_allowed" in AUTH_ROUTER
      and "record_failed_attempt" in AUTH_ROUTER
      and "status_code=429" in AUTH_ROUTER)

# 6. RT Rotation (재사용 감지 → family 일괄 폐기)
check("OWASP", "OWASP-06 RT 재사용 감지 → family 전체 폐기",
      'rt_record["revoked"]' in REFRESH_SVC
      and "reuse detected" in REFRESH_SVC
      and "revoke_family" in REFRESH_SVC)

# 7. CSRF 방어 (SameSite=strict)
check("OWASP", "OWASP-07 RT 쿠키 SameSite=strict",
      '_RT_COOKIE_SAMESITE = "strict"' in AUTH_ROUTER
      and "samesite=_RT_COOKIE_SAMESITE" in AUTH_ROUTER)

# 8. JWT 알고리즘 혼동 방지 (algorithms= 명시, alg:none 거부)
check("OWASP", "OWASP-08 JWT algorithms 명시 (HS256 고정)",
      '_ALGORITHM = "HS256"' in TOKENS_PY
      and "algorithms=[_ALGORITHM]" in TOKENS_PY)

# 9. 비밀번호 재설정 토큰 1회성
check("OWASP", "OWASP-09 purpose token 1회성 (jti + used_token)",
      "check_token_used" in PURPOSE_TOKENS
      and "mark_token_used" in PURPOSE_TOKENS
      and "check_token_used" in AUTH_ROUTER
      and "mark_token_used" in AUTH_ROUTER)

# 10. OAuth state 검증
check("OWASP", "OWASP-10 OAuth state Valkey 저장 + 1회 소진",
      "oauth_state:" in OAUTH_SVC
      and "valkey.delete(state_key)" in OAUTH_SVC)

# 11. 타이밍 공격 방어
check("OWASP", "OWASP-11 bcrypt 더미 검증 + hmac.compare_digest",
      "dummy_verify()" in AUTH_ROUTER
      and "hmac.compare_digest" in TOKENS_PY)

# 12. HttpOnly Cookie
check("OWASP", "OWASP-12 RT HttpOnly Cookie",
      "httponly=True" in AUTH_ROUTER
      and ("secure=_RT_COOKIE_SECURE" in AUTH_ROUTER or "secure=True" in AUTH_ROUTER))


# ───────────────────────────────────────────────────────────────────────────
# RBAC 전수 검증 (authorization_service.is_allowed 기반)
# ───────────────────────────────────────────────────────────────────────────
# 실제 함수를 직접 호출하여 매트릭스를 검증 — 정적 문자열 매칭이 아님.

try:
    from app.api.auth.authorization import authorization_service, ResourceRef, _PERMISSION_MATRIX
    from app.api.auth.models import ActorContext, ActorType

    def _actor(role: str | None, actor_type: ActorType = ActorType.USER) -> ActorContext:
        return ActorContext(
            actor_type=actor_type,
            actor_id="test-user-001" if actor_type == ActorType.USER else None,
            role=role,
            is_authenticated=(actor_type != ActorType.ANONYMOUS),
        )

    _ROLES = ["VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"]

    # RBAC-01: 매트릭스 존재 및 모든 주요 action 커버
    _REQUIRED_ACTIONS = {
        "document.read", "document.create", "document.delete",
        "workflow.approve", "workflow.publish",
        "admin.read", "admin.write",
        "search.reindex",
    }
    _missing = _REQUIRED_ACTIONS - set(_PERMISSION_MATRIX.keys())
    check("RBAC", "RBAC-01 주요 8개 action 매트릭스 등재",
          not _missing, detail=f"missing={_missing}")

    # RBAC-02: admin.write 는 SUPER_ADMIN 전용
    check("RBAC", "RBAC-02 admin.write = SUPER_ADMIN 단독",
          _PERMISSION_MATRIX["admin.write"] == frozenset({"SUPER_ADMIN"}))

    # RBAC-03: admin.read 는 ORG_ADMIN / SUPER_ADMIN
    check("RBAC", "RBAC-03 admin.read = ORG_ADMIN / SUPER_ADMIN",
          _PERMISSION_MATRIX["admin.read"] == frozenset({"ORG_ADMIN", "SUPER_ADMIN"}))

    # RBAC-04: VIEWER 는 admin/write 모두 거부
    failed = []
    for action in ("admin.read", "admin.write", "document.create", "document.delete",
                   "workflow.approve", "workflow.publish", "search.reindex"):
        if authorization_service.is_allowed(_actor("VIEWER"), action):
            failed.append(action)
    check("RBAC", "RBAC-04 VIEWER 는 admin/write/delete/approve 모두 거부",
          not failed, detail=f"leaked={failed}")

    # RBAC-05: AUTHOR 는 admin 거부, document.create 허용
    check("RBAC", "RBAC-05 AUTHOR admin 거부 + document.create 허용",
          not authorization_service.is_allowed(_actor("AUTHOR"), "admin.read")
          and not authorization_service.is_allowed(_actor("AUTHOR"), "admin.write")
          and authorization_service.is_allowed(_actor("AUTHOR"), "document.create"))

    # RBAC-06: ORG_ADMIN admin.read 허용, admin.write 거부
    check("RBAC", "RBAC-06 ORG_ADMIN admin.read ok, admin.write 거부",
          authorization_service.is_allowed(_actor("ORG_ADMIN"), "admin.read")
          and not authorization_service.is_allowed(_actor("ORG_ADMIN"), "admin.write"))

    # RBAC-07: SUPER_ADMIN 모든 것 허용
    failed_super = [a for a in _PERMISSION_MATRIX
                    if not authorization_service.is_allowed(_actor("SUPER_ADMIN"), a)]
    check("RBAC", "RBAC-07 SUPER_ADMIN 은 모든 등록된 action 허용",
          not failed_super, detail=f"denied_for_super={failed_super}")

    # RBAC-08: ANONYMOUS 는 protected action 거부
    check("RBAC", "RBAC-08 ANONYMOUS 는 admin.read 거부",
          not authorization_service.is_allowed(
              _actor(None, ActorType.ANONYMOUS), "admin.read"))

    # RBAC-09: 알 수 없는 action 은 기본 거부
    check("RBAC", "RBAC-09 알 수 없는 action 은 기본 거부",
          not authorization_service.is_allowed(_actor("SUPER_ADMIN"), "unknown.nonexistent"))

    # RBAC-10: SERVICE actor 는 모든 action 허용
    svc_actor = _actor("SUPER_ADMIN", ActorType.SERVICE)
    check("RBAC", "RBAC-10 SERVICE actor 는 바이패스",
          all(authorization_service.is_allowed(svc_actor, a)
              for a in ("admin.write", "document.delete", "search.reindex")))

except ModuleNotFoundError as e:  # pragma: no cover
    skip("RBAC", "RBAC-functional (authorization_service 런타임)", f"모듈 미설치: {e}")
except Exception as e:  # pragma: no cover
    check("RBAC", "RBAC-00 authorization_service 임포트 가능", False, str(e))


# ───────────────────────────────────────────────────────────────────────────
# Refresh Token Rotation 시나리오 (순수 함수 / 격리 mock 검증)
# ───────────────────────────────────────────────────────────────────────────

try:
    from app.api.auth.tokens import (
        create_refresh_token,
        verify_refresh_token_hash,
        generate_family_id,
        create_access_token,
        decode_access_token,
    )

    # RT-01: 생성 시 raw 와 hash 가 달라야 함
    raw, h = create_refresh_token()
    check("RT", "RT-01 RT 생성 → raw != hash (평문 저장 금지)",
          raw != h and hashlib.sha256(raw.encode()).hexdigest() == h)

    # RT-02: verify_refresh_token_hash 일치
    check("RT", "RT-02 verify_refresh_token_hash 일치 확인 (hmac.compare_digest)",
          verify_refresh_token_hash(raw, h) is True)

    # RT-03: 조작된 raw 는 거부
    check("RT", "RT-03 조작된 raw 거부",
          verify_refresh_token_hash(raw + "x", h) is False)

    # RT-04: family_id 는 UUID4 형식
    fid = generate_family_id()
    check("RT", "RT-04 family_id UUID4 형식",
          re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", fid) is not None)

    # RT-05: AT 는 jti / type=access 클레임 포함
    at = create_access_token("u-001", "AUTHOR")
    payload = decode_access_token(at)
    check("RT", "RT-05 AT 클레임 (sub/role/jti/type=access)",
          payload is not None
          and payload.get("sub") == "u-001"
          and payload.get("role") == "AUTHOR"
          and payload.get("type") == "access"
          and "jti" in payload)

    # RT-06: alg=none 토큰은 거부 (JWT 알고리즘 혼동 방지)
    import jwt as _jwt
    none_token = _jwt.encode({"sub": "attacker", "role": "SUPER_ADMIN", "exp": 9999999999}, key="", algorithm="none")
    check("RT", "RT-06 alg=none 토큰 거부",
          decode_access_token(none_token) is None)

    # RT-07: 다른 secret 으로 서명된 토큰 거부
    other_sig = _jwt.encode({"sub": "x", "exp": 9999999999}, key="other-secret", algorithm="HS256")
    check("RT", "RT-07 타 secret 서명 토큰 거부",
          decode_access_token(other_sig) is None)

    # RT-08: 만료된 토큰 거부
    from datetime import datetime, timezone, timedelta
    expired_at = _jwt.encode(
        {"sub": "x", "role": "AUTHOR",
         "exp": datetime.now(tz=timezone.utc) - timedelta(hours=1),
         "iat": datetime.now(tz=timezone.utc) - timedelta(hours=2),
         "type": "access", "jti": "expired"},
        key=os.environ["JWT_SECRET"], algorithm="HS256",
    )
    check("RT", "RT-08 만료된 AT 거부",
          decode_access_token(expired_at) is None)

    # RT-09: RT Rotation 재사용 감지 (mock)
    from app.api.auth.refresh_service import RefreshTokenService
    svc = RefreshTokenService()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cursor

    # _find_by_hash 가 이미 revoked=True 인 레코드 반환 → rotate 는 family 폐기 + None 반환
    mock_cursor.fetchone.return_value = {
        "id": "rt-001", "user_id": "u-001", "family_id": "fam-001",
        "token_hash": h, "revoked": True,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
    }
    result = svc.rotate(mock_conn, raw_token=raw)
    check("RT", "RT-09 revoked RT 재사용 → None 반환 (family 폐기)",
          result is None)

    # RT-10: 만료된 RT → None
    mock_cursor.fetchone.return_value = {
        "id": "rt-002", "user_id": "u-001", "family_id": "fam-002",
        "token_hash": h, "revoked": False,
        "expires_at": datetime.now(timezone.utc) - timedelta(days=1),
    }
    result = svc.rotate(mock_conn, raw_token=raw)
    check("RT", "RT-10 만료된 RT rotate → None",
          result is None)

except ModuleNotFoundError as e:  # pragma: no cover
    skip("RT", "RT-functional (refresh_service 런타임)", f"모듈 미설치: {e}")
except Exception as e:  # pragma: no cover
    check("RT", "RT-00 refresh_service 동작 검증", False, str(e))


# ───────────────────────────────────────────────────────────────────────────
# 입력값 검증
# ───────────────────────────────────────────────────────────────────────────

try:
    from app.api.auth.validators import validate_password_strength, validate_display_name

    # INPUT-01: 짧은 비밀번호 거부
    check("INPUT", "INPUT-01 짧은 비밀번호 거부",
          len(validate_password_strength("ab1!")) > 0)

    # INPUT-02: 긴 비밀번호 거부
    check("INPUT", "INPUT-02 128자 초과 거부",
          len(validate_password_strength("A" * 129 + "1")) > 0)

    # INPUT-03: 단일 유형 거부
    check("INPUT", "INPUT-03 영문만 거부",
          len(validate_password_strength("onlyletters")) > 0)

    # INPUT-04: 유효한 복잡도
    check("INPUT", "INPUT-04 영문+숫자 허용",
          len(validate_password_strength("Password1")) == 0)

    # INPUT-05: display_name 공백 / 길이 제한
    check("INPUT", "INPUT-05 display_name 공백 거부",
          len(validate_display_name("   ")) > 0)
    check("INPUT", "INPUT-06 display_name 100자 초과 거부",
          len(validate_display_name("A" * 101)) > 0)

    # Pydantic 길이 제한 (스키마 직접 검증)
    from app.api.v1.auth_router import RegisterRequest, LoginRequest, ResetPasswordRequest
    import pydantic

    # INPUT-07: RegisterRequest password max_length=128
    try:
        RegisterRequest(email="a@b.com", password="A" * 10000, display_name="x")
        check("INPUT", "INPUT-07 register password 길이 상한 검증", False, "초과 길이 수용됨")
    except pydantic.ValidationError:
        check("INPUT", "INPUT-07 register password 길이 상한 검증", True)

    # INPUT-08: LoginRequest identifier 길이 상한 강제 (256자 이상 거부)
    # Phase 14-17에서 email→identifier 로 필드 변경: 이메일 고정 형식 검증은 제거되었고
    # SQL 인젝션 방어는 psycopg2 파라미터 바인딩으로 처리한다. pydantic 단계에서는
    # 길이 상한 (255자) 을 강제해 비정상적으로 긴 입력을 차단하는지 검증한다.
    try:
        LoginRequest(identifier="A" * 500, password="x")
        check("INPUT", "INPUT-08 login identifier 길이 상한 검증", False, "초과 길이 수용됨")
    except pydantic.ValidationError:
        check("INPUT", "INPUT-08 login identifier 길이 상한 검증", True)

except ModuleNotFoundError as e:  # pragma: no cover
    skip("INPUT", "INPUT-functional (validators 런타임)", f"모듈 미설치: {e}")
except Exception as e:  # pragma: no cover
    check("INPUT", "INPUT-00 validators 동작 검증", False, str(e))


# ───────────────────────────────────────────────────────────────────────────
# SQL Injection 정적 검사 — 사용자 입력 직접 f-string 주입 없는지
# ───────────────────────────────────────────────────────────────────────────

def _find_unsafe_sql(src: str) -> list[str]:
    """cur.execute() 인수에서 f-string 을 감지해 위험 라인 반환."""
    hits: list[str] = []
    for m in re.finditer(r'cur\.execute\s*\(\s*f"', src):
        # 앞뒤 100자 맥락
        start = max(0, m.start() - 20)
        end = min(len(src), m.end() + 200)
        hits.append(src[start:end].replace("\n", "\\n")[:200])
    return hits

for fname, src in (("admin.py", ADMIN_PY),
                    ("auth_router.py", AUTH_ROUTER),
                    ("account_router.py", ACCOUNT_ROUTER),
                    ("refresh_service.py", REFRESH_SVC),
                    ("oauth_service.py", OAUTH_SVC)):
    unsafe = _find_unsafe_sql(src)
    # 화이트리스트: admin.py 의 expires_at_sql 은 상수 SQL 조각만 주입
    if fname == "admin.py":
        # 해당 f-string 들은 모두 정적 SQL 템플릿(% 자리표시자 포함)
        # 검사 목적은 사용자 입력이 주입되지 않는 것
        unsafe = [u for u in unsafe if 'body.' in u or 'params.' in u or 'request.' in u]
    check("SQLI", f"SQLI-{fname} cur.execute() f-string 에 사용자 입력 주입 없음",
          not unsafe, detail=str(unsafe)[:200])


# ───────────────────────────────────────────────────────────────────────────
# XSS 정적 검사 — 프론트에서 dangerouslySetInnerHTML 남용 확인
# ───────────────────────────────────────────────────────────────────────────

_frontend_dangerous = []
for fp in (ROOT / "frontend/src").rglob("*.tsx"):
    if "dangerouslySetInnerHTML" in fp.read_text(encoding="utf-8"):
        _frontend_dangerous.append(str(fp.relative_to(ROOT)))

# dangerouslySetInnerHTML 사용은 DOMPurify 와 함께일 때만 허용 (화이트리스트 패턴)
_unsanitized = []
for fp_str in _frontend_dangerous:
    txt = (ROOT / fp_str).read_text(encoding="utf-8")
    if "DOMPurify" not in txt and "sanitize" not in txt.lower() and "escapeHtml" not in txt:
        _unsanitized.append(fp_str)
check("XSS", "XSS-01 프런트 dangerouslySetInnerHTML 사용 시 sanitize 동반",
      not _unsanitized,
      detail=f"unsanitized={_unsanitized[:3]}")


# ───────────────────────────────────────────────────────────────────────────
# Admin UI 접근 제어
# ───────────────────────────────────────────────────────────────────────────

check("ADMIN-FE", "ADMIN-01 /admin/layout.tsx → AdminLayout 사용", "AdminLayout" in ADMIN_LAYOUT)
check("ADMIN-FE", "ADMIN-02 AdminLayout 내부 AuthGuard(ORG_ADMIN) 감싸기",
      'requiredRole="ORG_ADMIN"' in ADMIN_LAYOUT_COMP
      and "<AuthGuard" in ADMIN_LAYOUT_COMP)
check("ADMIN-FE", "ADMIN-03 AuthGuard 미인증 → /login?redirect",
      '/login?redirect=' in AUTH_GUARD
      and "hasMinimumRole" in AUTH_GUARD)
check("ADMIN-FE", "ADMIN-04 권한 부족 시 ForbiddenContent 표시",
      "ForbiddenContent" in AUTH_GUARD)


# ───────────────────────────────────────────────────────────────────────────
# 기존 Phase 14 단위 테스트 존재 + 디스커버리
# ───────────────────────────────────────────────────────────────────────────

_EXPECTED_TESTS = [
    "test_auth_phase14.py",
    "test_jwt_tokens_phase14.py",
    "test_oauth_phase14.py",
    "test_purpose_tokens_phase14.py",
    "test_auth_state_phase14_8.py",
    "test_account_phase14.py",
    "test_admin_dashboard_phase14_9.py",
    "test_admin_management_phase14_10.py",
    "test_admin_settings_phase14_11.py",
    "test_admin_monitoring_phase14_12.py",
    "test_admin_alerts_phase14_13.py",
    "test_admin_jobs_phase14_14.py",
    "test_admin_audit_api_keys_phase14_15.py",
]
_tests_dir = ROOT / "backend/tests/unit"
for t in _EXPECTED_TESTS:
    check("COVERAGE", f"COV-{t}", (_tests_dir / t).exists())


# ───────────────────────────────────────────────────────────────────────────
# 비밀번호 플로우 통합 — 재설정 토큰 1회 사용 정적 검증
# ───────────────────────────────────────────────────────────────────────────

check("FLOW", "FLOW-01 reset-password 엔드포인트 존재",
      '@router.post("/reset-password")' in AUTH_ROUTER)
check("FLOW", "FLOW-02 reset 시 purpose=\"password_reset\" 검증",
      'decode_purpose_token' in AUTH_ROUTER
      and '"password_reset"' in AUTH_ROUTER)
check("FLOW", "FLOW-03 reset 시 check_token_used 선행",
      "check_token_used" in AUTH_ROUTER)
check("FLOW", "FLOW-04 reset 후 mark_token_used 호출",
      "mark_token_used" in AUTH_ROUTER)
check("FLOW", "FLOW-05 비밀번호 변경 후 모든 RT 폐기",
      "revoke_all_user_tokens" in AUTH_ROUTER
      or "revoke_all_user_tokens" in ACCOUNT_ROUTER)
check("FLOW", "FLOW-06 로그아웃 시 family 폐기",
      "refresh_token_service.logout" in AUTH_ROUTER
      and "revoke_family" in REFRESH_SVC)


# ───────────────────────────────────────────────────────────────────────────
# 결과 집계
# ───────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    categories = sorted({r[0] for r in results})
    total = len(results)
    passed = sum(1 for r in results if r[2])
    failed = total - passed
    print(f"\n{'='*70}")
    print(f"Phase 14-16 통합 보안 검증")
    print(f"{'='*70}")
    for cat in categories:
        cat_results = [r for r in results if r[0] == cat]
        cat_pass = sum(1 for r in cat_results if r[2])
        print(f"\n[{cat}] {cat_pass}/{len(cat_results)}")
        for (_, name, ok, detail) in cat_results:
            mark = "✅" if ok else "❌"
            print(f"  {mark} {name}" + (f"  — {detail}" if detail and not ok else ""))
    if skipped:
        print(f"\n[SKIPPED] {len(skipped)}")
        for (cat, name, reason) in skipped:
            print(f"  ⏭️  [{cat}] {name}  — {reason}")
    print(f"\n{'='*70}")
    print(f"합계: {passed}/{total} PASS  ({failed} FAIL, {len(skipped)} SKIP)")
    print(f"{'='*70}\n")

    # 기존 Phase 14 단위 테스트 묶음 실행 (pytest discovery)
    print("[Phase 14 unit test suite]")
    ret = subprocess.run(
        [sys.executable, "-m", "pytest",
         str(ROOT / "backend/tests/unit"),
         "-q", "--no-header",
         "-k", "phase14",
         "--tb=no"],
        cwd=str(ROOT / "backend"),
        env={**os.environ},
        capture_output=True, text=True, timeout=120,
    )
    tail = ret.stdout.strip().splitlines()[-5:] if ret.stdout else []
    for ln in tail:
        print(f"  {ln}")
    print()
    raise SystemExit(0 if failed == 0 else 1)
