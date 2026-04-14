"""
Auth router — /api/v1/auth

Phase 14: 인증 엔드포인트.

엔드포인트:
  - POST /auth/register  — 회원가입
  - POST /auth/login     — 로그인 (AT + RT 발급)
  - POST /auth/refresh   — 토큰 갱신 (RT Rotation)
  - POST /auth/logout    — 로그아웃 (Family 폐기)
  - GET  /auth/oauth/gitlab          — GitLab OAuth 인증 시작
  - GET  /auth/oauth/gitlab/callback — GitLab OAuth 콜백 처리
  - POST /auth/forgot-password       — 비밀번호 재설정 이메일 발송
  - POST /auth/reset-password        — 비밀번호 재설정 실행
  - POST /auth/verify-email          — 이메일 인증 확인

보안 원칙:
  - 로그인 실패 시 구체적 원인 미노출 (열거 공격 방지)
  - 비밀번호 평문은 절대 로깅하지 않음
  - 타이밍 공격 방어 (사용자 미존재 시에도 bcrypt 더미 해싱)
  - RT는 HttpOnly Secure Cookie로만 전송
  - OAuth state + PKCE로 CSRF / Code Interception 방어
"""

import logging
from datetime import datetime, timezone
from urllib.parse import urlencode

import psycopg2
from fastapi import APIRouter, Cookie, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field, model_validator

from app.api.auth.oauth_service import gitlab_oauth_service
from app.api.auth.password import dummy_verify, hash_password, verify_password
from app.api.auth.purpose_tokens import (
    check_token_used,
    create_purpose_token,
    decode_purpose_token,
    mark_token_used,
)
from app.api.auth.rate_limit import check_login_allowed, clear_attempts, record_failed_attempt
from app.api.auth.refresh_service import refresh_token_service
from app.api.auth.validators import (
    validate_display_name,
    validate_password_strength,
    validate_username,
)
from app.api.context import get_request_ids
from app.api.responses.helpers import success_response
from app.audit.emitter import audit_emitter
from app.cache.valkey import get_valkey
from app.config import settings
from app.db import get_db
from app.repositories.users_repository import users_repository
from app.services.email_service import email_service

logger = logging.getLogger(__name__)

router = APIRouter()

# RT Cookie 설정 상수
_RT_COOKIE_NAME = "refresh_token"
_RT_COOKIE_PATH = "/api/v1/auth"
_RT_COOKIE_MAX_AGE = settings.jwt_refresh_expire_days * 86400  # 초 단위
_RT_COOKIE_SECURE = settings.environment == "production"
_RT_COOKIE_SAMESITE = "strict"


def _set_refresh_cookie(response: Response, raw_token: str) -> None:
    """Response에 RT HttpOnly Cookie를 설정한다."""
    response.set_cookie(
        key=_RT_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=_RT_COOKIE_SECURE,
        samesite=_RT_COOKIE_SAMESITE,
        path=_RT_COOKIE_PATH,
        max_age=_RT_COOKIE_MAX_AGE,
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Response에서 RT Cookie를 삭제한다."""
    response.delete_cookie(
        key=_RT_COOKIE_NAME,
        path=_RT_COOKIE_PATH,
        httponly=True,
        secure=_RT_COOKIE_SECURE,
        samesite=_RT_COOKIE_SAMESITE,
    )


def _get_client_ip(request: Request) -> str | None:
    """클라이언트 IP를 추출한다 (X-Forwarded-For 우선)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


# ---------------------------------------------------------------------------
# Request / Response 스키마
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    """회원가입 요청."""
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=100)
    # Phase 14-17: 아이디(username) 선택 입력
    username: str | None = Field(default=None, min_length=0, max_length=30)


class RegisterResponse(BaseModel):
    """회원가입 응답."""
    id: str
    email: str
    display_name: str
    status: str
    username: str | None = None


class LoginRequest(BaseModel):
    """로그인 요청. 이메일 또는 아이디(username)로 로그인할 수 있다.

    - identifier: 이메일 또는 아이디. 구 클라이언트 호환을 위해 `email` 필드로
      전송해도 동일하게 동작한다.
    """
    identifier: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def _accept_email_alias(cls, data):
        if isinstance(data, dict) and "identifier" not in data and "email" in data:
            data = {**data, "identifier": data["email"]}
        return data


class LoginTokenResponse(BaseModel):
    """로그인 성공 응답 (AT 포함)."""
    access_token: str
    token_type: str = "Bearer"
    expires_in: int


