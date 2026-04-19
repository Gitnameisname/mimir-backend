"""
GitLab OAuth2/OIDC 서비스 (Phase 14-4).

책임:
  - PKCE (code_verifier / code_challenge) 생성
  - OAuth state 생성 및 Valkey 저장/검증
  - Authorization Code → GitLab Token 교환
  - GitLab UserInfo 조회
  - 계정 연결/생성 (oauth_accounts + users)
  - GitLab 토큰 암호화 저장

보안 원칙:
  - PKCE S256: Authorization Code Interception 방어
  - state 파라미터: CSRF 방어 (Valkey에 10분 TTL 저장)
  - GitLab 토큰은 AES-256-GCM으로 암호화하여 DB에 저장
  - Self-managed GitLab + GitLab.com 모두 지원
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import psycopg2.extensions

from app.api.auth.encryption import encrypt_token
from app.api.auth.refresh_service import refresh_token_service
from app import config
from app.repositories.users_repository import users_repository

logger = logging.getLogger(__name__)

# PKCE / State 상수
_CODE_VERIFIER_LENGTH = 64  # 64바이트 → 86자 base64url
_STATE_LENGTH = 32  # 32바이트 → 43자 base64url
_STATE_TTL_SECONDS = 600  # 10분

# GitLab OIDC 스코프
_GITLAB_SCOPES = "openid profile email read_user"


# ---------------------------------------------------------------------------
# PKCE 유틸리티
# ---------------------------------------------------------------------------

def _generate_code_verifier() -> str:
    """PKCE code_verifier를 생성한다 (RFC 7636)."""
    return secrets.token_urlsafe(_CODE_VERIFIER_LENGTH)


def _generate_code_challenge(verifier: str) -> str:
    """PKCE code_challenge를 S256 방식으로 생성한다 (RFC 7636)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _generate_state() -> str:
    """OAuth state 파라미터를 생성한다."""
    return secrets.token_urlsafe(_STATE_LENGTH)


# ---------------------------------------------------------------------------
# OIDC Discovery
# ---------------------------------------------------------------------------

def _get_discovery_url() -> str:
    """GitLab OIDC Discovery URL."""
    base = config.settings.gitlab_base_url.rstrip("/")
    return f"{base}/.well-known/openid-configuration"


def _fetch_oidc_config() -> dict[str, Any] | None:
    """GitLab OIDC Discovery 문서를 가져온다.

    캐시는 별도로 하지 않음 (Valkey 캐시 추가 가능).

    Returns:
        OIDC configuration dict 또는 None (실패 시).
    """
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(_get_discovery_url())
            resp.raise_for_status()
            return resp.json()
    except Exception:
        logger.exception("OIDC discovery failed for %s", config.settings.gitlab_base_url)
        return None


# ---------------------------------------------------------------------------
# GitLab OAuth 서비스
# ---------------------------------------------------------------------------

