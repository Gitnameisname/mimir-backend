"""
OAuth 토큰 암호화/복호화 (AES-256-GCM).

Phase 14-4: GitLab OAuth 토큰을 DB에 저장할 때 암호화한다.

보안 원칙:
  - AES-256-GCM: 인증 + 암호화를 동시에 제공 (AEAD)
  - 12바이트 랜덤 nonce: 매 암호화마다 고유
  - base64로 인코딩하여 TEXT 컬럼에 저장
  - 키는 환경 변수(OAUTH_TOKEN_ENCRYPTION_KEY)에서 로드
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

logger = logging.getLogger(__name__)

_NONCE_SIZE = 12  # 96-bit nonce (GCM 권장)


def _get_encryption_key() -> Optional[bytes]:
    """환경 변수에서 AES-256 키를 가져온다.

    Returns:
        32바이트 키 또는 None (미설정 시).
    """
    key = settings.oauth_encryption_key_bytes
    if key and len(key) != 32:
        logger.error("OAUTH_TOKEN_ENCRYPTION_KEY must be exactly 32 bytes (256 bits)")
        return None
    return key


def encrypt_token(plaintext: str) -> str:
    """AES-256-GCM으로 토큰을 암호화한다.

    Args:
        plaintext: 암호화할 평문 토큰.

    Returns:
        base64 인코딩된 암호문 (nonce 포함).

    Raises:
        RuntimeError: OAUTH_TOKEN_ENCRYPTION_KEY 미설정 시. 평문 저장은 허용하지 않는다.
    """
    key = _get_encryption_key()
    if key is None:
        raise RuntimeError(
            "OAUTH_TOKEN_ENCRYPTION_KEY가 설정되지 않았습니다. "
            "OAuth 토큰 평문 저장은 보안 정책상 허용되지 않습니다."
        )

    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_SIZE)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    # nonce + ciphertext를 하나의 blob으로 base64 인코딩
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_token(encrypted: str) -> Optional[str]:
    """AES-256-GCM으로 토큰을 복호화한다.

    Args:
        encrypted: base64 인코딩된 암호문 (nonce 포함).

    Returns:
        평문 토큰 또는 None (복호화 실패 시).
    """
    key = _get_encryption_key()
    if key is None:
        logger.error("decrypt_token: OAUTH_TOKEN_ENCRYPTION_KEY 미설정 — 복호화 불가")
        return None

    try:
        data = base64.b64decode(encrypted)
        if len(data) < _NONCE_SIZE + 16:  # nonce(12) + 최소 GCM tag(16)
            logger.error("decrypt_token: data too short")
            return None

        nonce = data[:_NONCE_SIZE]
        ciphertext = data[_NONCE_SIZE:]
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")
    except Exception:
        logger.exception("decrypt_token: decryption failed")
        return None
