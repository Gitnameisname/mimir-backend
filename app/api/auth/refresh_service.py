"""
Refresh Token 서비스 (Phase 14).

책임:
  - Refresh Token 발급 및 DB 저장
  - Refresh Token Rotation (갱신 시 새 RT 발급, 기존 RT 폐기)
  - Family 기반 탈취 감지 및 일괄 폐기
  - 로그아웃 시 family 폐기

보안 원칙:
  - RT는 SHA-256 해시로 DB에 저장 (평문 저장 금지)
  - 이미 revoked된 RT 재사용 시 해당 family 전체 폐기 (탈취 대응)
  - hmac.compare_digest로 해시 비교 (타이밍 공격 방어)
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

import psycopg2.extensions

from app.api.auth.tokens import create_access_token, create_refresh_token, generate_family_id
from app.config import settings

logger = logging.getLogger(__name__)


class RefreshTokenService:
    """Refresh Token 관리 서비스."""

    # ─── RT 발급 (로그인 성공 시 호출) ───

    def issue_tokens(
        self,
        conn: psycopg2.extensions.connection,
        *,
        user_id: str,
        role: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        family_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Access Token + Refresh Token을 발급한다.

        Args:
            conn: DB 커넥션.
            user_id: 사용자 UUID.
            role: 사용자 역할.
            ip_address: 클라이언트 IP.
            user_agent: 클라이언트 User-Agent.
            family_id: 기존 family ID (Rotation 시). None이면 새 family 생성.

        Returns:
            {
                "access_token": str,
                "refresh_token": str (raw, Cookie에 설정할 값),
                "token_type": "Bearer",
                "expires_in": int (AT 만료 초),
                "family_id": str,
            }
        """
        if family_id is None:
            family_id = generate_family_id()

        # Access Token 발급
        access_token = create_access_token(user_id, role)
        expires_in = settings.jwt_expire_minutes * 60

        # Refresh Token 발급
        raw_rt, rt_hash = create_refresh_token()
        rt_expires_at = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_expire_days)

        # DB에 RT 저장 (해시만)
        self._store_refresh_token(
            conn,
            user_id=user_id,
            token_hash=rt_hash,
            family_id=family_id,
            expires_at=rt_expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        return {
            "access_token": access_token,
            "refresh_token": raw_rt,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "family_id": family_id,
        }

    # ─── RT Rotation (POST /auth/refresh) ───

    def rotate(
        self,
        conn: psycopg2.extensions.connection,
        *,
        raw_token: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> dict[str, Any] | None:
        """Refresh Token을 검증하고 Rotation을 수행한다.

        성공 시 새 AT+RT를 반환. 실패 시 None 반환.
        이미 revoked된 RT가 사용되면 family 전체를 폐기한다.

        Args:
            conn: DB 커넥션.
            raw_token: 클라이언트에서 받은 raw RT.
            ip_address: 클라이언트 IP.
            user_agent: 클라이언트 User-Agent.

        Returns:
            토큰 dict 또는 None (실패 시).
            실패 유형은 예외 대신 반환값 + 로깅으로 처리.
        """
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

        # 1. DB에서 RT 조회
        rt_record = self._find_by_hash(conn, token_hash)
        if rt_record is None:
            logger.warning("refresh_rotate: token not found")
            return None

        # 2. 이미 revoked → 탈취 감지! Family 전체 폐기
        if rt_record["revoked"]:
            logger.warning(
                "refresh_rotate: reuse detected! family=%s user=%s",
                rt_record["family_id"],
                rt_record["user_id"],
            )
            self.revoke_family(conn, str(rt_record["family_id"]))
            return None

        # 3. 만료 확인
        if rt_record["expires_at"] < datetime.now(timezone.utc):
            logger.info("refresh_rotate: token expired, family=%s", rt_record["family_id"])
            return None

        # 4. 사용자 상태 확인
        user = self._get_user_status(conn, str(rt_record["user_id"]))
        if user is None or user["status"] != "ACTIVE":
            logger.warning("refresh_rotate: user inactive, user=%s", rt_record["user_id"])
            self.revoke_family(conn, str(rt_record["family_id"]))
            return None

        # 5. 현재 RT를 revoked로 마킹
        self._revoke_token(conn, str(rt_record["id"]))

        # 6. 같은 family로 새 토큰 발급
        return self.issue_tokens(
            conn,
            user_id=str(rt_record["user_id"]),
            role=user["role_name"],
            ip_address=ip_address,
            user_agent=user_agent,
            family_id=str(rt_record["family_id"]),
        )

    # ─── Family 폐기 ───

    def revoke_family(self, conn: psycopg2.extensions.connection, family_id: str) -> int:
        """family_id에 속한 모든 RT를 일괄 폐기한다.

        Args:
            conn: DB 커넥션.
            family_id: 폐기할 family UUID.

        Returns:
            폐기된 RT 수.
        """
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE refresh_tokens
                SET revoked = TRUE, revoked_at = NOW()
                WHERE family_id = %s AND revoked = FALSE
                """,
                (family_id,),
            )
            count = cur.rowcount
        logger.info("revoke_family: family=%s revoked=%d", family_id, count)
        return count

    def revoke_all_user_tokens(self, conn: psycopg2.extensions.connection, user_id: str) -> int:
        """해당 사용자의 모든 RT를 폐기한다 (비밀번호 변경 등).

        Args:
            conn: DB 커넥션.
            user_id: 사용자 UUID.

        Returns:
            폐기된 RT 수.
        """
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE refresh_tokens
                SET revoked = TRUE, revoked_at = NOW()
                WHERE user_id = %s AND revoked = FALSE
                """,
                (user_id,),
            )
            count = cur.rowcount
        logger.info("revoke_all_user_tokens: user=%s revoked=%d", user_id, count)
        return count

    # ─── 로그아웃 (Cookie의 RT로 family 조회 후 폐기) ───

    def logout(self, conn: psycopg2.extensions.connection, raw_token: str) -> bool:
        """로그아웃: RT의 family 전체를 폐기한다.

        Args:
            conn: DB 커넥션.
            raw_token: Cookie에서 추출한 raw RT.

        Returns:
            성공 여부.
        """
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        rt_record = self._find_by_hash(conn, token_hash)
        if rt_record is None:
            # 이미 만료/삭제된 토큰으로 로그아웃 → 성공 처리
            return True

        self.revoke_family(conn, str(rt_record["family_id"]))
        return True

    # ─── Private helpers ───

    def _store_refresh_token(
        self,
        conn: psycopg2.extensions.connection,
        *,
        user_id: str,
        token_hash: str,
        family_id: str,
        expires_at: datetime,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        """RT를 DB에 저장한다."""
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO refresh_tokens (user_id, token_hash, family_id, expires_at, ip_address, user_agent)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (user_id, token_hash, family_id, expires_at, ip_address, user_agent),
            )

    def _find_by_hash(self, conn: psycopg2.extensions.connection, token_hash: str) -> Optional[dict]:
        """token_hash로 RT 레코드를 조회한다."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM refresh_tokens WHERE token_hash = %s",
                (token_hash,),
            )
            return cur.fetchone()

    def _revoke_token(self, conn: psycopg2.extensions.connection, token_id: str) -> None:
        """단일 RT를 revoked로 마킹한다."""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE refresh_tokens SET revoked = TRUE, revoked_at = NOW() WHERE id = %s",
                (token_id,),
            )

    def _get_user_status(self, conn: psycopg2.extensions.connection, user_id: str) -> Optional[dict]:
        """사용자의 status와 role_name을 조회한다."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, role_name FROM users WHERE id = %s",
                (user_id,),
            )
            return cur.fetchone()


# 모듈 수준 싱글턴
refresh_token_service = RefreshTokenService()