class GitLabOAuthService:
    """GitLab OAuth2 + OIDC 서비스."""

    # ─── 인증 시작: Authorization URL 생성 ───

    def create_authorization_url(
        self, valkey, *, link_to_user_id: str | None = None,
    ) -> dict[str, str] | None:
        """GitLab 인증 URL을 생성하고, state+PKCE를 Valkey에 저장한다.

        Args:
            valkey: Valkey (Redis) 클라이언트.
            link_to_user_id: 기존 사용자에 연결할 경우 해당 사용자 ID.

        Returns:
            {"url": str, "state": str} 또는 None (설정 미완료 시).
        """
        if not config.settings.is_oauth_enabled:
            logger.warning("GitLab OAuth is not configured")
            return None

        # OIDC Discovery에서 authorization_endpoint 획득
        oidc_config = _fetch_oidc_config()
        if oidc_config is None:
            return None
        authorization_endpoint = oidc_config.get("authorization_endpoint")
        if not authorization_endpoint:
            logger.error("OIDC config missing authorization_endpoint")
            return None

        # PKCE + state 생성
        state = _generate_state()
        code_verifier = _generate_code_verifier()
        code_challenge = _generate_code_challenge(code_verifier)

        # Valkey에 state → code_verifier 매핑 저장 (10분 TTL)
        state_payload: dict[str, str] = {"code_verifier": code_verifier}
        if link_to_user_id:
            state_payload["link_to_user_id"] = link_to_user_id
        state_data = json.dumps(state_payload)
        valkey.setex(
            f"oauth_state:{state}",
            _STATE_TTL_SECONDS,
            state_data,
        )

        # Authorization URL 구성
        params = {
            "client_id": config.settings.gitlab_client_id,
            "redirect_uri": config.settings.gitlab_redirect_uri,
            "response_type": "code",
            "scope": _GITLAB_SCOPES,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        url = f"{authorization_endpoint}?{urlencode(params)}"

        return {"url": url, "state": state}

    # ─── 콜백 처리: Code → Token 교환 + 계정 연결 ───

    def handle_callback(
        self,
        conn: psycopg2.extensions.connection,
        valkey,
        *,
        code: str,
        state: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any] | None:
        """OAuth 콜백을 처리한다.

        1. state 검증 + code_verifier 복원
        2. Authorization Code → Token 교환
        3. UserInfo 조회
        4. 계정 연결/생성
        5. AT + RT 발급

        Args:
            conn: DB 커넥션.
            valkey: Valkey 클라이언트.
            code: Authorization Code.
            state: OAuth state 파라미터.
            ip_address: 클라이언트 IP.
            user_agent: 클라이언트 User-Agent.

        Returns:
            토큰 dict 또는 None (실패 시).
        """
        # 1. State 검증 + code_verifier 복원
        state_key = f"oauth_state:{state}"
        state_data_raw = valkey.get(state_key)
        if state_data_raw is None:
            logger.warning("oauth_callback: invalid or expired state")
            return None

        # state는 1회용 → 즉시 삭제
        valkey.delete(state_key)

        try:
            state_data = json.loads(state_data_raw)
            code_verifier = state_data["code_verifier"]
            link_to_user_id = state_data.get("link_to_user_id")
        except (json.JSONDecodeError, KeyError):
            logger.error("oauth_callback: corrupted state data")
            return None

        # 2. OIDC Discovery에서 token_endpoint, userinfo_endpoint 획득
        oidc_config = _fetch_oidc_config()
        if oidc_config is None:
            return None
        token_endpoint = oidc_config.get("token_endpoint")
        userinfo_endpoint = oidc_config.get("userinfo_endpoint")
        if not token_endpoint or not userinfo_endpoint:
            logger.error("OIDC config missing endpoints")
            return None

        # 3. Authorization Code → Token 교환
        gitlab_tokens = self._exchange_code(
            token_endpoint, code, code_verifier
        )
        if gitlab_tokens is None:
            return None

        # 4. UserInfo 조회
        access_token_gitlab = gitlab_tokens.get("access_token")
        if not access_token_gitlab:
            logger.error("oauth_callback: no access_token from GitLab")
            return None

        user_info = self._fetch_userinfo(userinfo_endpoint, access_token_gitlab)
        if user_info is None:
            return None

        # 5. 이메일 확인 (GitLab이 이메일을 비공개로 설정한 경우 에러)
        email = user_info.get("email")
        if not email:
            logger.warning("oauth_callback: GitLab user has no email (private setting)")
            return None

        provider_uid = str(user_info.get("sub", ""))
        if not provider_uid:
            logger.error("oauth_callback: no sub claim in userinfo")
            return None

        # 6. 계정 연결/생성
        user = self._link_or_create_account(
            conn,
            provider="gitlab",
            provider_uid=provider_uid,
            email=email.lower().strip(),
            display_name=user_info.get("name", email.split("@")[0]),
            avatar_url=user_info.get("picture") or user_info.get("avatar_url"),
            gitlab_tokens=gitlab_tokens,
            raw_profile=user_info,
            link_to_user_id=link_to_user_id,
        )
        if user is None:
            return None

        # 7. last_login_at 갱신
        users_repository.record_login_success(conn, user.id)

        # 8. Mimir AT + RT 발급
        tokens = refresh_token_service.issue_tokens(
            conn,
            user_id=user.id,
            role=user.role_name,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        return tokens

    # ─── Private: Code → Token 교환 ───

    def _exchange_code(
        self,
        token_endpoint: str,
        code: str,
        code_verifier: str,
    ) -> dict[str, Any] | None:
        """Authorization Code를 GitLab Token으로 교환한다."""
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    token_endpoint,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": config.settings.gitlab_client_id,
                        "client_secret": config.settings.gitlab_client_secret,
                        "code": code,
                        "redirect_uri": config.settings.gitlab_redirect_uri,
                        "code_verifier": code_verifier,
                    },
                )
                if resp.status_code != 200:
                    logger.error(
                        "oauth_exchange_code: GitLab returned %d: %s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return None
                return resp.json()
        except Exception:
            logger.exception("oauth_exchange_code: request failed")
            return None

    # ─── Private: UserInfo 조회 ───

    def _fetch_userinfo(
        self, userinfo_endpoint: str, access_token: str
    ) -> dict[str, Any] | None:
        """GitLab UserInfo 엔드포인트를 호출한다."""
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if resp.status_code != 200:
                    logger.error(
                        "oauth_userinfo: GitLab returned %d",
                        resp.status_code,
                    )
                    return None
                return resp.json()
        except Exception:
            logger.exception("oauth_userinfo: request failed")
            return None

    # ─── Private: 계정 연결/생성 ───

    def _link_or_create_account(
        self,
        conn: psycopg2.extensions.connection,
        *,
        provider: str,
        provider_uid: str,
        email: str,
        display_name: str,
        avatar_url: str | None,
        gitlab_tokens: dict[str, Any],
        raw_profile: dict[str, Any],
        link_to_user_id: str | None = None,
    ):
        """OAuth 프로필로 계정을 연결하거나 새로 생성한다.

        1. oauth_accounts에서 (provider, provider_uid) 조회 → 기존 계정
        2. 없으면 users에서 email로 조회 → 기존 사용자에 연결
        3. 둘 다 없으면 → 새 사용자 + OAuth 계정 생성

        Returns:
            User 객체 또는 None (실패 시).
        """
        from app.audit.emitter import audit_emitter

        # GitLab 토큰 암호화
        encrypted_at = encrypt_token(gitlab_tokens.get("access_token", ""))
        encrypted_rt = None
        if gitlab_tokens.get("refresh_token"):
            encrypted_rt = encrypt_token(gitlab_tokens["refresh_token"])

        token_expires_at = None
        if gitlab_tokens.get("expires_in"):
            token_expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(gitlab_tokens["expires_in"])
            )

        # 0. 명시적 계정 연결 요청 (계정 관리 페이지에서 연결)
        if link_to_user_id:
            target_user = users_repository.get_by_id(conn, link_to_user_id)
            if not target_user or target_user.status != "ACTIVE":
                logger.warning("oauth_link: target user %s not found or inactive", link_to_user_id)
                return None

            # 이미 다른 사용자에게 연결된 provider_uid인지 확인
            existing_oauth = self._find_oauth_account(conn, provider, provider_uid)
            if existing_oauth and str(existing_oauth["user_id"]) != link_to_user_id:
                logger.warning(
                    "oauth_link: provider_uid %s already linked to different user %s",
                    provider_uid, existing_oauth["user_id"],
                )
                return None

            if not existing_oauth:
                self._create_oauth_account(
                    conn,
                    user_id=link_to_user_id,
                    provider=provider,
                    provider_uid=provider_uid,
                    provider_email=email,
                    provider_name=display_name,
                    avatar_url=avatar_url,
                    access_token=encrypted_at,
                    refresh_token=encrypted_rt,
                    token_expires_at=token_expires_at,
                    raw_profile=raw_profile,
                )
            else:
                self._update_oauth_account(
                    conn,
                    oauth_id=str(existing_oauth["id"]),
                    access_token=encrypted_at,
                    refresh_token=encrypted_rt,
                    token_expires_at=token_expires_at,
                    provider_email=email,
                    provider_name=display_name,
                    avatar_url=avatar_url,
                    raw_profile=raw_profile,
                )

            audit_emitter.emit(
                event_type="user.oauth_linked",
                action="auth.oauth.link",
                actor_id=link_to_user_id,
                actor_type="user",
                resource_type="user",
                resource_id=link_to_user_id,
                result="success",
                metadata={"provider": provider, "provider_uid": provider_uid},
            )
            logger.info(
                "oauth_linked_explicit: user=%s provider=%s uid=%s",
                link_to_user_id, provider, provider_uid,
            )
            return target_user

        # 1. oauth_accounts에서 기존 연결 조회
        existing_oauth = self._find_oauth_account(conn, provider, provider_uid)
        if existing_oauth:
            user_id = str(existing_oauth["user_id"])
            # 토큰 갱신
            self._update_oauth_account(
                conn,
                oauth_id=str(existing_oauth["id"]),
                access_token=encrypted_at,
                refresh_token=encrypted_rt,
                token_expires_at=token_expires_at,
                provider_email=email,
                provider_name=display_name,
                avatar_url=avatar_url,
                raw_profile=raw_profile,
            )
            user = users_repository.get_by_id(conn, user_id)
            if user and user.status != "ACTIVE":
                logger.warning("oauth_link: user %s is inactive", user_id)
                return None

            audit_emitter.emit(
                event_type="user.oauth_login",
                action="auth.oauth.login",
                actor_id=user_id,
                actor_type="user",
                resource_type="user",
                resource_id=user_id,
                result="success",
                metadata={"provider": provider},
            )
            return user

        # 2. 이메일로 기존 사용자 조회
        existing_user = users_repository.get_by_email(conn, email)
        if existing_user:
            if existing_user.status != "ACTIVE":
                logger.warning(
                    "oauth_link: user %s (email=%s) is inactive",
                    existing_user.id, email,
                )
                return None

            # 보안: 미확인 이메일(로컬 가입 중 인증 미완) 계정에 OAuth 를 자동 연결하면
            # 공격자가 피해자의 이메일로 선점 가입 → 피해자 OAuth 로그인 시 계정 탈취 가능.
            # → 기존 계정이 email_verified=False 이면 자동 연결을 거부한다.
            #    (사용자는 계정 관리 페이지에서 명시적 link_to_user_id 플로우로 연결해야 함)
            if not existing_user.email_verified:
                logger.warning(
                    "oauth_link_refused: existing user %s has unverified email %s — "
                    "potential account takeover attempt",
                    existing_user.id, email,
                )
                audit_emitter.emit(
                    event_type="user.oauth_link_refused",
                    action="auth.oauth.link",
                    actor_id=None,
                    actor_type="user",
                    resource_type="user",
                    resource_id=existing_user.id,
                    result="failure",
                    metadata={
                        "provider": provider,
                        "provider_uid": provider_uid,
                        "reason": "existing_email_unverified",
                    },
                )
                return None

            # 기존 사용자에 OAuth 계정 연결
            self._create_oauth_account(
                conn,
                user_id=existing_user.id,
                provider=provider,
                provider_uid=provider_uid,
                provider_email=email,
                provider_name=display_name,
                avatar_url=avatar_url,
                access_token=encrypted_at,
                refresh_token=encrypted_rt,
                token_expires_at=token_expires_at,
                raw_profile=raw_profile,
            )

            # avatar_url 갱신 (기존 사용자에 없는 경우)
            if avatar_url and not existing_user.avatar_url:
                users_repository.update(conn, existing_user.id, avatar_url=avatar_url)

            audit_emitter.emit(
                event_type="user.oauth_linked",
                action="auth.oauth.link",
                actor_id=existing_user.id,
                actor_type="user",
                resource_type="user",
                resource_id=existing_user.id,
                result="success",
                metadata={"provider": provider, "provider_uid": provider_uid},
            )
            logger.info(
                "oauth_linked: user=%s provider=%s uid=%s",
                existing_user.id, provider, provider_uid,
            )
            return existing_user

        # 3. 새 사용자 생성 + OAuth 계정 연결
        new_user = users_repository.create(
            conn,
            email=email,
            display_name=display_name,
            role_name="VIEWER",
            status="ACTIVE",
            auth_provider=provider,
            email_verified=True,  # GitLab이 이메일 인증 보장
            avatar_url=avatar_url,
        )

        self._create_oauth_account(
            conn,
            user_id=new_user.id,
            provider=provider,
            provider_uid=provider_uid,
            provider_email=email,
            provider_name=display_name,
            avatar_url=avatar_url,
            access_token=encrypted_at,
            refresh_token=encrypted_rt,
            token_expires_at=token_expires_at,
            raw_profile=raw_profile,
        )

        audit_emitter.emit(
            event_type="user.oauth_registered",
            action="auth.oauth.register",
            actor_id=new_user.id,
            actor_type="user",
            resource_type="user",
            resource_id=new_user.id,
            result="success",
            metadata={"provider": provider, "provider_uid": provider_uid},
        )
        logger.info(
            "oauth_registered: user=%s email=%s provider=%s",
            new_user.id, email, provider,
        )
        return new_user

    # ─── Private: DB helpers ───

    def _find_oauth_account(
        self,
        conn: psycopg2.extensions.connection,
        provider: str,
        provider_uid: str,
    ) -> dict | None:
        """oauth_accounts에서 (provider, provider_uid)로 조회."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM oauth_accounts WHERE provider = %s AND provider_uid = %s",
                (provider, provider_uid),
            )
            return cur.fetchone()

    def _create_oauth_account(
        self,
        conn: psycopg2.extensions.connection,
        *,
        user_id: str,
        provider: str,
        provider_uid: str,
        provider_email: str | None,
        provider_name: str | None,
        avatar_url: str | None,
        access_token: str | None,
        refresh_token: str | None,
        token_expires_at: datetime | None,
        raw_profile: dict | None,
    ) -> None:
        """oauth_accounts에 새 레코드를 생성한다."""
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_accounts
                    (user_id, provider, provider_uid, provider_email, provider_name,
                     avatar_url, access_token, refresh_token, token_expires_at, raw_profile)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id, provider, provider_uid, provider_email, provider_name,
                    avatar_url, access_token, refresh_token, token_expires_at,
                    json.dumps(raw_profile or {}),
                ),
            )

    def _update_oauth_account(
        self,
        conn: psycopg2.extensions.connection,
        *,
        oauth_id: str,
        access_token: str | None,
        refresh_token: str | None,
        token_expires_at: datetime | None,
        provider_email: str | None,
        provider_name: str | None,
        avatar_url: str | None,
        raw_profile: dict | None,
    ) -> None:
        """oauth_accounts의 토큰 및 프로필 정보를 갱신한다."""
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE oauth_accounts
                SET access_token = %s, refresh_token = %s, token_expires_at = %s,
                    provider_email = %s, provider_name = %s, avatar_url = %s,
                    raw_profile = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (
                    access_token, refresh_token, token_expires_at,
                    provider_email, provider_name, avatar_url,
                    json.dumps(raw_profile or {}), oauth_id,
                ),
            )

    def find_by_user_id(
        self,
        conn: psycopg2.extensions.connection,
        user_id: str,
        provider: str | None = None,
    ) -> list[dict]:
        """사용자의 OAuth 계정 목록을 조회한다."""
        with conn.cursor() as cur:
            if provider:
                cur.execute(
                    "SELECT * FROM oauth_accounts WHERE user_id = %s AND provider = %s",
                    (user_id, provider),
                )
            else:
                cur.execute(
                    "SELECT * FROM oauth_accounts WHERE user_id = %s",
                    (user_id,),
                )
            return cur.fetchall() or []


# 모듈 수준 싱글턴
gitlab_oauth_service = GitLabOAuthService()