class RefreshTokenResponse(BaseModel):
    """토큰 갱신 응답."""
    access_token: str
    token_type: str = "Bearer"
    expires_in: int


class ForgotPasswordRequest(BaseModel):
    """비밀번호 재설정 요청."""
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """비밀번호 재설정 실행."""
    token: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=1, max_length=128)


class VerifyEmailRequest(BaseModel):
    """이메일 인증."""
    token: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# POST /auth/register
# ---------------------------------------------------------------------------

@router.post("/register", status_code=201)
def register(body: RegisterRequest, request: Request):
    """이메일/비밀번호 회원가입."""
    req_id, trace_id = get_request_ids(request)

    # 1. 비밀번호 복잡도 검증
    pw_errors = validate_password_strength(body.password)
    if pw_errors:
        raise HTTPException(status_code=422, detail={"errors": pw_errors})

    # 2. 표시 이름 검증
    name_errors = validate_display_name(body.display_name)
    if name_errors:
        raise HTTPException(status_code=422, detail={"errors": name_errors})

    email_lower = body.email.lower().strip()

    # 3. 아이디(username) 검증 (입력된 경우에만)
    username_value: str | None = None
    if body.username and body.username.strip():
        un_errors = validate_username(body.username)
        if un_errors:
            raise HTTPException(status_code=422, detail={"errors": un_errors})
        username_value = body.username.strip()

    with get_db() as conn:
        # 4. 이메일/아이디 중복 검사
        if users_repository.get_by_email(conn, email_lower):
            raise HTTPException(status_code=409, detail="이미 사용 중인 이메일입니다")
        if username_value and users_repository.get_by_username(conn, username_value):
            raise HTTPException(status_code=409, detail="이미 사용 중인 아이디입니다")

        # 5. bcrypt 해싱 + 사용자 생성
        #    TOCTOU: 사전 존재 검사와 INSERT 사이의 레이스로 인해 UNIQUE 제약 위반이
        #    발생할 수 있으므로 409 로 변환한다. 위반된 컬럼을 pgcode/메시지로
        #    식별하기 어려우므로 이메일/아이디 둘 다 재확인하여 분기한다.
        hashed = hash_password(body.password)
        try:
            user = users_repository.create(
                conn,
                email=email_lower,
                display_name=body.display_name.strip(),
                role_name="VIEWER",
                status="ACTIVE",
                password_hash=hashed,
                auth_provider="local",
                email_verified=False,
                username=username_value,
            )
        except psycopg2.errors.UniqueViolation as e:
            logger.warning(
                "register_race email=%s username=%s: %s",
                email_lower, username_value, e,
            )
            # 롤백 후 어느 쪽이 충돌했는지 재조회
            conn.rollback()
            if users_repository.get_by_email(conn, email_lower):
                raise HTTPException(status_code=409, detail="이미 사용 중인 이메일입니다")
            raise HTTPException(status_code=409, detail="이미 사용 중인 아이디입니다")

    audit_emitter.emit(
        event_type="user.registered",
        action="auth.register",
        actor_id=user.id,
        resource_type="user",
        resource_id=user.id,
        result="success",
        request_id=req_id,
    )
    logger.info("user_registered email=%s user_id=%s", email_lower, user.id)

    # Phase 14-5: 이메일 인증 메일 발송 (실패해도 가입 흐름 블로킹 안 함)
    try:
        verify_token = create_purpose_token(
            user_id=user.id,
            purpose="email_verify",
            expire_minutes=1440,  # 24시간
        )
        verify_url = f"{settings.frontend_url}/verify-email?token={verify_token}"
        email_service.send_email_verification(email_lower, verify_url)
    except Exception:
        logger.exception("verify_email_send_failed user_id=%s", user.id)

    return success_response(
        data=RegisterResponse(
            id=user.id, email=user.email,
            display_name=user.display_name, status=user.status,
            username=user.username,
        ),
        request_id=req_id, trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------

@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response):
    """이메일 또는 아이디(username) + 비밀번호 로그인 → AT + RT 발급."""
    req_id, trace_id = get_request_ids(request)
    identifier_raw = body.identifier.strip()
    # Rate-limit 키는 소문자로 통일 (이메일/아이디 대소문자 우회 방지)
    identifier_key = identifier_raw.lower()
    valkey = get_valkey()

    # 1. Valkey 기반 시도 제한 확인
    if not check_login_allowed(valkey, identifier_key):
        raise HTTPException(
            status_code=429,
            detail="로그인 시도 횟수를 초과했습니다. 잠시 후 다시 시도해 주세요.",
        )

    with get_db() as conn:
        # 2. 이메일 또는 아이디로 사용자 조회
        user = users_repository.get_by_identifier(conn, identifier_raw)

        if user is None:
            dummy_verify()
            record_failed_attempt(valkey, identifier_key)
            audit_emitter.emit(
                event_type="user.login_failed", action="auth.login",
                actor_id=None, resource_type="user", result="failure",
                request_id=req_id, metadata={"reason": "user_not_found"},
            )
            raise HTTPException(status_code=401, detail="이메일/아이디 또는 비밀번호가 올바르지 않습니다")

        # 3. 계정 상태 확인
        if user.status != "ACTIVE":
            dummy_verify()
            audit_emitter.emit(
                event_type="user.login_failed", action="auth.login",
                actor_id=user.id, resource_type="user", resource_id=user.id,
                result="denied", request_id=req_id,
                metadata={"reason": "account_inactive", "status": user.status},
            )
            raise HTTPException(status_code=403, detail="비활성 또는 정지된 계정입니다")

        # 4. DB 수준 잠금 확인
        if user.locked_until and user.locked_until > datetime.now(timezone.utc):
            dummy_verify()
            raise HTTPException(
                status_code=429,
                detail="로그인 시도 횟수를 초과했습니다. 잠시 후 다시 시도해 주세요.",
            )

        # 5. 비밀번호 검증
        if not user.password_hash or not verify_password(body.password, user.password_hash):
            record_failed_attempt(valkey, identifier_key)
            users_repository.record_login_failure(conn, user.id)
            audit_emitter.emit(
                event_type="user.login_failed", action="auth.login",
                actor_id=user.id, resource_type="user", resource_id=user.id,
                result="failure", request_id=req_id,
                metadata={"reason": "invalid_password"},
            )
            raise HTTPException(status_code=401, detail="이메일/아이디 또는 비밀번호가 올바르지 않습니다")

        # 6. 성공: 카운터 초기화 + last_login_at 갱신
        clear_attempts(valkey, identifier_key)
        users_repository.record_login_success(conn, user.id)

        # 7. AT + RT 발급
        tokens = refresh_token_service.issue_tokens(
            conn,
            user_id=user.id,
            role=user.role_name,
            ip_address=_get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )

    # RT를 HttpOnly Cookie로 설정
    _set_refresh_cookie(response, tokens["refresh_token"])

    audit_emitter.emit(
        event_type="user.login_success", action="auth.login",
        actor_id=user.id, actor_role=user.role_name,
        resource_type="user", resource_id=user.id,
        result="success", request_id=req_id,
    )
    logger.info("login_success identifier=%s user_id=%s", identifier_key, user.id)

    return success_response(
        data=LoginTokenResponse(
            access_token=tokens["access_token"],
            token_type=tokens["token_type"],
            expires_in=tokens["expires_in"],
        ),
        request_id=req_id, trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /auth/refresh
# ---------------------------------------------------------------------------

@router.post("/refresh")
def refresh(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
):
    """Refresh Token으로 새 AT + RT를 발급한다 (Rotation).

    RT는 Cookie에서 자동 추출된다.
    이미 사용된 RT로 요청 시 family 전체가 폐기된다 (탈취 감지).
    """
    req_id, trace_id = get_request_ids(request)

    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh Token이 없습니다")

    with get_db() as conn:
        tokens = refresh_token_service.rotate(
            conn,
            raw_token=refresh_token,
            ip_address=_get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )

    if tokens is None:
        # Rotation 실패: 만료, 탈취, 사용자 비활성 등
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="유효하지 않은 Refresh Token입니다")

    # 새 RT를 Cookie로 설정
    _set_refresh_cookie(response, tokens["refresh_token"])

    logger.info("token_refreshed family=%s", tokens["family_id"])

    return success_response(
        data=RefreshTokenResponse(
            access_token=tokens["access_token"],
            token_type=tokens["token_type"],
            expires_in=tokens["expires_in"],
        ),
        request_id=req_id, trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------

@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
):
    """로그아웃: RT Family 전체를 폐기하고 Cookie를 삭제한다."""
    req_id, trace_id = get_request_ids(request)

    if refresh_token:
        with get_db() as conn:
            refresh_token_service.logout(conn, refresh_token)

    _clear_refresh_cookie(response)

    audit_emitter.emit(
        event_type="user.logout",
        action="auth.logout",
        actor_id=None,  # AT에서 추출 가능하지만 optional
        resource_type="user",
        result="success",
        request_id=req_id,
    )

    logger.info("logout_success")

    return success_response(
        data={"message": "로그아웃 완료"},
        request_id=req_id, trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /auth/oauth/gitlab — GitLab OAuth 인증 시작
# ---------------------------------------------------------------------------

@router.get("/oauth/gitlab")
def oauth_gitlab_start(request: Request):
    """GitLab OAuth 인증을 시작한다.

    PKCE code_verifier를 생성하고, state와 함께 Valkey에 저장한 뒤
    GitLab 인증 페이지로 302 리다이렉트한다.
    """
    if not settings.is_oauth_enabled:
        raise HTTPException(status_code=501, detail="GitLab OAuth가 설정되지 않았습니다")

    valkey = get_valkey()
    result = gitlab_oauth_service.create_authorization_url(valkey)

    if result is None:
        raise HTTPException(
            status_code=502,
            detail="GitLab OIDC Discovery에 실패했습니다",
        )

    return RedirectResponse(url=result["url"], status_code=302)


# ---------------------------------------------------------------------------
# GET /auth/oauth/gitlab/callback — GitLab OAuth 콜백 처리
# ---------------------------------------------------------------------------

@router.get("/oauth/gitlab/callback")
def oauth_gitlab_callback(
    request: Request,
    response: Response,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
):
    """GitLab OAuth 콜백을 처리한다.

    성공 시 AT를 URL fragment에, RT를 HttpOnly Cookie에 설정하고
    프론트엔드로 리다이렉트한다.
    """
    req_id, trace_id = get_request_ids(request)

    # GitLab이 에러를 반환한 경우 (사용자 거부 등)
    if error:
        logger.warning(
            "oauth_callback_error: error=%s desc=%s",
            error,
            error_description,
        )
        # 프론트엔드 에러 페이지로 리다이렉트
        error_params = urlencode({"error": error, "error_description": error_description or ""})
        return RedirectResponse(
            url=f"{settings.frontend_url}/auth/callback?{error_params}",
            status_code=302,
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="code 및 state 파라미터가 필요합니다")

    valkey = get_valkey()

    with get_db() as conn:
        tokens = gitlab_oauth_service.handle_callback(
            conn,
            valkey,
            code=code,
            state=state,
            ip_address=_get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )

    if tokens is None:
        # 프론트엔드 에러 페이지로 리다이렉트
        error_params = urlencode({"error": "oauth_failed", "error_description": "OAuth 인증에 실패했습니다"})
        return RedirectResponse(
            url=f"{settings.frontend_url}/auth/callback?{error_params}",
            status_code=302,
        )

    # AT를 URL fragment로, RT를 HttpOnly Cookie로 설정
    fragment_params = urlencode({
        "access_token": tokens["access_token"],
        "token_type": tokens["token_type"],
        "expires_in": tokens["expires_in"],
    })
    redirect_url = f"{settings.frontend_url}/auth/callback#{fragment_params}"

    redirect_response = RedirectResponse(url=redirect_url, status_code=302)
    _set_refresh_cookie(redirect_response, tokens["refresh_token"])

    audit_emitter.emit(
        event_type="user.oauth_callback_success",
        action="auth.oauth.callback",
        resource_type="user",
        result="success",
        request_id=req_id,
        metadata={"provider": "gitlab"},
    )

    return redirect_response


# ---------------------------------------------------------------------------
# POST /auth/forgot-password — 비밀번호 재설정 이메일 발송
# ---------------------------------------------------------------------------

@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordRequest, request: Request):
    """비밀번호 재설정 이메일을 발송한다.

    사용자 존재 여부와 무관하게 동일한 200 응답을 반환한다 (이메일 열거 방지).
    """
    req_id, trace_id = get_request_ids(request)
    email_lower = body.email.lower().strip()

    with get_db() as conn:
        user = users_repository.get_by_email(conn, email_lower)

    if user and user.status == "ACTIVE" and user.password_hash:
        # 사용자 존재 + 로컬 인증 사용자인 경우에만 이메일 발송
        token = create_purpose_token(
            user_id=user.id,
            purpose="password_reset",
            expire_minutes=30,
        )
        reset_url = f"{settings.frontend_url}/reset-password?token={token}"
        email_service.send_password_reset_email(email_lower, reset_url)

        audit_emitter.emit(
            event_type="user.password_reset_requested",
            action="auth.forgot_password",
            actor_id=user.id,
            resource_type="user",
            resource_id=user.id,
            result="success",
            request_id=req_id,
        )
        logger.info("password_reset_requested email=%s user_id=%s", email_lower, user.id)
    else:
        # 사용자 미존재/비활성/OAuth 전용 → 로깅만 하고 동일 응답
        logger.info("password_reset_requested email=%s (no action)", email_lower)

    return success_response(
        data={"message": "재설정 링크가 이메일로 발송되었습니다"},
        request_id=req_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /auth/reset-password — 비밀번호 재설정 실행
# ---------------------------------------------------------------------------

@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest, request: Request):
    """비밀번호 재설정 토큰을 검증하고 새 비밀번호를 설정한다."""
    req_id, trace_id = get_request_ids(request)
    valkey = get_valkey()

    # 1. 토큰 디코딩 + 목적 검증
    payload = decode_purpose_token(body.token, expected_purpose="password_reset")
    if payload is None:
        raise HTTPException(status_code=400, detail="유효하지 않거나 만료된 토큰입니다")

    jti = payload.get("jti", "")
    user_id = payload.get("sub", "")

    # 2. 1회성 확인
    if check_token_used(valkey, jti):
        raise HTTPException(status_code=400, detail="이미 사용된 토큰입니다")

    # 3. 새 비밀번호 복잡도 검증
    pw_errors = validate_password_strength(body.new_password)
    if pw_errors:
        raise HTTPException(status_code=422, detail={"errors": pw_errors})

    with get_db() as conn:
        # 4. 사용자 확인
        user = users_repository.get_by_id(conn, user_id)
        if not user or user.status != "ACTIVE":
            raise HTTPException(status_code=400, detail="유효하지 않은 토큰입니다")

        # 5. bcrypt 해싱 + password_hash 업데이트
        new_hash = hash_password(body.new_password)
        users_repository.update(conn, user_id, password_hash=new_hash)

        # 6. 해당 사용자의 모든 RT family 폐기 (강제 재로그인)
        revoked_count = refresh_token_service.revoke_all_user_tokens(conn, user_id)

    # 7. 토큰 사용 완료 표시 (TTL 30분)
    mark_token_used(valkey, jti, ttl_seconds=1800)

    audit_emitter.emit(
        event_type="user.password_reset_completed",
        action="auth.reset_password",
        actor_id=user_id,
        resource_type="user",
        resource_id=user_id,
        result="success",
        request_id=req_id,
        metadata={"revoked_tokens": revoked_count},
    )
    logger.info(
        "password_reset_completed user_id=%s revoked_tokens=%d",
        user_id, revoked_count,
    )

    return success_response(
        data={"message": "비밀번호가 변경되었습니다"},
        request_id=req_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /auth/verify-email — 이메일 인증
# ---------------------------------------------------------------------------

@router.post("/verify-email")
def verify_email(body: VerifyEmailRequest, request: Request):
    """이메일 인증 토큰을 검증하고 이메일 인증을 완료한다."""
    req_id, trace_id = get_request_ids(request)
    valkey = get_valkey()

    # 1. 토큰 디코딩 + 목적 검증
    payload = decode_purpose_token(body.token, expected_purpose="email_verify")
    if payload is None:
        raise HTTPException(status_code=400, detail="유효하지 않거나 만료된 토큰입니다")

    jti = payload.get("jti", "")
    user_id = payload.get("sub", "")

    # 2. 1회성 확인
    if check_token_used(valkey, jti):
        raise HTTPException(status_code=400, detail="이미 사용된 토큰입니다")

    with get_db() as conn:
        # 3. 사용자 확인
        user = users_repository.get_by_id(conn, user_id)
        if not user:
            raise HTTPException(status_code=400, detail="유효하지 않은 토큰입니다")

        # 이미 인증된 경우
        if user.email_verified:
            mark_token_used(valkey, jti, ttl_seconds=86400)
            return success_response(
                data={"message": "이메일이 이미 인증되었습니다"},
                request_id=req_id,
                trace_id=trace_id,
            )

        # 4. email_verified 업데이트
        users_repository.update(
            conn,
            user_id,
            email_verified=True,
        )

    # 5. 토큰 사용 완료 표시 (TTL 24시간)
    mark_token_used(valkey, jti, ttl_seconds=86400)

    audit_emitter.emit(
        event_type="user.email_verified",
        action="auth.verify_email",
        actor_id=user_id,
        resource_type="user",
        resource_id=user_id,
        result="success",
        request_id=req_id,
    )
    logger.info("email_verified user_id=%s", user_id)

    return success_response(
        data={"message": "이메일 인증이 완료되었습니다"},
        request_id=req_id,
        trace_id=trace_id,
    )
