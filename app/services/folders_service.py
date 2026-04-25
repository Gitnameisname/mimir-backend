"""
Folders 서비스 — S3 Phase 2 FG 2-1.

책임:
  - 계층 폴더 CRUD + 이동 (materialized path 유지)
  - 순환 참조 방지 (자기 자신/하위를 부모로 지정 금지)
  - 깊이 상한 확인 (10)
  - document_folder 연결 관리 (Scope ACL 통과 문서만)
"""

import logging
from typing import Optional

import psycopg2.extensions

from app.api.auth.models import ActorContext
from app.api.errors.exceptions import (
    ApiConflictError,
    ApiNotFoundError,
    ApiValidationError,
)
from app.models.folder import Folder
from app.repositories.documents_repository import documents_repository
from app.repositories.folders_repository import (
    FOLDER_PATH_MAX_DEPTH,
    compute_child_path,
    folders_repository,
)
from app.services.documents_service import _resolve_viewer_scope_profile_ids
from app.utils.strings import normalize_display_name

logger = logging.getLogger(__name__)

_NAME_MIN = 1
_NAME_MAX = 200
_NAME_LABEL = "폴더 이름"


def _normalize_name(raw: str) -> str:
    """폴더 이름 정규화 (공백 압축 + 길이 검사 + '/' 금지).

    내부적으로 :func:`app.utils.strings.normalize_display_name` 에 위임한다.
    기존 import 호환(테스트 포함) 을 유지하기 위해 서비스 모듈에 남겨둔 thin wrapper.
    Docs: ``docs/함수도서관/backend.md`` §1.4 B6.
    """
    return normalize_display_name(
        raw,
        _NAME_MIN,
        _NAME_MAX,
        forbid_slash=True,
        label=_NAME_LABEL,
    )


def _require_actor(actor: Optional[ActorContext]) -> str:
    if actor is None or actor.actor_id is None:
        raise ApiValidationError("인증된 사용자만 폴더를 관리할 수 있습니다")
    return str(actor.actor_id)


