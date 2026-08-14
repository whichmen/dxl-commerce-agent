from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import DecisionInternal, IncomingEvent, now_ms


class SQLiteStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    received_at_ms INTEGER NOT NULL,
                    tenant_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    platform_nickname TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    media_url TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_events_trace_id ON events(trace_id);
                CREATE INDEX IF NOT EXISTS idx_events_session_key ON events(session_key);
                CREATE INDEX IF NOT EXISTS idx_events_received_at ON events(received_at_ms);
                CREATE INDEX IF NOT EXISTS idx_events_dedup ON events(tenant_id, platform, store_id, message_id);

                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    session_key TEXT NOT NULL,
                    category TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    action TEXT NOT NULL,
                    reply_text TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    amount REAL NOT NULL,
                    need_more INTEGER NOT NULL,
                    manual INTEGER NOT NULL,
                    extra_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_decisions_trace_id ON decisions(trace_id);
                CREATE INDEX IF NOT EXISTS idx_decisions_session_key ON decisions(session_key);
                CREATE INDEX IF NOT EXISTS idx_decisions_created_at ON decisions(created_at_ms);

                CREATE TABLE IF NOT EXISTS wecom_send_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_ms INTEGER NOT NULL,
                    member_id TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_wecom_send_created_at ON wecom_send_log(created_at_ms);
                CREATE INDEX IF NOT EXISTS idx_wecom_send_member_id ON wecom_send_log(member_id);

                CREATE TABLE IF NOT EXISTS delivery_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sent_at_ms INTEGER NOT NULL,
                    tenant_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sent_text TEXT NOT NULL,
                    sent_image_urls_json TEXT NOT NULL,
                    reason TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_delivery_lookup
                    ON delivery_receipts(tenant_id, platform, store_id, message_id, sent_at_ms);
                CREATE INDEX IF NOT EXISTS idx_delivery_session
                    ON delivery_receipts(tenant_id, platform, store_id, session_key, sent_at_ms);
                CREATE INDEX IF NOT EXISTS idx_delivery_trace_id
                    ON delivery_receipts(trace_id);

                CREATE TABLE IF NOT EXISTS media_assets (
                    media_id TEXT PRIMARY KEY,
                    created_at_ms INTEGER NOT NULL,
                    source_ref TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    sha1 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_media_assets_source_ref
                    ON media_assets(source_ref);
                """
            )
            self._conn.execute(
                """
                DELETE FROM delivery_receipts
                WHERE id NOT IN (
                    SELECT MAX(id)
                    FROM delivery_receipts
                    GROUP BY tenant_id, platform, store_id, message_id
                )
                """
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_idempotency
                ON delivery_receipts(tenant_id, platform, store_id, message_id)
                """
            )
            self._migrate_legacy_schema()
            self._assert_strict_schema()
            self._conn.commit()

    def get_media_asset_by_source_ref(self, source_ref: str) -> dict[str, Any] | None:
        source = str(source_ref or "").strip()
        if not source:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT media_id, source_ref, local_path, sha1, size_bytes, created_at_ms
                FROM media_assets
                WHERE source_ref = ?
                ORDER BY created_at_ms DESC
                LIMIT 1
                """,
                (source,),
            ).fetchone()
        if row is None:
            return None
        return {
            "media_id": str(row["media_id"] or ""),
            "source_ref": str(row["source_ref"] or ""),
            "local_path": str(row["local_path"] or ""),
            "sha1": str(row["sha1"] or ""),
            "size_bytes": int(row["size_bytes"] or 0),
            "created_at_ms": int(row["created_at_ms"] or 0),
        }

    def get_media_asset_by_sha1(self, sha1: str) -> dict[str, Any] | None:
        digest = str(sha1 or "").strip().lower()
        if not digest:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT media_id, source_ref, local_path, sha1, size_bytes, created_at_ms
                FROM media_assets
                WHERE lower(sha1) = ?
                ORDER BY created_at_ms DESC
                LIMIT 1
                """,
                (digest,),
            ).fetchone()
        if row is None:
            return None
        return {
            "media_id": str(row["media_id"] or ""),
            "source_ref": str(row["source_ref"] or ""),
            "local_path": str(row["local_path"] or ""),
            "sha1": str(row["sha1"] or ""),
            "size_bytes": int(row["size_bytes"] or 0),
            "created_at_ms": int(row["created_at_ms"] or 0),
        }

    def upsert_media_asset(
        self,
        *,
        media_id: str,
        source_ref: str,
        local_path: str,
        sha1: str,
        size_bytes: int,
    ) -> None:
        mid = str(media_id or "").strip()
        source = str(source_ref or "").strip()
        path = str(local_path or "").strip()
        digest = str(sha1 or "").strip()
        size = int(size_bytes or 0)
        if (not mid) or (not source) or (not path) or (not digest) or size <= 0:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO media_assets (
                    media_id, created_at_ms, source_ref, local_path, sha1, size_bytes
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_id) DO UPDATE SET
                    created_at_ms = excluded.created_at_ms,
                    source_ref = excluded.source_ref,
                    local_path = excluded.local_path,
                    sha1 = excluded.sha1,
                    size_bytes = excluded.size_bytes
                """,
                (mid, now_ms(), source, path, digest, size),
            )
            self._conn.commit()

    def get_media_path(self, media_id: str) -> str:
        mid = str(media_id or "").strip()
        if not mid:
            return ""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT local_path
                FROM media_assets
                WHERE media_id = ?
                LIMIT 1
                """,
                (mid,),
            ).fetchone()
        if row is None:
            return ""
        return str(row["local_path"] or "").strip()

    def _table_columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _migrate_legacy_schema(self) -> None:
        delivery_cols = self._table_columns("delivery_receipts")
        if ("session_key" not in delivery_cols) and ("session_id" in delivery_cols):
            self._conn.execute("ALTER TABLE delivery_receipts RENAME COLUMN session_id TO session_key")

        events_cols = self._table_columns("events")
        if "session_id" in events_cols:
            self._rebuild_events_without_session_id(events_cols=events_cols)

    def _rebuild_events_without_session_id(self, *, events_cols: set[str]) -> None:
        select_session_key = "session_key" if "session_key" in events_cols else "session_id"
        self._conn.execute("ALTER TABLE events RENAME TO events_legacy_session_id")
        self._conn.execute(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL,
                received_at_ms INTEGER NOT NULL,
                tenant_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                store_id TEXT NOT NULL,
                store_name TEXT NOT NULL,
                session_key TEXT NOT NULL,
                customer_id TEXT NOT NULL,
                platform_nickname TEXT NOT NULL,
                message_id TEXT NOT NULL,
                message_type TEXT NOT NULL,
                text TEXT NOT NULL,
                media_url TEXT NOT NULL,
                raw_json TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            f"""
            INSERT INTO events (
                id, trace_id, received_at_ms, tenant_id, platform, store_id, store_name,
                session_key, customer_id, platform_nickname, message_id,
                message_type, text, media_url, raw_json
            )
            SELECT
                id, trace_id, received_at_ms, tenant_id, platform, store_id, store_name,
                COALESCE(NULLIF({select_session_key}, ''), 'unknown'),
                customer_id, platform_nickname, message_id,
                message_type, text, media_url, raw_json
            FROM events_legacy_session_id
            """
        )
        self._conn.execute("DROP TABLE events_legacy_session_id")
        self._conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_events_trace_id ON events(trace_id);
            CREATE INDEX IF NOT EXISTS idx_events_session_key ON events(session_key);
            CREATE INDEX IF NOT EXISTS idx_events_received_at ON events(received_at_ms);
            CREATE INDEX IF NOT EXISTS idx_events_dedup ON events(tenant_id, platform, store_id, message_id);
            """
        )

    def _assert_strict_schema(self) -> None:
        columns = self._table_columns("events")
        if "platform_nickname" not in columns:
            raise RuntimeError(
                "events schema missing platform_nickname; delete data/clawbot.db and restart"
            )
        if "session_key" not in columns:
            raise RuntimeError(
                "events schema missing session_key; delete data/clawbot.db and restart"
            )
        if "customer_nickname" in columns:
            raise RuntimeError(
                "legacy events schema detected (customer_nickname); delete data/clawbot.db and restart"
            )

    def lookup_cached_decision(
        self,
        *,
        tenant_id: str,
        platform: str,
        store_id: str,
        message_id: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT d.action, d.reply_text, d.reason, d.category, d.confidence,
                       d.amount, d.need_more, d.manual, d.extra_json, d.trace_id
                FROM events e
                JOIN decisions d ON d.trace_id = e.trace_id
                WHERE e.tenant_id = ?
                  AND e.platform = ?
                  AND e.store_id = ?
                  AND e.message_id = ?
                ORDER BY e.id DESC
                LIMIT 1
                """,
                (tenant_id, platform, store_id, message_id),
            ).fetchone()
        if row is None:
            return None
        try:
            extra = json.loads(row["extra_json"] or "{}")
            if not isinstance(extra, dict):
                extra = {}
        except Exception:
            extra = {}
        return {
            "action": str(row["action"]),
            "reply_text": str(row["reply_text"]),
            "reason": str(row["reason"]),
            "category": str(row["category"]),
            "confidence": float(row["confidence"]),
            "amount": float(row["amount"]),
            "need_more": bool(row["need_more"]),
            "manual": bool(row["manual"]),
            "extra": extra,
            "trace_id": str(row["trace_id"]),
        }

    def insert_event_and_decision(
        self,
        trace_id: str,
        event: IncomingEvent,
        decision: DecisionInternal,
    ) -> None:
        received = now_ms()
        event_session_key = event.session_key()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO events (
                    trace_id, received_at_ms, tenant_id, platform, store_id, store_name,
                    session_key, customer_id, platform_nickname, message_id,
                    message_type, text, media_url, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    received,
                    event.tenant_id,
                    event.platform,
                    event.store_id,
                    event.store_name,
                    event_session_key,
                    event.customer_id,
                    event.platform_nickname,
                    event.message_id,
                    event.message_type,
                    event.text,
                    event.media_url,
                    json.dumps(event.raw, ensure_ascii=False),
                ),
            )
            self._conn.execute(
                """
                INSERT INTO decisions (
                    trace_id, created_at_ms, session_key, category, confidence,
                    action, reply_text, reason, amount, need_more, manual, extra_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    now_ms(),
                    event_session_key,
                    decision.category,
                    decision.confidence,
                    decision.action,
                    decision.reply_text,
                    decision.reason,
                    decision.amount,
                    1 if decision.need_more else 0,
                    1 if decision.manual else 0,
                    json.dumps(decision.extra, ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def record_delivery(
        self,
        *,
        tenant_id: str,
        platform: str,
        store_id: str,
        session_key: str,
        message_id: str,
        trace_id: str,
        status: str,
        sent_text: str,
        sent_image_urls: list[str] | None,
        reason: str,
        sent_at_ms: int | None = None,
    ) -> None:
        tenant = str(tenant_id or "").strip()
        plat = str(platform or "").strip()
        store_key = str(store_id or "").strip()
        sess = str(session_key or "").strip()
        mid = str(message_id or "").strip()
        if (not tenant) or (not plat) or (not store_key) or (not sess) or (not mid):
            return

        urls: list[str] = []
        for item in sent_image_urls or []:
            val = str(item or "").strip()
            if not val:
                continue
            if val in urls:
                continue
            urls.append(val)

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO delivery_receipts (
                    sent_at_ms, tenant_id, platform, store_id, session_key, message_id,
                    trace_id, status, sent_text, sent_image_urls_json, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, platform, store_id, message_id)
                DO UPDATE SET
                    sent_at_ms = excluded.sent_at_ms,
                    session_key = excluded.session_key,
                    trace_id = excluded.trace_id,
                    status = excluded.status,
                    sent_text = excluded.sent_text,
                    sent_image_urls_json = excluded.sent_image_urls_json,
                    reason = excluded.reason
                """,
                (
                    int(sent_at_ms or now_ms()),
                    tenant,
                    plat,
                    store_key,
                    sess,
                    mid,
                    str(trace_id or "").strip(),
                    str(status or "sent").strip() or "sent",
                    str(sent_text or "").strip(),
                    json.dumps(urls, ensure_ascii=False),
                    str(reason or "").strip(),
                ),
            )
            self._conn.commit()

    def summary(self, minutes: int = 60, include_wecom: bool = False) -> dict[str, Any]:
        cutoff = now_ms() - max(minutes, 1) * 60 * 1000
        where_platform = ""
        if not include_wecom:
            where_platform = " AND e.platform <> 'wecom' "
        with self._lock:
            total = self._conn.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM decisions d
                JOIN events e ON e.trace_id = d.trace_id
                WHERE d.created_at_ms >= ?
                {where_platform}
                """,
                (cutoff,),
            ).fetchone()["n"]

            manual = self._conn.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM decisions d
                JOIN events e ON e.trace_id = d.trace_id
                WHERE d.created_at_ms >= ?
                  AND d.manual = 1
                {where_platform}
                """,
                (cutoff,),
            ).fetchone()["n"]

            by_action_rows = self._conn.execute(
                f"""
                SELECT action, COUNT(*) AS n
                FROM decisions d
                JOIN events e ON e.trace_id = d.trace_id
                WHERE d.created_at_ms >= ?
                {where_platform}
                GROUP BY action
                ORDER BY n DESC
                """,
                (cutoff,),
            ).fetchall()

            by_category_rows = self._conn.execute(
                f"""
                SELECT category, COUNT(*) AS n
                FROM decisions d
                JOIN events e ON e.trace_id = d.trace_id
                WHERE d.created_at_ms >= ?
                {where_platform}
                GROUP BY category
                ORDER BY n DESC
                """,
                (cutoff,),
            ).fetchall()

            by_reason_rows = self._conn.execute(
                f"""
                SELECT reason, COUNT(*) AS n
                FROM decisions d
                JOIN events e ON e.trace_id = d.trace_id
                WHERE d.created_at_ms >= ?
                {where_platform}
                GROUP BY reason
                ORDER BY n DESC
                LIMIT 10
                """,
                (cutoff,),
            ).fetchall()

        return {
            "window_minutes": minutes,
            "total": int(total),
            "manual": int(manual),
            "include_wecom": bool(include_wecom),
            "by_action": {row["action"]: int(row["n"]) for row in by_action_rows},
            "by_category": {row["category"]: int(row["n"]) for row in by_category_rows},
            "top_reasons": [
                {"reason": row["reason"], "count": int(row["n"])} for row in by_reason_rows
            ],
        }

    def record_wecom_send(self, member_id: str) -> None:
        member = str(member_id or "").strip()
        if not member:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO wecom_send_log (created_at_ms, member_id)
                VALUES (?, ?)
                """,
                (now_ms(), member),
            )
            self._conn.commit()

    def wecom_usage(self, *, member_id: str = "", minutes: int = 60) -> dict[str, Any]:
        window_minutes = max(1, min(int(minutes), 24 * 60))
        cutoff_window = now_ms() - window_minutes * 60 * 1000
        cutoff_1m = now_ms() - 60 * 1000
        cutoff_60m = now_ms() - 60 * 60 * 1000
        member = str(member_id or "").strip()
        with self._lock:
            has_send_log = self._conn.execute(
                "SELECT 1 AS ok FROM wecom_send_log LIMIT 1"
            ).fetchone() is not None

        if has_send_log:
            where_member = ""
            args_member: tuple[Any, ...] = ()
            if member:
                where_member = " AND member_id = ? "
                args_member = (member,)
            with self._lock:
                sent_1m_row = self._conn.execute(
                    f"""
                    SELECT COUNT(*) AS n
                    FROM wecom_send_log
                    WHERE created_at_ms >= ?
                    {where_member}
                    """,
                    (cutoff_1m, *args_member),
                ).fetchone()

                sent_60m_row = self._conn.execute(
                    f"""
                    SELECT COUNT(*) AS n
                    FROM wecom_send_log
                    WHERE created_at_ms >= ?
                    {where_member}
                    """,
                    (cutoff_60m, *args_member),
                ).fetchone()

                sent_window_row = self._conn.execute(
                    f"""
                    SELECT COUNT(*) AS n
                    FROM wecom_send_log
                    WHERE created_at_ms >= ?
                    {where_member}
                    """,
                    (cutoff_window, *args_member),
                ).fetchone()

                top_rows = self._conn.execute(
                    """
                    SELECT member_id, COUNT(*) AS n
                    FROM wecom_send_log
                    WHERE created_at_ms >= ?
                    GROUP BY member_id
                    ORDER BY n DESC
                    LIMIT 10
                    """,
                    (cutoff_window,),
                ).fetchall()
        else:
            where_member = ""
            args_member = ()
            if member:
                where_member = " AND e.customer_id = ? "
                args_member = (member,)
            with self._lock:
                sent_1m_row = self._conn.execute(
                    f"""
                    SELECT COUNT(*) AS n
                    FROM decisions d
                    JOIN events e ON e.trace_id = d.trace_id
                    WHERE e.platform = 'wecom'
                      AND d.action = 'send'
                      AND d.created_at_ms >= ?
                      {where_member}
                    """,
                    (cutoff_1m, *args_member),
                ).fetchone()

                sent_60m_row = self._conn.execute(
                    f"""
                    SELECT COUNT(*) AS n
                    FROM decisions d
                    JOIN events e ON e.trace_id = d.trace_id
                    WHERE e.platform = 'wecom'
                      AND d.action = 'send'
                      AND d.created_at_ms >= ?
                      {where_member}
                    """,
                    (cutoff_60m, *args_member),
                ).fetchone()

                sent_window_row = self._conn.execute(
                    f"""
                    SELECT COUNT(*) AS n
                    FROM decisions d
                    JOIN events e ON e.trace_id = d.trace_id
                    WHERE e.platform = 'wecom'
                      AND d.action = 'send'
                      AND d.created_at_ms >= ?
                      {where_member}
                    """,
                    (cutoff_window, *args_member),
                ).fetchone()

                top_rows = self._conn.execute(
                    """
                    SELECT e.customer_id, COUNT(*) AS n
                    FROM decisions d
                    JOIN events e ON e.trace_id = d.trace_id
                    WHERE e.platform = 'wecom'
                      AND d.action = 'send'
                      AND d.created_at_ms >= ?
                    GROUP BY e.customer_id
                    ORDER BY n DESC
                    LIMIT 10
                    """,
                    (cutoff_window,),
                ).fetchall()

        sent_1m = int(sent_1m_row["n"] if sent_1m_row else 0)
        sent_60m = int(sent_60m_row["n"] if sent_60m_row else 0)
        sent_window = int(sent_window_row["n"] if sent_window_row else 0)

        return {
            "platform": "wecom",
            "member_id": member,
            "window_minutes": window_minutes,
            "sent_count_window": sent_window,
            "sent_count_1m": sent_1m,
            "sent_count_60m": sent_60m,
            "limits": {
                "per_member_1m": 30,
                "per_member_60m": 1000,
            },
            "estimated_usage_ratio": {
                "ratio_1m": round((sent_1m / 30.0), 4),
                "ratio_60m": round((sent_60m / 1000.0), 4),
            },
            "top_members_window": [
                {
                    "member_id": str(row["member_id"] if has_send_log else row["customer_id"] or ""),
                    "sent_count": int(row["n"]),
                }
                for row in top_rows
            ],
        }

    def trace(self, trace_id: str) -> dict[str, Any] | None:
        with self._lock:
            event_row = self._conn.execute(
                """
                SELECT * FROM events WHERE trace_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (trace_id,),
            ).fetchone()
            decision_row = self._conn.execute(
                """
                SELECT * FROM decisions WHERE trace_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (trace_id,),
            ).fetchone()

        if not event_row and not decision_row:
            return None

        payload: dict[str, Any] = {"trace_id": trace_id}
        if event_row:
            payload["event"] = {
                "platform": event_row["platform"],
                "store_id": event_row["store_id"],
                "session_key": event_row["session_key"],
                "platform_nickname": event_row["platform_nickname"],
                "message_type": event_row["message_type"],
                "text": event_row["text"],
                "media_url": event_row["media_url"],
                "received_at_ms": event_row["received_at_ms"],
            }

        if decision_row:
            payload["decision"] = {
                "action": decision_row["action"],
                "category": decision_row["category"],
                "confidence": decision_row["confidence"],
                "reason": decision_row["reason"],
                "amount": decision_row["amount"],
                "need_more": bool(decision_row["need_more"]),
                "manual": bool(decision_row["manual"]),
                "created_at_ms": decision_row["created_at_ms"],
            }

        return payload

    def window_metrics(
        self,
        *,
        start_ms: int,
        end_ms: int,
        include_wecom: bool = False,
    ) -> dict[str, Any]:
        start = int(start_ms)
        end = int(end_ms)
        if end <= start:
            end = start + 1
        where_platform = ""
        if not include_wecom:
            where_platform = " AND e.platform <> 'wecom' "

        with self._lock:
            event_row = self._conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total_messages,
                    COUNT(DISTINCT e.session_key) AS unique_sessions,
                    COUNT(DISTINCT CASE
                        WHEN e.customer_id <> '' THEN e.platform || ':' || e.customer_id
                        ELSE e.session_key
                    END) AS unique_customers
                FROM events e
                WHERE e.received_at_ms >= ?
                  AND e.received_at_ms < ?
                  {where_platform}
                """,
                (start, end),
            ).fetchone()

            decision_row = self._conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total_decisions,
                    SUM(CASE WHEN d.manual = 1 THEN 1 ELSE 0 END) AS manual_count
                FROM decisions d
                JOIN events e ON e.trace_id = d.trace_id
                WHERE d.created_at_ms >= ?
                  AND d.created_at_ms < ?
                  {where_platform}
                """,
                (start, end),
            ).fetchone()

            category_rows = self._conn.execute(
                f"""
                SELECT d.category, COUNT(*) AS n
                FROM decisions d
                JOIN events e ON e.trace_id = d.trace_id
                WHERE d.created_at_ms >= ?
                  AND d.created_at_ms < ?
                  {where_platform}
                GROUP BY d.category
                ORDER BY n DESC
                """,
                (start, end),
            ).fetchall()

        return {
            "start_ms": start,
            "end_ms": end,
            "include_wecom": bool(include_wecom),
            "total_messages": int(event_row["total_messages"] or 0),
            "unique_sessions": int(event_row["unique_sessions"] or 0),
            "unique_customers": int(event_row["unique_customers"] or 0),
            "total_decisions": int(decision_row["total_decisions"] or 0),
            "manual_count": int(decision_row["manual_count"] or 0),
            "by_category": {str(row["category"]): int(row["n"]) for row in category_rows},
        }

    def recent_sessions(self, limit: int = 20, include_wecom: bool = False) -> list[dict[str, Any]]:
        capped = max(1, min(limit, 200))
        where_platform = ""
        if not include_wecom:
            where_platform = " WHERE e.platform <> 'wecom' "
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT e.session_key, e.platform_nickname, e.store_id, e.store_name, e.text, e.media_url,
                       d.action, d.category,
                       CASE
                           WHEN e.platform = 'wecom' THEN COALESCE(NULLIF(dr.reason, ''), d.reason)
                           ELSE COALESCE(dr.reason, '')
                       END AS reason,
                       d.created_at_ms, d.trace_id
                FROM decisions d
                JOIN events e ON e.trace_id = d.trace_id
                LEFT JOIN (
                    SELECT r.tenant_id, r.platform, r.store_id, r.message_id, r.reason, r.sent_at_ms
                    FROM delivery_receipts r
                    JOIN (
                        SELECT tenant_id, platform, store_id, message_id, MAX(sent_at_ms) AS max_sent_at_ms
                        FROM delivery_receipts
                        GROUP BY tenant_id, platform, store_id, message_id
                    ) m
                    ON r.tenant_id = m.tenant_id
                       AND r.platform = m.platform
                       AND r.store_id = m.store_id
                       AND r.message_id = m.message_id
                       AND r.sent_at_ms = m.max_sent_at_ms
                ) dr
                ON dr.tenant_id = e.tenant_id
                   AND dr.platform = e.platform
                   AND dr.store_id = e.store_id
                   AND dr.message_id = e.message_id
                {where_platform}
                ORDER BY d.id DESC
                LIMIT ?
                """,
                (capped,),
            ).fetchall()

        return [
            {
                "trace_id": row["trace_id"],
                "session_key": row["session_key"],
                "store_id": row["store_id"],
                "store_name": row["store_name"],
                "platform_nickname": row["platform_nickname"],
                "text": row["text"],
                "media_url": row["media_url"],
                "action": row["action"],
                "category": row["category"],
                "reason": row["reason"],
                "created_at_ms": row["created_at_ms"],
            }
            for row in rows
        ]

    def recent_dialogue(self, session_key: str, limit: int = 8) -> list[dict[str, Any]]:
        capped = max(1, min(limit, 50))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    e.platform,
                    e.tenant_id,
                    e.store_id,
                    e.message_id,
                    e.text,
                    e.media_url,
                    COALESCE(dr.sent_text, '') AS sent_text,
                    COALESCE(dr.reason, '') AS sent_reason,
                    COALESCE(d.reply_text, '') AS decision_reply_text,
                    COALESCE(d.reason, '') AS decision_reason,
                    d.created_at_ms
                FROM events e
                LEFT JOIN decisions d ON d.trace_id = e.trace_id
                LEFT JOIN (
                    SELECT r.tenant_id, r.platform, r.store_id, r.message_id, r.sent_text, r.reason, r.sent_at_ms
                    FROM delivery_receipts r
                    JOIN (
                        SELECT tenant_id, platform, store_id, message_id, MAX(sent_at_ms) AS max_sent_at_ms
                        FROM delivery_receipts
                        GROUP BY tenant_id, platform, store_id, message_id
                    ) m
                    ON r.tenant_id = m.tenant_id
                       AND r.platform = m.platform
                       AND r.store_id = m.store_id
                       AND r.message_id = m.message_id
                       AND r.sent_at_ms = m.max_sent_at_ms
                ) dr
                ON dr.tenant_id = e.tenant_id
                   AND dr.platform = e.platform
                   AND dr.store_id = e.store_id
                   AND dr.message_id = e.message_id
                WHERE e.session_key = ?
                ORDER BY e.id DESC
                LIMIT ?
                """,
                (session_key, capped),
            ).fetchall()

        dialogue: list[dict[str, Any]] = []
        for row in reversed(rows):
            customer_text = str(row["text"] or "").strip()
            media_url = str(row["media_url"] or "").strip()
            if not customer_text and media_url:
                customer_text = f"[图片消息 URL={media_url}]"
            if str(row["platform"] or "").strip() == "wecom":
                agent_text = (
                    str(row["sent_text"] or "").strip()
                    or str(row["decision_reply_text"] or "").strip()
                )
                reason = (
                    str(row["sent_reason"] or "").strip()
                    or str(row["decision_reason"] or "").strip()
                )
            else:
                # Customer history must use actual delivered replies only.
                agent_text = str(row["sent_text"] or "").strip()
                reason = str(row["sent_reason"] or "").strip()
            dialogue.append(
                {
                    "customer": customer_text,
                    "agent": agent_text,
                    "reason": reason,
                    "created_at_ms": int(row["created_at_ms"] or 0),
                }
            )
        return dialogue

    def dialogue_since(
        self,
        session_key: str,
        *,
        start_ms: int,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """
        Return dialogue events for a session since start_ms.

        Ordered by message time ascending. If records exceed limit, newest records are kept.
        """
        capped = max(1, min(limit, 5000))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    e.platform,
                    e.tenant_id,
                    e.store_id,
                    e.message_id,
                    e.text,
                    e.media_url,
                    COALESCE(dr.sent_text, '') AS sent_text,
                    COALESCE(dr.reason, '') AS sent_reason,
                    COALESCE(d.reply_text, '') AS decision_reply_text,
                    COALESCE(d.reason, '') AS decision_reason,
                    e.received_at_ms
                FROM events e
                LEFT JOIN decisions d ON d.trace_id = e.trace_id
                LEFT JOIN (
                    SELECT r.tenant_id, r.platform, r.store_id, r.message_id, r.sent_text, r.reason, r.sent_at_ms
                    FROM delivery_receipts r
                    JOIN (
                        SELECT tenant_id, platform, store_id, message_id, MAX(sent_at_ms) AS max_sent_at_ms
                        FROM delivery_receipts
                        GROUP BY tenant_id, platform, store_id, message_id
                    ) m
                    ON r.tenant_id = m.tenant_id
                       AND r.platform = m.platform
                       AND r.store_id = m.store_id
                       AND r.message_id = m.message_id
                       AND r.sent_at_ms = m.max_sent_at_ms
                ) dr
                ON dr.tenant_id = e.tenant_id
                   AND dr.platform = e.platform
                   AND dr.store_id = e.store_id
                   AND dr.message_id = e.message_id
                WHERE e.session_key = ?
                  AND e.received_at_ms >= ?
                ORDER BY e.id DESC
                LIMIT ?
                """,
                (session_key, int(start_ms), capped),
            ).fetchall()

        dialogue: list[dict[str, Any]] = []
        for row in reversed(rows):
            customer_text = str(row["text"] or "").strip()
            media_url = str(row["media_url"] or "").strip()
            if not customer_text and media_url:
                customer_text = f"[图片消息 URL={media_url}]"
            if str(row["platform"] or "").strip() == "wecom":
                agent_text = (
                    str(row["sent_text"] or "").strip()
                    or str(row["decision_reply_text"] or "").strip()
                )
                reason = (
                    str(row["sent_reason"] or "").strip()
                    or str(row["decision_reason"] or "").strip()
                )
            else:
                agent_text = str(row["sent_text"] or "").strip()
                reason = str(row["sent_reason"] or "").strip()
            dialogue.append(
                {
                    "customer": customer_text,
                    "agent": agent_text,
                    "reason": reason,
                    "created_at_ms": int(row["received_at_ms"] or 0),
                }
            )
        return dialogue

    def query_events(
        self,
        *,
        platform: str = "",
        store_id: str = "",
        store_name: str = "",
        platform_nickname: str = "",
        session_key: str = "",
        include_wecom: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        capped = max(1, min(limit, 500))
        clauses: list[str] = []
        params: list[Any] = []

        if session_key:
            clauses.append("e.session_key = ?")
            params.append(session_key)
        if platform:
            clauses.append("e.platform = ?")
            params.append(platform)
        if store_id:
            clauses.append("e.store_id = ?")
            params.append(store_id)
        if store_name:
            clauses.append("e.store_name = ?")
            params.append(store_name)
        if platform_nickname:
            clauses.append("e.platform_nickname = ?")
            params.append(platform_nickname)
        if not include_wecom:
            clauses.append("e.platform <> 'wecom'")

        where_sql = ""
        if clauses:
            where_sql = "WHERE " + " AND ".join(clauses)

        sql = f"""
            SELECT
                e.trace_id,
                e.session_key,
                e.platform,
                e.store_id,
                e.store_name,
                e.platform_nickname,
                e.message_type,
                e.text,
                e.media_url,
                e.received_at_ms,
                CASE
                    WHEN e.platform = 'wecom'
                        THEN COALESCE(NULLIF(dr.sent_text, ''), d.reply_text, '')
                    ELSE COALESCE(dr.sent_text, '')
                END AS reply_text,
                CASE
                    WHEN e.platform = 'wecom'
                        THEN COALESCE(NULLIF(dr.reason, ''), d.reason, '')
                    ELSE COALESCE(dr.reason, '')
                END AS reason
            FROM events e
            LEFT JOIN decisions d ON d.trace_id = e.trace_id
            LEFT JOIN (
                SELECT r.tenant_id, r.platform, r.store_id, r.message_id, r.sent_text, r.reason, r.sent_at_ms
                FROM delivery_receipts r
                JOIN (
                    SELECT tenant_id, platform, store_id, message_id, MAX(sent_at_ms) AS max_sent_at_ms
                    FROM delivery_receipts
                    GROUP BY tenant_id, platform, store_id, message_id
                ) m
                ON r.tenant_id = m.tenant_id
                   AND r.platform = m.platform
                   AND r.store_id = m.store_id
                   AND r.message_id = m.message_id
                   AND r.sent_at_ms = m.max_sent_at_ms
            ) dr
            ON dr.tenant_id = e.tenant_id
               AND dr.platform = e.platform
               AND dr.store_id = e.store_id
               AND dr.message_id = e.message_id
            {where_sql}
            ORDER BY e.id DESC
            LIMIT ?
        """
        params.append(capped)

        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()

        return [
            {
                "trace_id": row["trace_id"],
                "session_key": row["session_key"],
                "platform": row["platform"],
                "store_id": row["store_id"],
                "store_name": row["store_name"],
                "platform_nickname": row["platform_nickname"],
                "message_type": row["message_type"],
                "text": row["text"],
                "media_url": row["media_url"],
                "received_at_ms": row["received_at_ms"],
                "reply_text": row["reply_text"],
                "reason": row["reason"],
            }
            for row in rows
        ]
