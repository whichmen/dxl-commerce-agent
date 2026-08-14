"""Local SQLite state store for deduplication and minimal session state."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from .models import IncomingMessage


class LocalStateStore:
    """Persistent local state used by a single worker process."""

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dedup_keys (
                    tenant_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (tenant_id, platform, store_id, session_key, message_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_state (
                    tenant_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'auto',
                    last_in_msg_id TEXT NOT NULL DEFAULT '',
                    last_reply_text TEXT NOT NULL DEFAULT '',
                    last_reply_at_ms INTEGER NOT NULL DEFAULT 0,
                    updated_at_ms INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, platform, store_id, session_key)
                )
                """
            )
            self._migrate_legacy_session_id(conn)
            self._migrate_session_state_columns(conn)

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}

    def _migrate_legacy_session_id(self, conn: sqlite3.Connection) -> None:
        for table in ("dedup_keys", "session_state"):
            cols = self._table_columns(conn, table)
            if ("session_key" not in cols) and ("session_id" in cols):
                conn.execute(f"ALTER TABLE {table} RENAME COLUMN session_id TO session_key")

    def _migrate_session_state_columns(self, conn: sqlite3.Connection) -> None:
        cols = self._table_columns(conn, "session_state")
        if "last_reply_text" not in cols:
            conn.execute(
                "ALTER TABLE session_state ADD COLUMN last_reply_text TEXT NOT NULL DEFAULT ''"
            )

    def mark_incoming_if_new(self, msg: IncomingMessage) -> bool:
        """
        Insert incoming message dedup key.

        Returns True when this message is first seen, False when duplicate.
        """
        session_key = msg.session_key()
        with self._connect() as conn:
            # Cross-session dedup: same platform/store/message_id should only be processed once.
            existing = conn.execute(
                """
                SELECT 1
                FROM dedup_keys
                WHERE tenant_id=? AND platform=? AND store_id=? AND message_id=?
                LIMIT 1
                """,
                (
                    msg.tenant_id,
                    msg.platform,
                    msg.store_id,
                    msg.message_id,
                ),
            ).fetchone()
            if existing:
                return False

        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO dedup_keys (
                        tenant_id, platform, store_id, session_key, message_id, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        msg.tenant_id,
                        msg.platform,
                        msg.store_id,
                        session_key,
                        msg.message_id,
                        msg.timestamp_ms,
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def unmark_incoming(self, msg: IncomingMessage) -> None:
        """Remove a previously inserted dedup key so the message can be retried."""
        session_key = msg.session_key()
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM dedup_keys
                WHERE tenant_id=? AND platform=? AND store_id=? AND session_key=? AND message_id=?
                """,
                (
                    msg.tenant_id,
                    msg.platform,
                    msg.store_id,
                    session_key,
                    msg.message_id,
                ),
            )

    def get_mode(self, msg: IncomingMessage) -> str:
        session_key = msg.session_key()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT mode
                FROM session_state
                WHERE tenant_id=? AND platform=? AND store_id=? AND session_key=?
                """,
                (msg.tenant_id, msg.platform, msg.store_id, session_key),
            ).fetchone()
        return str(row[0]) if row else "auto"

    def set_mode(self, msg: IncomingMessage, mode: str, at_ms: int) -> None:
        session_key = msg.session_key()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_state (
                    tenant_id, platform, store_id, session_key, mode, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, platform, store_id, session_key)
                DO UPDATE SET mode=excluded.mode, updated_at_ms=excluded.updated_at_ms
                """,
                (
                    msg.tenant_id,
                    msg.platform,
                    msg.store_id,
                    session_key,
                    mode,
                    at_ms,
                ),
            )

    def can_auto_reply(
        self,
        msg: IncomingMessage,
        now_ts_ms: int,
        min_reply_interval_ms: int,
    ) -> bool:
        """
        Return whether this session can be auto-replied now.

        Rules:
        - mode must be auto
        - no rapid duplicate send in short interval
        """
        session_key = msg.session_key()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT mode, last_reply_at_ms
                FROM session_state
                WHERE tenant_id=? AND platform=? AND store_id=? AND session_key=?
                """,
                (msg.tenant_id, msg.platform, msg.store_id, session_key),
            ).fetchone()

        if not row:
            return True

        mode, last_reply_at_ms = str(row[0]), int(row[1] or 0)
        if mode != "auto":
            return False

        if last_reply_at_ms and now_ts_ms - last_reply_at_ms < min_reply_interval_ms:
            return False
        return True

    def get_last_reply_text(
        self,
        *,
        tenant_id: str,
        platform: str,
        store_id: str,
        session_key: str,
    ) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT last_reply_text
                FROM session_state
                WHERE tenant_id=? AND platform=? AND store_id=? AND session_key=?
                """,
                (tenant_id, platform, store_id, session_key),
            ).fetchone()
        if not row:
            return ""
        return str(row[0] or "")

    def mark_reply(self, msg: IncomingMessage, at_ms: int, reply_text: str = "") -> None:
        session_key = msg.session_key()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_state (
                    tenant_id, platform, store_id, session_key,
                    mode, last_in_msg_id, last_reply_text, last_reply_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, 'auto', ?, ?, ?, ?)
                ON CONFLICT(tenant_id, platform, store_id, session_key)
                DO UPDATE SET
                    last_in_msg_id=excluded.last_in_msg_id,
                    last_reply_text=excluded.last_reply_text,
                    last_reply_at_ms=excluded.last_reply_at_ms,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (
                    msg.tenant_id,
                    msg.platform,
                    msg.store_id,
                    session_key,
                    msg.message_id,
                    str(reply_text or ""),
                    at_ms,
                    at_ms,
                ),
            )
