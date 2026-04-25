"""
Folders persistence repository — S3 Phase 2 FG 2-1.

책임
----
  - folders 테이블 CRUD (self-referencing tree)
  - materialized path 유지 (이동 시 하위 전체 재계산)
  - document_folder N:1 테이블 관리
  - 순환 참조 방지는 **서비스 계층**에서 선제 검증, repository 는 DB CHECK 로
    최종 방어

설계 원칙
---------
  - owner 별 트리 격리 — 다른 owner 의 폴더로 이동 금지 (서비스에서 강제)
  - path 포맷: 항상 ``/`` 시작/종료. 루트 = ``/<name>/``, 자식 = ``<parent.path><name>/``
  - depth 상한 10 (Alembic CHECK + 서비스 재확인)
"""

import logging
from typing import Any, Optional, Sequence

import psycopg2.extensions

from app.models.folder import Folder
from app.db.cursor_helpers import fetch_many_as, fetch_one_as

logger = logging.getLogger(__name__)

FOLDER_PATH_MAX_DEPTH = 10

_FOLDER_SELECT_COLS = (
    "id, owner_id, parent_id, name, path, depth, created_at, updated_at"
)


def _row_to_folder(row: dict[str, Any]) -> Folder:
    return Folder(
        id=str(row["id"]),
        owner_id=str(row["owner_id"]),
        parent_id=str(row["parent_id"]) if row.get("parent_id") else None,
        name=row["name"],
        path=row["path"],
        depth=int(row["depth"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def compute_child_path(parent_path: Optional[str], name: str) -> str:
    """부모 path + 자식 이름 → 자식의 materialized path.

    - 루트 (parent_path=None): ``/<name>/``
    - 자식: ``<parent_path><name>/``
    """
    safe_name = name.replace("/", "_")  # 이름에 '/' 가 섞이면 구분자 혼동
    if parent_path is None:
        return f"/{safe_name}/"
    assert parent_path.startswith("/") and parent_path.endswith("/")
    return f"{parent_path}{safe_name}/"


class FoldersRepository:
    """폴더 테이블 CRUD + materialized path 재계산."""

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        conn: psycopg2.extensions.connection,
        *,
        owner_id: str,
        parent_id: Optional[str],
        name: str,
        path: str,
        depth: int,
    ) -> Folder:
        sql = f"""
            INSERT INTO folders (owner_id, parent_id, name, path, depth)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING {_FOLDER_SELECT_COLS}
        """
        with conn.cursor() as cur:
            cur.execute(sql, (owner_id, parent_id, name, path, depth))
            row = cur.fetchone()
        return _row_to_folder(dict(row))

    def get_by_id(
        self,
        conn: psycopg2.extensions.connection,
        folder_id: str,
        *,
        owner_id: Optional[str] = None,
    ) -> Optional[Folder]:
        where_parts = ["id = %s"]
        params: list[Any] = [folder_id]
        if owner_id is not None:
            where_parts.append("owner_id = %s")
            params.append(owner_id)

        sql = f"""
            SELECT {_FOLDER_SELECT_COLS}
            FROM folders
            WHERE {' AND '.join(where_parts)}
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_folder(dict(row))

    def list_by_owner(
        self,
        conn: psycopg2.extensions.connection,
        owner_id: str,
    ) -> list[Folder]:
        """소유자의 전체 폴더 트리를 path 순으로 반환 (root-first)."""
        sql = f"""
            SELECT {_FOLDER_SELECT_COLS}
            FROM folders
            WHERE owner_id = %s
            ORDER BY path ASC
        """
        return fetch_many_as(conn, sql, (owner_id,), lambda r: _row_to_folder(dict(r)))

    def rename(
        self,
        conn: psycopg2.extensions.connection,
        folder_id: str,
        *,
        new_name: str,
    ) -> Optional[Folder]:
        """이름만 변경. path 도 함께 재계산되어야 하므로 내부에서 하위 재계산 호출."""
        # 현재 path 조회
        current = self.get_by_id(conn, folder_id)
        if current is None:
            return None

        safe_name = new_name.replace("/", "_")
        # 새 path 계산: 부모 path + new_name + '/'
        # 부모 path = current.path 에서 current.name 제거
        prefix = current.path[: -(len(current.name) + 1)]  # '<prefix>/<name>/' → '<prefix>/'
        new_path = f"{prefix}{safe_name}/"

        # 자신 업데이트 + 하위 재계산
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE folders
                SET name = %s, path = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (new_name, new_path, folder_id),
            )
            # 하위 전체 path prefix 치환
            cur.execute(
                """
                UPDATE folders
                SET path = %s || substring(path FROM %s), updated_at = NOW()
                WHERE owner_id = %s AND path LIKE %s AND id <> %s
                """,
                (
                    new_path,
                    len(current.path) + 1,  # substring 은 1-based
                    current.owner_id,
                    current.path + "%",
                    folder_id,
                ),
            )
        return self.get_by_id(conn, folder_id)

    def move(
        self,
        conn: psycopg2.extensions.connection,
        folder_id: str,
        *,
        new_parent_id: Optional[str],
        new_parent_path: Optional[str],
        new_depth: int,
    ) -> Optional[Folder]:
        """부모 변경 + 자신 및 하위 전체 path/depth 재계산.

        ``new_parent_path`` 는 서비스 계층이 get_by_id 로 미리 조회해 전달.
        새 path = new_parent_path + self.name + '/' (루트면 '/self.name/').

        depth 차이만큼 하위 전체 depth 를 shift 한다.
        """
        current = self.get_by_id(conn, folder_id)
        if current is None:
            return None

        # 새 path 계산
        new_path = compute_child_path(new_parent_path, current.name)
        depth_delta = new_depth - current.depth

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE folders
                SET parent_id = %s, path = %s, depth = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (new_parent_id, new_path, new_depth, folder_id),
            )
            # 하위: path prefix 치환 + depth 이동
            cur.execute(
                """
                UPDATE folders
                SET path = %s || substring(path FROM %s),
                    depth = depth + %s,
                    updated_at = NOW()
                WHERE owner_id = %s AND path LIKE %s AND id <> %s
                """,
                (
                    new_path,
                    len(current.path) + 1,
                    depth_delta,
                    current.owner_id,
                    current.path + "%",
                    folder_id,
                ),
            )
        return self.get_by_id(conn, folder_id)

    def delete_if_empty(
        self,
        conn: psycopg2.extensions.connection,
        folder_id: str,
    ) -> bool:
        """하위 폴더 / 연결된 문서가 없는 경우에만 삭제.

        Returns:
            True  : 삭제 성공
            False : 폴더 없음 또는 하위 존재로 삭제 거부
        """
        with conn.cursor() as cur:
            # 하위 폴더 여부
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM folders WHERE parent_id = %s",
                (folder_id,),
            )
            if int(cur.fetchone()["cnt"]) > 0:
                return False
            # 연결된 문서 여부
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM document_folder WHERE folder_id = %s",
                (folder_id,),
            )
            if int(cur.fetchone()["cnt"]) > 0:
                return False

            cur.execute("DELETE FROM folders WHERE id = %s", (folder_id,))
            return cur.rowcount > 0

    def is_descendant(
        self,
        conn: psycopg2.extensions.connection,
        *,
        ancestor_id: str,
        maybe_descendant_id: str,
    ) -> bool:
        """maybe_descendant 가 ancestor 의 하위(자기 자신 포함)인가.

        순환 참조 방지에 사용 — 자신이나 하위를 새 부모로 지정할 수 없음.
        """
        ancestor = self.get_by_id(conn, ancestor_id)
        target = self.get_by_id(conn, maybe_descendant_id)
        if ancestor is None or target is None:
            return False
        if ancestor.owner_id != target.owner_id:
            return False  # 다른 owner 는 애초에 관계 없음
        return target.path == ancestor.path or target.path.startswith(ancestor.path)

    # ------------------------------------------------------------------
    # document_folder — N:1
    # ------------------------------------------------------------------

    def set_folder(
        self,
        conn: psycopg2.extensions.connection,
        *,
        document_id: str,
        folder_id: Optional[str],
    ) -> None:
        """문서의 폴더 지정 / 해제.

        folder_id=None 이면 해제 (DELETE). 있으면 UPSERT (ON CONFLICT UPDATE).
        """
        with conn.cursor() as cur:
            if folder_id is None:
                cur.execute(
                    "DELETE FROM document_folder WHERE document_id = %s",
                    (document_id,),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO document_folder (document_id, folder_id)
                    VALUES (%s, %s)
                    ON CONFLICT (document_id)
                    DO UPDATE SET folder_id = EXCLUDED.folder_id, assigned_at = NOW()
                    """,
                    (document_id, folder_id),
                )

    def get_folder_of_document(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
    ) -> Optional[str]:
        return fetch_one_as(conn, "SELECT folder_id FROM document_folder WHERE document_id = %s", (document_id,), lambda row: str(row["folder_id"]))


# 모듈 수준 싱글턴
folders_repository = FoldersRepository()
