from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any


class IdentityStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS identities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    org_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    room_name TEXT NOT NULL DEFAULT '',
                    platform_nickname TEXT NOT NULL,
                    km_name TEXT NOT NULL,
                    dxl_nickname TEXT NOT NULL DEFAULT '',
                    km_identity_type TEXT NOT NULL DEFAULT 'mapped',
                    last_tid TEXT NOT NULL DEFAULT '',
                    UNIQUE(org_id, platform, room_name, platform_nickname, km_name)
                );
                CREATE INDEX IF NOT EXISTS idx_identity_lookup
                    ON identities(org_id, platform, room_name, platform_nickname);
                CREATE INDEX IF NOT EXISTS idx_identity_km
                    ON identities(org_id, platform, km_name);

                CREATE TABLE IF NOT EXISTS blacklist (
                    org_id TEXT NOT NULL,
                    wangwang_id TEXT NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(org_id, wangwang_id)
                );
                """
            )
            self._conn.commit()

    def upsert_identity(self, record: dict[str, Any]) -> None:
        values = {
            "org_id": str(record.get("org_id") or "").strip(),
            "platform": str(record.get("platform") or "").strip(),
            "room_name": str(record.get("room_name") or "").strip(),
            "platform_nickname": str(record.get("platform_nickname") or "").strip(),
            "km_name": str(record.get("km_name") or "").strip(),
            "dxl_nickname": str(record.get("dxl_nickname") or "").strip(),
            "km_identity_type": str(record.get("km_identity_type") or "mapped").strip()
            or "mapped",
            "last_tid": str(record.get("last_tid") or "").strip(),
        }
        required = ("org_id", "platform", "platform_nickname", "km_name")
        if any(not values[key] for key in required):
            raise ValueError("org_id, platform, platform_nickname and km_name are required")

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO identities (
                    org_id, platform, room_name, platform_nickname, km_name,
                    dxl_nickname, km_identity_type, last_tid
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(org_id, platform, room_name, platform_nickname, km_name)
                DO UPDATE SET
                    dxl_nickname = excluded.dxl_nickname,
                    km_identity_type = excluded.km_identity_type,
                    last_tid = excluded.last_tid
                """,
                tuple(values[key] for key in (
                    "org_id",
                    "platform",
                    "room_name",
                    "platform_nickname",
                    "km_name",
                    "dxl_nickname",
                    "km_identity_type",
                    "last_tid",
                )),
            )
            self._conn.commit()

    def lookup(
        self,
        *,
        org_id: str,
        platform: str,
        room_name: str,
        alias_value: str,
        limit: int = 20,
    ) -> list[dict[str, str]]:
        clauses = [
            "org_id = ?",
            "platform = ?",
            "(platform_nickname = ? OR km_name = ? OR dxl_nickname = ?)",
        ]
        params: list[str | int] = [org_id, platform, alias_value, alias_value, alias_value]
        if room_name:
            clauses.append("(room_name = ? OR room_name = '')")
            params.append(room_name)
        params.append(max(1, min(int(limit), 100)))
        query = f"""
            SELECT platform_nickname, km_name, dxl_nickname, km_identity_type, last_tid
            FROM identities
            WHERE {' AND '.join(clauses)}
            ORDER BY CASE WHEN room_name = ? THEN 0 ELSE 1 END, id DESC
            LIMIT ?
        """
        params.insert(-1, room_name)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {
                "platform_nickname": str(row["platform_nickname"] or ""),
                "km_name": str(row["km_name"] or ""),
                "dxl_nickname": str(row["dxl_nickname"] or ""),
                "km_identity_type": str(row["km_identity_type"] or ""),
                "last_tid": str(row["last_tid"] or ""),
            }
            for row in rows
        ]

    def blacklist_add(self, *, org_id: str, wangwang_id: str, expires_at_ms: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO blacklist(org_id, wangwang_id, expires_at_ms)
                VALUES (?, ?, ?)
                ON CONFLICT(org_id, wangwang_id)
                DO UPDATE SET expires_at_ms = excluded.expires_at_ms
                """,
                (org_id, wangwang_id, int(expires_at_ms)),
            )
            self._conn.commit()

    def blacklist_remove(self, *, org_id: str, wangwang_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM blacklist WHERE org_id = ? AND wangwang_id = ?",
                (org_id, wangwang_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0
