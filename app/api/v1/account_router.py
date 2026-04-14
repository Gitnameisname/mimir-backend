"""
Account router — /api/v1/account

Phase 14-7: 계정 관리 API.

엔드포인트:
  - GET    /account/profile                       — 프로필 조회
  - PATCH  /account/profile                       — 프로필 수정
  - POST   /account/change-password               — 비밀번호 변경
  - GET    /account/oauth-accounts                — 연결된 소셜 계정 목록
  - POST   /account/oauth-accounts/gitlab/link    — GitLab 계정 연결 시작
  - DELETE /account/oauth-accounts/gitlab/unlink  — GitLab 계정 해제
  - GET    /account/sessions                      — 활성 세션 목록
  - DELETE /account/sessions/{session_id}          — 세션 강제 종료

보안 원칙:
  - 모든 엔드포인트는 인증 필수 (Bearer Token)
  - 본인 데이터만 접근 가능 (타인 접근 불가)
  - 비밀번호 변경 시 현재 비밀번호 확인 필수
  - GitLab 해제 시 다른 로그인 수단 존재 확인
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.api.auth import resolve_current_actor, ActorContext
from app.api.auth.oauth_service import gitlab_oauth_service
from app.api.auth.password import hash_password, verify_password
from app.api.auth.refresh_service import refresh_token_service
from app.api.auth.validators import validate_display_name, validate_password_strength
from app.api.context import get_request_ids
from app.api.responses.helpers import success_response
from app.audit.emitter import audit_emitter
from app.cache.valkey import get_valkey
from app.config import settings
from app.db import get_db
from app.repositories.users_repository import users_repository

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Auth 헬퍼: 인증 필수 확인
# ---------------------------------------------------------------------------

def _require_auth(actor: ActorContext) -> str:
    """인증된 사용자의 ID를 반환한다. 미인증 시 401."""
    if not actor.is_authenticated or not actor.resolved_id:
        raise HTTPException(status_code=401, detail="인증이 필요합니다")
    return actor.resolved_id


# ---------------------------------------------------------------------------
# Request / Response 스키마
# ---------------------------------------------------------------------------

class ProfileResponse(BaseModel):
    """프로필 조회 응답."""
    id: str
    email: str
    display_name: str
    avatar_url: Optional[str] = None
    auth_provider: str
    email_verified: bool
    role_name: str
    created_at: str
    has_password: bool


class UpdateProfileRequest(BaseModel):
    """프로필 수정 요청."""
    display_name: Optional[str] = Field(None, min_length=1, max_length=100)
    avatar_url: Optional[str] = Field(None, max_length=500)


class ChangePasswordRequest(BaseModel):
    """비밀번호 변경 요청."""
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=1, max_length=128)


class OAuthAccountResponse(BaseModel):
    """OAuth 계정 응답."""
    provider: str
    provider_email: Optional[str] = None
    provider_name: Optional[str] = None
    created_at: str


class SessionResponse(BaseModel):
    """세션 응답."""
    id: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: str
    is_current: bool


# ---------------------------------------------------------------------------
# GET /account/profile — 프로필 조회
# ---------------------------------------------------------------------------

@router.get("/profile")
def get_profile(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """인증된 사용자의 프로필을 조회한다."""
    req_id, trace_id = get_request_ids(request)
    user_id = _require_auth(actor)

    with get_db() as conn:
        user = users_repository.get_by_id(conn, user_id)

    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")

    return success_response(
        data=ProfileResponse(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            auth_provider=user.auth_provider,
            email_verified=user.email_verified,
            role_name=user.role_name,
            created_at=user.created_at.isoformat() if user.created_at else "",
            has_password=bool(user.password_hash),
        ),
        request_id=req_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# PATCH /account/profile — 프로필 수정
# ---------------------------------------------------------------------------

@router.patch("/profile")
def update_profile(
    body: UpdateProfileRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """프로필 정보를 수정한다 (display_name, avatar_url)."""
    req_id, trace_id = get_request_ids(request)
    user_id = _require_auth(actor)

    # display_name 유효성 검증
    if body.display_name is not None:
        name_errors = validate_display_name(body.display_name)
        if name_errors:
            raise HTTPException(status_code=422, detail={"errors": name_errors})

    # avatar_url 기본 검증 (길이는 Field에서 처리, 프로토콜만 확인)
    if body.avatar_url is not None and body.avatar_url.strip():
        url = body.avatar_url.strip()
        if not url.startswith(("https://", "http://")):
            raise HTTPException(status_code=422, detail="아바타 URL은 http:// 또는 https://로 시작해야 합니다")

    update_kwargs = {}
    if body.display_name is not None:
        update_kwargs["display_name"] = body.display_name.strip()
    if body.avatar_url is not None:
        update_kwargs["avatar_url"] = body.avatar_url.strip() if body.avatar_url.strip() else None

    if not update_kwargs:
        raise HTTPException(status_code=422, detail="수정할 항목이 없습니다")

    with get_db() as conn:
        updated_user = users_repository.update(conn, user_id, **update_kwargs)

    if not updated_user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")

    audit_emitter.emit(
        event_type="user.profile_updated",
        action="account.update_profile",
        actor_id=user_id,
        resource_type="user",
        resource_id=user_id,
        result="success",
        request_id=req_id,
        metadata={"fields": list(update_kwargs.keys())},
    )
    logger.info("profile_updated user_id=%s fields=%s", user_id, list(update_kwargs.keys()))

    return success_response(
        data=ProfileResponse(
            id=updated_user.id,
            email=updated_user.email,
            display_name=updated_user.display_name,
            avatar_url=updated_user.avatar_url,
            auth_provider=updated_user.auth_provider,
            email_verified=updated_user.email_verified,
            role_name=updated_user.role_name,
            created_at=updated_user.created_at.isoformat() if updated_user.created_at else "",
            has_password=bool(updated_user.password_hash),
        ),
        request_id=req_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /account/change-password — 비밀번호 변경
# ---------------------------------------------------------------------------

@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """비밀번호를 변경한다.

    처리 흐름:
      1. 현재 비밀번호 검증 (bcrypt)
      2. 새 비밀번호 복잡도 검증
      3. 새 비밀번호 해싱 → 저장
      4. 현재 세션 제외, 다른 모든 RT family 폐기
      5. 감사 이벤트 기록
    """
    req_id, trace_id = get_request_ids(request)
    user_id = _require_auth(actor)

    with get_db() as conn:
        user = users_repository.get_by_id(conn, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")

        # GitLab 전용 계정(password_hash=NULL)인 경우 비밀번호 변경 비활성화
        if not user.password_hash:
            raise HTTPException(
                status_code=400,
                detail="소셜 로그인 계정은 비밀번호를 변경할 수 없습니다. 먼저 비밀번호를 설정해 주세요.",
            )

        # 1. 현재 비밀번호 검증
        if not verify_password(body.current_password, user.password_hash):
            audit_emitter.emit(
                event_type="user.password_change_failed",
                action="account.change_password",
                actor_id=user_id,
                resource_type="user",
                resource_id=user_id,
                result="failure",
                request_id=req_id,
                metadata={"reason": "invalid_current_password"},
            )
            raise HTTPException(status_code=401, detail="현재 비밀번호가 올바르지 않습니다")

        # 2. 새 비밀번호 복잡도 검증
        pw_errors = validate_password_strength(body.new_password)
        if pw_errors:
            raise HTTPException(status_code=422, detail={"errors": pw_errors})

        # 3. 새 비밀번호 해싱 → 저장
        new_hash = hash_password(body.new_password)
        users_repository.update(conn, user_id, password_hash=new_hash)

        # 4. 다른 모든 RT family 폐기 (현재 세션 포함 — 프론트엔드에서 재로그인 처리)
        revoked_count = refresh_token_service.revoke_all_user_tokens(conn, user_id)

    audit_emitter.emit(
        event_type="user.password_changed",
        action="account.change_password",
        actor_id=user_id,
        resource_type="user",
        resource_id=user_id,
        result="success",
        request_id=req_id,
        metadata={"revoked_tokens": revoked_count},
    )
    logger.info(
        "password_changed user_id=%s revoked_tokens=%d",
        user_id, revoked_count,
    )

    return success_response(
        data={"message": "비밀번호가 변경되었습니다. 다시 로그인해 주세요."},
        request_id=req_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /account/oauth-accounts — 연결된 소셜 계정 목록
# ---------------------------------------------------------------------------

@router.get("/oauth-accounts")
def list_oauth_accounts(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """연결된 소셜 계정 목록을 조회한다."""
    req_id, trace_id = get_request_ids(request)
    user_id = _require_auth(actor)

    with get_db() as conn:
        accounts = gitlab_oauth_service.find_by_user_id(conn, user_id)

    result = [
        OAuthAccountResponse(
            provider=acc["provider"],
            provider_email=acc.get("provider_email"),
            provider_name=acc.get("provider_name"),
            created_at=acc["created_at"].isoformat() if acc.get("created_at") else "",
        )
        for acc in accounts
    ]

    return success_response(
        data=result,
        request_id=req_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /account/oauth-accounts/gitlab/link — GitLab 계정 연결 시작
# ---------------------------------------------------------------------------

@router.post("/oauth-accounts/gitlab/link")
def link_gitlab(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """GitLab 계정 연결을 시작한다.

    기존 사용자의 계정에 GitLab OAuth를 연결하기 위한 인증 URL을 반환한다.
    콜백에서 link_to_user_id를 state에 포함하여 기존 계정에 연결한다.
    """
    req_id, trace_id = get_request_ids(request)
    user_id = _require_auth(actor)

    if not settings.is_oauth_enabled:
        raise HTTPException(status_code=501, detail="GitLab OAuth가 설정되지 않았습니다")

    # 이미 GitLab 계정이 연결되어 있는지 확인
    with get_db() as conn:
        existing = gitlab_oauth_service.find_by_user_id(conn, user_id, provider="gitlab")
        if existing:
            raise HTTPException(status_code=409, detail="이미 GitLab 계정이 연결되어 있습니다")

    valkey = get_valkey()
    result = gitlab_oauth_service.create_authorization_url(valkey, link_to_user_id=user_id)

    if result is None:
        raise HTTPException(
            status_code=502,
            detail="GitLab OIDC Discovery에 실패했습니다",
        )

    audit_emitter.emit(
        event_type="user.oauth_link_started",
        action="account.oauth.link_start",
        actor_id=user_id,
        resource_type="user",
        resource_id=user_id,
        result="success",
        request_id=req_id,
        metadata={"provider": "gitlab"},
    )

    return success_response(
        data={"url": result["url"]},
        request_id=req_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# DELETE /account/oauth-accounts/gitlab/unlink — GitLab 계정 해제
# ---------------------------------------------------------------------------

@router.delete("/oauth-accounts/gitlab/unlink")
def unlink_gitlab(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """GitLab 계정 연결을 해제한다.

    로컬 비밀번호가 설정되어 있는지 확인하고, 없으면 해제를 거부한다.
    (유일한 로그인 수단 해제 방지)
    """
    req_id, trace_id = get_request_ids(request)
    user_id = _require_auth(actor)

    with get_db() as conn:
        user = users_repository.get_by_id(conn, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")

        # 비밀번호 미설정 상태에서 유일한 로그인 수단 해제 시도 방지
        if not user.password_hash:
            raise HTTPException(
                status_code=400,
                detail="비밀번호가 설정되지 않은 상태에서는 GitLab 계정을 해제할 수 없습니다. 먼저 비밀번호를 설정해 주세요.",
            )

        # GitLab OAuth 계정 삭제
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM oauth_accounts WHERE user_id = %s AND provider = 'gitlab'",
                (user_id,),
            )
            deleted_count = cur.rowcount

    if deleted_count == 0:
        raise HTTPException(status_code=404, detail="연결된 GitLab 계정이 없습니다")

    audit_emitter.emit(
        event_type="user.oauth_unlinked",
        action="account.oauth.unlink",
        actor_id=user_id,
        resource_type="user",
        resource_id=user_id,
        result="success",
        request_id=req_id,
        metadata={"provider": "gitlab"},
    )
    logger.info("oauth_unlinked user_id=%s provider=gitlab", user_id)

    return success_response(
        data={"message": "GitLab 계정 연결이 해제되었습니다"},
        request_id=req_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /account/sessions — 활성 세션 목록
# ---------------------------------------------------------------------------

@router.get("/sessions")
def list_sessions(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """활성 세션 목록을 조회한다.

    refresh_tokens 테이블에서 현재 사용자의 활성(revoked=FALSE) 토큰 목록을 반환한다.
    현재 세션에는 is_current=True 배지를 표시한다.
    """
    req_id, trace_id = get_request_ids(request)
    user_id = _require_auth(actor)

    # 현재 RT의 family_id를 알기 위해 Cookie에서 RT 추출
    current_rt_raw = request.cookies.get("refresh_token")
    current_family_id = None

    if current_rt_raw:
        import hashlib
        current_rt_hash = hashlib.sha256(current_rt_raw.encode("utf-8")).hexdigest()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT family_id FROM refresh_tokens WHERE token_hash = %s",
                    (current_rt_hash,),
                )
                row = cur.fetchone()
                if row:
                    current_family_id = str(row["family_id"])

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (family_id)
                    id, family_id, ip_address, user_agent, created_at
                FROM refresh_tokens
                WHERE user_id = %s AND revoked = FALSE AND expires_at > NOW()
                ORDER BY family_id, created_at DESC
                """,
                (user_id,),
            )
            rows = cur.fetchall()

    sessions = [
        SessionResponse(
            id=str(row["family_id"]),
            ip_address=row.get("ip_address"),
            user_agent=row.get("user_agent"),
            created_at=row["created_at"].isoformat() if row.get("created_at") else "",
            is_current=(str(row["family_id"]) == current_family_id) if current_family_id else False,
        )
        for row in rows
    ]

    return success_response(
        data=sessions,
        request_id=req_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# DELETE /account/sessions/{session_id} — 세션 강제 종료
# ---------------------------------------------------------------------------

@router.delete("/sessions/{session_id}")
def revoke_session(
    session_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """특정 세션(family)을 강제 종료한다.

    현재 세션은 삭제 불가 (별도 로그아웃 사용).
    """
    req_id, trace_id = get_request_ids(request)
    user_id = _require_auth(actor)

    # 현재 세션인지 확인
    current_rt_raw = request.cookies.get("refresh_token")
    if current_rt_raw:
        import hashlib
        current_rt_hash = hashlib.sha256(current_rt_raw.encode("utf-8")).hexdigest()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT family_id FROM refresh_tokens WHERE token_hash = %s",
                    (current_rt_hash,),
                )
                row = cur.fetchone()
                if row and str(row["family_id"]) == session_id:
                    raise HTTPException(
                        status_code=400,
                        detail="현재 세션은 삭제할 수 없습니다. 로그아웃을 사용해 주세요.",
                    )

    # 해당 family가 본인 소유인지 확인 후 폐기
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM refresh_tokens
                WHERE family_id = %s AND user_id = %s AND revoked = FALSE
                """,
                (session_id, user_id),
            )
            row = cur.fetchone()
            if not row or row["cnt"] == 0:
                raise HTTPException(status_code=404, detail="해당 세션을 찾을 수 없습니다")

        revoked_count = refresh_token_service.revoke_family(conn, session_id)

    audit_emitter.emit(
        event_type="user.session_revoked",
        action="account.session.revoke",
        actor_id=user_id,
        resource_type="session",
        resource_id=session_id,
        result="success",
        request_id=req_id,
        metadata={"revoked_tokens": revoked_count},
    )
    logger.info(
        "session_revoked user_id=%s family_id=%s revoked=%d",
        user_id, session_id, revoked_count,
    )

    return success_response(
        data={"message": "세션이 종료되었습니다"},
        request_id=req_id,
        trace_id=trace_id,
    )