class FoldersService:
    """계층 폴더 비즈니스 로직."""

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_folder(
        self,
        conn: psycopg2.extensions.connection,
        *,
        actor: ActorContext,
        parent_id: Optional[str],
        name: str,
    ) -> Folder:
        owner_id = _require_actor(actor)
        name = _normalize_name(name)

        parent_path: Optional[str] = None
        parent_depth = -1  # 루트의 parent 깊이 = -1 → 루트 자신 depth=0
        if parent_id is not None:
            parent = folders_repository.get_by_id(conn, parent_id, owner_id=owner_id)
            if parent is None:
                raise ApiNotFoundError(f"Parent folder '{parent_id}' not found")
            parent_path = parent.path
            parent_depth = parent.depth

        depth = parent_depth + 1
        if depth > FOLDER_PATH_MAX_DEPTH:
            raise ApiValidationError(
                f"폴더 깊이 상한({FOLDER_PATH_MAX_DEPTH}) 을 초과했습니다",
            )
        path = compute_child_path(parent_path, name)

        try:
            return folders_repository.create(
                conn,
                owner_id=owner_id,
                parent_id=parent_id,
                name=name,
                path=path,
                depth=depth,
            )
        except psycopg2.errors.UniqueViolation as exc:
            raise ApiConflictError(
                f"같은 경로의 폴더가 이미 존재합니다: '{path}'",
            ) from exc

    def get_folder(
        self,
        conn: psycopg2.extensions.connection,
        folder_id: str,
        *,
        actor: ActorContext,
    ) -> Folder:
        owner_id = _require_actor(actor)
        folder = folders_repository.get_by_id(conn, folder_id, owner_id=owner_id)
        if folder is None:
            raise ApiNotFoundError(f"Folder '{folder_id}' not found")
        return folder

    def list_folders(
        self,
        conn: psycopg2.extensions.connection,
        *,
        actor: ActorContext,
    ) -> list[Folder]:
        owner_id = _require_actor(actor)
        return folders_repository.list_by_owner(conn, owner_id)

    def rename_folder(
        self,
        conn: psycopg2.extensions.connection,
        folder_id: str,
        *,
        actor: ActorContext,
        new_name: str,
    ) -> Folder:
        self.get_folder(conn, folder_id, actor=actor)  # 소유권 + 존재 확인
        new_name = _normalize_name(new_name)
        try:
            renamed = folders_repository.rename(conn, folder_id, new_name=new_name)
        except psycopg2.errors.UniqueViolation as exc:
            raise ApiConflictError(
                "같은 경로의 폴더가 이미 존재합니다",
            ) from exc
        if renamed is None:
            raise ApiNotFoundError(f"Folder '{folder_id}' not found")
        return renamed

    def move_folder(
        self,
        conn: psycopg2.extensions.connection,
        folder_id: str,
        *,
        actor: ActorContext,
        new_parent_id: Optional[str],
    ) -> Folder:
        """폴더 이동.

        체크 항목:
          1. 소유권 (folder, new_parent 모두 actor 소유)
          2. 자기 자신 / 하위를 부모로 지정 금지 (순환 참조)
          3. 깊이 상한 10
        """
        owner_id = _require_actor(actor)
        current = self.get_folder(conn, folder_id, actor=actor)

        new_parent_path: Optional[str] = None
        new_parent_depth = -1
        if new_parent_id is not None:
            if new_parent_id == folder_id:
                raise ApiValidationError("자기 자신을 부모로 지정할 수 없습니다")
            # 순환 참조: 새 부모가 자기 자신의 하위인가
            if folders_repository.is_descendant(
                conn,
                ancestor_id=folder_id,
                maybe_descendant_id=new_parent_id,
            ):
                raise ApiValidationError(
                    "자기 자신의 하위 폴더로는 이동할 수 없습니다 (순환 참조)",
                )
            new_parent = folders_repository.get_by_id(
                conn, new_parent_id, owner_id=owner_id,
            )
            if new_parent is None:
                raise ApiNotFoundError(f"Parent folder '{new_parent_id}' not found")
            new_parent_path = new_parent.path
            new_parent_depth = new_parent.depth

        new_depth = new_parent_depth + 1
        # 하위 depth 도 함께 증가하므로 하위 max depth 체크 필요
        depth_delta = new_depth - current.depth
        subtree = [
            f for f in folders_repository.list_by_owner(conn, owner_id)
            if f.path.startswith(current.path)
        ]
        max_subtree_depth = max((f.depth for f in subtree), default=current.depth)
        if max_subtree_depth + depth_delta > FOLDER_PATH_MAX_DEPTH:
            raise ApiValidationError(
                f"이동하면 일부 하위 폴더가 깊이 상한({FOLDER_PATH_MAX_DEPTH})을 초과합니다",
            )

        try:
            moved = folders_repository.move(
                conn,
                folder_id,
                new_parent_id=new_parent_id,
                new_parent_path=new_parent_path,
                new_depth=new_depth,
            )
        except psycopg2.errors.UniqueViolation as exc:
            raise ApiConflictError(
                "이동 후 경로가 기존 폴더와 충돌합니다",
            ) from exc
        if moved is None:
            raise ApiNotFoundError(f"Folder '{folder_id}' not found")
        return moved

    def delete_folder(
        self,
        conn: psycopg2.extensions.connection,
        folder_id: str,
        *,
        actor: ActorContext,
    ) -> None:
        """하위 폴더 / 문서가 없을 때만 삭제. 있으면 409."""
        self.get_folder(conn, folder_id, actor=actor)
        ok = folders_repository.delete_if_empty(conn, folder_id)
        if not ok:
            raise ApiConflictError(
                "하위 폴더 또는 문서가 있는 폴더는 삭제할 수 없습니다",
            )

    # ------------------------------------------------------------------
    # document_folder — 문서의 폴더 지정/해제
    # ------------------------------------------------------------------

    def set_document_folder(
        self,
        conn: psycopg2.extensions.connection,
        *,
        actor: ActorContext,
        document_id: str,
        folder_id: Optional[str],
    ) -> None:
        """문서를 폴더에 배치 / 해제.

        체크:
          1. 문서 viewer Scope 통과 (documents.scope_profile_id)
          2. folder_id 가 있으면 actor 소유 폴더
        """
        owner_id = _require_actor(actor)
        viewer_ids = _resolve_viewer_scope_profile_ids(actor)
        doc = documents_repository.get_by_id(
            conn, document_id, viewer_scope_profile_ids=viewer_ids,
        )
        if doc is None:
            raise ApiNotFoundError(f"Document '{document_id}' not found")

        if folder_id is not None:
            folder = folders_repository.get_by_id(
                conn, folder_id, owner_id=owner_id,
            )
            if folder is None:
                raise ApiNotFoundError(f"Folder '{folder_id}' not found")

        folders_repository.set_folder(
            conn, document_id=document_id, folder_id=folder_id,
        )


# 모듈 수준 싱글턴
folders_service = FoldersService()
