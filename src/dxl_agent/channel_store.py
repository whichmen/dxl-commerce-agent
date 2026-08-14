from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .domain import ConflictError, scoped_key
from .storage import canonical_hash


def _now() -> str:
    return datetime.now(UTC).isoformat()


def outbound_payload_hash(
    *,
    outbox_key: str,
    connector_id: str,
    conversation_id: str,
    body: str,
    idempotency_key: str,
) -> str:
    return canonical_hash(
        {
            "outbox_key": outbox_key,
            "connector_id": connector_id,
            "conversation_id": conversation_id,
            "body": body,
            "idempotency_key": idempotency_key,
        }
    )


@dataclass(frozen=True, slots=True)
class InboxRecord:
    event_key: str
    connector_id: str
    external_event_id: str
    tenant_id: str
    channel: str
    store_id: str
    conversation_id: str
    customer_id: str
    message_id: str
    text: str
    attachments: tuple[str, ...]
    received_at: str
    binding_fingerprint: str
    payload_fingerprint: str
    supports_native_idempotency: bool
    supports_receipt_lookup: bool

    @property
    def session_key(self) -> str:
        return scoped_key(
            "session",
            self.tenant_id,
            self.channel,
            self.store_id,
            self.customer_id,
        )


@dataclass(frozen=True, slots=True)
class InboxLease(InboxRecord):
    claim_token: str
    attempt_count: int


@dataclass(frozen=True, slots=True)
class OutboxLease:
    outbox_key: str
    connector_id: str
    event_key: str
    session_key: str
    tenant_id: str
    channel: str
    store_id: str
    conversation_id: str
    customer_id: str
    body: str
    idempotency_key: str
    payload_hash: str
    binding_fingerprint: str
    supports_native_idempotency: bool
    supports_receipt_lookup: bool
    message_kind: str
    session_generation: int
    claim_token: str
    attempt_count: int


class ChannelStore:
    """Durable synthetic connector inbox/outbox with fenced SQLite leases.

    This store deliberately knows nothing about a platform DOM, device protocol,
    account, or credential. Connectors normalize those details before this trust
    boundary. The demo may share a database file with ``SQLiteStore`` while using
    an independent connection.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._session_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self.connection:
            self.connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA busy_timeout=5000;
                CREATE TABLE IF NOT EXISTS connector_cursors (
                    connector_id TEXT PRIMARY KEY,
                    cursor TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    connector_id TEXT NOT NULL,
                    external_event_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    attachments_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    binding_fingerprint TEXT NOT NULL,
                    payload_fingerprint TEXT NOT NULL,
                    supports_native_idempotency INTEGER NOT NULL,
                    supports_receipt_lookup INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    claim_token TEXT,
                    claim_expires_at REAL,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(connector_id, external_event_id)
                );
                CREATE INDEX IF NOT EXISTS channel_inbox_ready_idx
                    ON channel_inbox(state, next_attempt_at, created_at);
                CREATE TABLE IF NOT EXISTS channel_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    outbox_key TEXT NOT NULL UNIQUE,
                    connector_id TEXT NOT NULL,
                    event_key TEXT NOT NULL UNIQUE,
                    session_key TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_hash TEXT NOT NULL,
                    binding_fingerprint TEXT NOT NULL,
                    supports_native_idempotency INTEGER NOT NULL,
                    supports_receipt_lookup INTEGER NOT NULL,
                    message_kind TEXT NOT NULL,
                    session_generation INTEGER NOT NULL,
                    decision_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    claim_token TEXT,
                    claim_expires_at REAL,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    remote_receipt TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS channel_outbox_ready_idx
                    ON channel_outbox(state, next_attempt_at, created_at);
                CREATE TABLE IF NOT EXISTS session_controls (
                    session_key TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS handoff_events (
                    handoff_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def cursor(self, connector_id: str) -> str | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT cursor FROM connector_cursors WHERE connector_id = ?",
                (connector_id,),
            ).fetchone()
        return str(row["cursor"]) if row else None

    def session_fence(self, session_key: str) -> asyncio.Lock:
        """Serialize local decisions, handoff changes, and sends for one session."""

        return self._session_locks[session_key]

    def record_poll(
        self,
        *,
        connector_id: str,
        expected_cursor: str | None,
        next_cursor: str,
        records: Sequence[InboxRecord],
    ) -> int:
        """Atomically persist normalized events and advance an opaque cursor."""

        timestamp = _now()
        inserted = 0
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                current_row = self.connection.execute(
                    "SELECT cursor FROM connector_cursors WHERE connector_id = ?",
                    (connector_id,),
                ).fetchone()
                current_cursor = str(current_row["cursor"]) if current_row else None
                if current_cursor != expected_cursor:
                    raise ConflictError("Connector cursor changed while a poll was in flight")
                for record in records:
                    if record.connector_id != connector_id:
                        raise ValueError("A connector cannot enqueue an event for another binding")
                    cursor = self.connection.execute(
                        """
                        INSERT OR IGNORE INTO channel_inbox(
                            event_key, connector_id, external_event_id, tenant_id,
                            channel, store_id, conversation_id, customer_id,
                            message_id, text, attachments_json, received_at,
                            session_key, binding_fingerprint, payload_fingerprint,
                            supports_native_idempotency, supports_receipt_lookup,
                            state, created_at, updated_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            'ready', ?, ?
                        )
                        """,
                        (
                            record.event_key,
                            record.connector_id,
                            record.external_event_id,
                            record.tenant_id,
                            record.channel,
                            record.store_id,
                            record.conversation_id,
                            record.customer_id,
                            record.message_id,
                            record.text,
                            json.dumps(record.attachments, ensure_ascii=False),
                            record.received_at,
                            record.session_key,
                            record.binding_fingerprint,
                            record.payload_fingerprint,
                            int(record.supports_native_idempotency),
                            int(record.supports_receipt_lookup),
                            timestamp,
                            timestamp,
                        ),
                    )
                    if cursor.rowcount == 0:
                        existing = self.connection.execute(
                            """
                            SELECT payload_fingerprint FROM channel_inbox
                            WHERE connector_id = ? AND external_event_id = ?
                            """,
                            (record.connector_id, record.external_event_id),
                        ).fetchone()
                        if (
                            existing is None
                            or existing["payload_fingerprint"] != record.payload_fingerprint
                        ):
                            raise ConflictError(
                                "Duplicate connector event changed its normalized payload"
                            )
                    inserted += cursor.rowcount
                self.connection.execute(
                    """
                    INSERT INTO connector_cursors(connector_id, cursor, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(connector_id) DO UPDATE SET
                        cursor = excluded.cursor,
                        updated_at = excluded.updated_at
                    """,
                    (connector_id, next_cursor, timestamp),
                )
                self.connection.commit()
                return inserted
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def claim_inbox(self, *, lease_seconds: float = 60.0) -> InboxLease | None:
        now = time.time()
        token = uuid.uuid4().hex
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    """
                    SELECT candidate.* FROM channel_inbox AS candidate
                    WHERE candidate.next_attempt_at <= ?
                      AND (candidate.state = 'ready' OR (
                        candidate.state = 'processing'
                        AND COALESCE(candidate.claim_expires_at, 0) <= ?
                      ))
                      AND NOT EXISTS (
                        SELECT 1 FROM channel_inbox AS earlier
                        WHERE earlier.session_key = candidate.session_key
                          AND earlier.id < candidate.id
                          AND earlier.state IN ('ready', 'processing')
                      )
                    ORDER BY candidate.id
                    LIMIT 1
                    """,
                    (now, now),
                ).fetchone()
                if row is None:
                    self.connection.commit()
                    return None
                cursor = self.connection.execute(
                    """
                    UPDATE channel_inbox
                    SET state = 'processing', claim_token = ?, claim_expires_at = ?,
                        attempt_count = attempt_count + 1, updated_at = ?
                    WHERE event_key = ? AND (
                        state = 'ready' OR (
                            state = 'processing' AND COALESCE(claim_expires_at, 0) <= ?
                        )
                    )
                    """,
                    (token, now + lease_seconds, _now(), row["event_key"], now),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("Inbox lease was claimed concurrently")
                claimed = self.connection.execute(
                    "SELECT * FROM channel_inbox WHERE event_key = ?",
                    (row["event_key"],),
                ).fetchone()
                self.connection.commit()
                if claimed is None:
                    raise RuntimeError("Claimed inbox event disappeared")
                return self._decode_inbox(claimed, token)
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def renew_inbox(self, lease: InboxLease, *, lease_seconds: float = 60.0) -> None:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE channel_inbox
                SET claim_expires_at = ?, updated_at = ?
                WHERE event_key = ? AND state = 'processing' AND claim_token = ?
                """,
                (time.time() + lease_seconds, _now(), lease.event_key, lease.claim_token),
            )
        if cursor.rowcount != 1:
            raise ConflictError("Inbox lease was lost before renewal")

    def complete_with_reply(
        self,
        lease: InboxLease,
        *,
        reply: str,
        decision: dict[str, Any],
    ) -> str | None:
        outbox_key = scoped_key("outbox", lease.connector_id, lease.event_key)
        idempotency_key = scoped_key("delivery", lease.connector_id, lease.event_key)
        timestamp = _now()
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                control = self.connection.execute(
                    "SELECT state, generation FROM session_controls WHERE session_key = ?",
                    (lease.session_key,),
                ).fetchone()
                if control is not None and control["state"] == "human_active":
                    parked = self.connection.execute(
                        """
                        UPDATE channel_inbox
                        SET state = 'parked', claim_token = NULL, claim_expires_at = NULL,
                            updated_at = ?
                        WHERE event_key = ? AND state = 'processing' AND claim_token = ?
                        """,
                        (timestamp, lease.event_key, lease.claim_token),
                    )
                    if parked.rowcount != 1:
                        raise ConflictError("Inbox lease was lost before handoff parking")
                    self.connection.commit()
                    return None
                generation = int(control["generation"]) if control else 0
                cursor = self.connection.execute(
                    """
                    UPDATE channel_inbox
                    SET state = 'completed', claim_token = NULL, claim_expires_at = NULL,
                        last_error = NULL, updated_at = ?
                    WHERE event_key = ? AND state = 'processing' AND claim_token = ?
                    """,
                    (timestamp, lease.event_key, lease.claim_token),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("Inbox lease was lost before completion")
                payload_hash = outbound_payload_hash(
                    outbox_key=outbox_key,
                    connector_id=lease.connector_id,
                    conversation_id=lease.conversation_id,
                    body=reply,
                    idempotency_key=idempotency_key,
                )
                inserted = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO channel_outbox(
                        outbox_key, connector_id, event_key, session_key,
                        tenant_id, channel, store_id, conversation_id, customer_id,
                        body, idempotency_key, payload_hash, binding_fingerprint,
                        supports_native_idempotency, supports_receipt_lookup,
                        message_kind, session_generation, decision_json, state,
                        created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'ready', ?, ?
                    )
                    """,
                    (
                        outbox_key,
                        lease.connector_id,
                        lease.event_key,
                        lease.session_key,
                        lease.tenant_id,
                        lease.channel,
                        lease.store_id,
                        lease.conversation_id,
                        lease.customer_id,
                        reply,
                        idempotency_key,
                        payload_hash,
                        lease.binding_fingerprint,
                        int(lease.supports_native_idempotency),
                        int(lease.supports_receipt_lookup),
                        "auto_reply",
                        generation,
                        json.dumps(decision, ensure_ascii=False),
                        timestamp,
                        timestamp,
                    ),
                )
                if inserted.rowcount == 0:
                    existing = self.connection.execute(
                        "SELECT payload_hash FROM channel_outbox WHERE outbox_key = ?",
                        (outbox_key,),
                    ).fetchone()
                    if existing is None or existing["payload_hash"] != payload_hash:
                        raise ConflictError("Outbox key was reused with a different payload")
                self.connection.commit()
                return outbox_key
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def requeue_inbox(
        self,
        lease: InboxLease,
        *,
        reason: str,
        retry_delay_seconds: float = 0,
        max_attempts: int = 3,
    ) -> None:
        state = "dead" if lease.attempt_count >= max_attempts else "ready"
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE channel_inbox
                SET state = ?, claim_token = NULL, claim_expires_at = NULL,
                    next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE event_key = ? AND state = 'processing' AND claim_token = ?
                """,
                (
                    state,
                    time.time() + retry_delay_seconds,
                    reason[:300],
                    _now(),
                    lease.event_key,
                    lease.claim_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Inbox lease was lost before retry")
            if state == "dead":
                self._activate_handoff(
                    lease.session_key,
                    lease.event_key,
                    "decision_retry_exhausted",
                    {"attempt_count": lease.attempt_count},
                )

    def handoff_with_ack(self, lease: InboxLease, *, reason: str, acknowledgement: str) -> str:
        """Move a session to human control and enqueue one deterministic acknowledgement."""

        outbox_key = scoped_key("outbox", lease.connector_id, lease.event_key)
        idempotency_key = scoped_key("delivery", lease.connector_id, lease.event_key)
        timestamp = _now()
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                cursor = self.connection.execute(
                    """
                    UPDATE channel_inbox
                    SET state = 'completed', claim_token = NULL, claim_expires_at = NULL,
                        updated_at = ?
                    WHERE event_key = ? AND state = 'processing' AND claim_token = ?
                    """,
                    (timestamp, lease.event_key, lease.claim_token),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("Inbox lease was lost before handoff")
                generation = self._activate_handoff(
                    lease.session_key,
                    lease.event_key,
                    reason,
                    {},
                )
                payload_hash = outbound_payload_hash(
                    outbox_key=outbox_key,
                    connector_id=lease.connector_id,
                    conversation_id=lease.conversation_id,
                    body=acknowledgement,
                    idempotency_key=idempotency_key,
                )
                inserted = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO channel_outbox(
                        outbox_key, connector_id, event_key, session_key,
                        tenant_id, channel, store_id, conversation_id, customer_id,
                        body, idempotency_key, payload_hash, binding_fingerprint,
                        supports_native_idempotency, supports_receipt_lookup,
                        message_kind, session_generation, decision_json, state,
                        created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'ready', ?, ?
                    )
                    """,
                    (
                        outbox_key,
                        lease.connector_id,
                        lease.event_key,
                        lease.session_key,
                        lease.tenant_id,
                        lease.channel,
                        lease.store_id,
                        lease.conversation_id,
                        lease.customer_id,
                        acknowledgement,
                        idempotency_key,
                        payload_hash,
                        lease.binding_fingerprint,
                        int(lease.supports_native_idempotency),
                        int(lease.supports_receipt_lookup),
                        "handoff_ack",
                        generation,
                        json.dumps({"status": "human_handoff"}, ensure_ascii=False),
                        timestamp,
                        timestamp,
                    ),
                )
                if inserted.rowcount == 0:
                    existing = self.connection.execute(
                        "SELECT payload_hash FROM channel_outbox WHERE outbox_key = ?",
                        (outbox_key,),
                    ).fetchone()
                    if existing is None or existing["payload_hash"] != payload_hash:
                        raise ConflictError("Handoff outbox key changed its payload")
                self.connection.commit()
                return outbox_key
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def park_for_human(self, lease: InboxLease) -> None:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE channel_inbox
                SET state = 'parked', claim_token = NULL, claim_expires_at = NULL,
                    updated_at = ?
                WHERE event_key = ? AND state = 'processing' AND claim_token = ?
                """,
                (_now(), lease.event_key, lease.claim_token),
            )
        if cursor.rowcount != 1:
            raise ConflictError("Inbox lease was lost before parking")

    def session_state(self, session_key: str) -> str:
        with self._lock:
            row = self.connection.execute(
                "SELECT state FROM session_controls WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        return str(row["state"]) if row else "bot"

    def release_handoff(self, session_key: str) -> None:
        timestamp = _now()
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                unknown = self.connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM channel_outbox
                    WHERE session_key = ? AND state = 'unknown'
                    """,
                    (session_key,),
                ).fetchone()
                if unknown is not None and int(unknown["count"]) > 0:
                    raise ConflictError(
                        "Ambiguous deliveries must be resolved before automation resumes"
                    )
                control = self.connection.execute(
                    "SELECT state, generation FROM session_controls WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
                if control is None or control["state"] != "human_active":
                    self.connection.commit()
                    return
                in_flight = self.connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM channel_outbox
                    WHERE session_key = ? AND state IN ('delivering', 'unknown')
                    """,
                    (session_key,),
                ).fetchone()
                if in_flight is not None and int(in_flight["count"]) > 0:
                    raise ConflictError(
                        "Delivering or ambiguous replies must settle before automation resumes"
                    )
                generation = (int(control["generation"]) if control else 0) + 1
                transitioned = self.connection.execute(
                    """
                    UPDATE session_controls
                    SET state = 'bot', reason = 'operator_released',
                        generation = ?, updated_at = ?
                    WHERE session_key = ? AND state = 'human_active'
                      AND generation = ?
                    """,
                    (generation, timestamp, session_key, int(control["generation"])),
                )
                if transitioned.rowcount != 1:
                    raise ConflictError("Handoff generation changed before release")
                self.connection.execute(
                    """
                    UPDATE channel_outbox
                    SET state = 'cancelled', last_error = 'handoff_released_before_ack',
                        updated_at = ?
                    WHERE session_key = ? AND state = 'ready'
                      AND message_kind = 'handoff_ack'
                    """,
                    (timestamp, session_key),
                )
                self.connection.execute(
                    """
                    UPDATE channel_inbox
                    SET state = 'ready', next_attempt_at = 0, updated_at = ?
                    WHERE session_key = ? AND state = 'parked'
                    """,
                    (timestamp, session_key),
                )
                self.connection.commit()
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def claim_outbox(self, *, lease_seconds: float = 60.0) -> OutboxLease | None:
        now = time.time()
        token = uuid.uuid4().hex
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                unsafe_expired = self.connection.execute(
                    """
                    SELECT o.outbox_key, o.session_key, o.event_key
                    FROM channel_outbox AS o
                    LEFT JOIN session_controls AS s ON s.session_key = o.session_key
                    WHERE o.state = 'delivering'
                      AND COALESCE(o.claim_expires_at, 0) <= ?
                      AND (
                        o.supports_native_idempotency = 0
                        OR COALESCE(s.generation, 0) != o.session_generation
                        OR (o.message_kind = 'auto_reply'
                            AND COALESCE(s.state, 'bot') != 'bot')
                        OR (o.message_kind = 'handoff_ack'
                            AND COALESCE(s.state, 'bot') != 'human_active')
                      )
                    """,
                    (now,),
                ).fetchall()
                for expired in unsafe_expired:
                    transitioned = self.connection.execute(
                        """
                        UPDATE channel_outbox
                        SET state = 'unknown', claim_token = NULL,
                            claim_expires_at = NULL,
                            last_error = 'expired_non_idempotent_delivery', updated_at = ?
                        WHERE outbox_key = ? AND state = 'delivering'
                          AND COALESCE(claim_expires_at, 0) <= ?
                        """,
                        (_now(), expired["outbox_key"], now),
                    )
                    if transitioned.rowcount == 1:
                        self._activate_handoff(
                            str(expired["session_key"]),
                            str(expired["event_key"]),
                            "ambiguous_expired_delivery",
                            {},
                        )
                row = self.connection.execute(
                    """
                    SELECT o.* FROM channel_outbox AS o
                    LEFT JOIN session_controls AS s ON s.session_key = o.session_key
                    WHERE o.next_attempt_at <= ?
                      AND (
                        o.state = 'ready' OR (
                          o.state = 'delivering'
                          AND COALESCE(o.claim_expires_at, 0) <= ?
                          AND o.supports_native_idempotency = 1
                        )
                      )
                      AND COALESCE(s.generation, 0) = o.session_generation
                      AND (
                        (o.message_kind = 'auto_reply'
                          AND COALESCE(s.state, 'bot') = 'bot')
                        OR (o.message_kind = 'handoff_ack'
                          AND COALESCE(s.state, 'bot') = 'human_active')
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM channel_outbox AS earlier
                        WHERE earlier.session_key = o.session_key
                          AND earlier.id < o.id
                          AND earlier.state IN ('ready', 'delivering')
                      )
                    ORDER BY o.id
                    LIMIT 1
                    """,
                    (now, now),
                ).fetchone()
                if row is None:
                    self.connection.commit()
                    return None
                cursor = self.connection.execute(
                    """
                    UPDATE channel_outbox
                    SET state = 'delivering', claim_token = ?, claim_expires_at = ?,
                        attempt_count = attempt_count + 1, updated_at = ?
                    WHERE outbox_key = ? AND (
                        state = 'ready' OR (
                            state = 'delivering' AND COALESCE(claim_expires_at, 0) <= ?
                            AND supports_native_idempotency = 1
                        )
                    )
                    """,
                    (token, now + lease_seconds, _now(), row["outbox_key"], now),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("Outbox lease was claimed concurrently")
                claimed = self.connection.execute(
                    "SELECT * FROM channel_outbox WHERE outbox_key = ?",
                    (row["outbox_key"],),
                ).fetchone()
                self.connection.commit()
                if claimed is None:
                    raise RuntimeError("Claimed outbox command disappeared")
                return OutboxLease(
                    outbox_key=str(claimed["outbox_key"]),
                    connector_id=str(claimed["connector_id"]),
                    event_key=str(claimed["event_key"]),
                    session_key=str(claimed["session_key"]),
                    tenant_id=str(claimed["tenant_id"]),
                    channel=str(claimed["channel"]),
                    store_id=str(claimed["store_id"]),
                    conversation_id=str(claimed["conversation_id"]),
                    customer_id=str(claimed["customer_id"]),
                    body=str(claimed["body"]),
                    idempotency_key=str(claimed["idempotency_key"]),
                    payload_hash=str(claimed["payload_hash"]),
                    binding_fingerprint=str(claimed["binding_fingerprint"]),
                    supports_native_idempotency=bool(claimed["supports_native_idempotency"]),
                    supports_receipt_lookup=bool(claimed["supports_receipt_lookup"]),
                    message_kind=str(claimed["message_kind"]),
                    session_generation=int(claimed["session_generation"]),
                    claim_token=token,
                    attempt_count=int(claimed["attempt_count"]),
                )
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def renew_outbox(self, lease: OutboxLease, *, lease_seconds: float = 60.0) -> None:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE channel_outbox
                SET claim_expires_at = ?, updated_at = ?
                WHERE outbox_key = ? AND state = 'delivering' AND claim_token = ?
                """,
                (time.time() + lease_seconds, _now(), lease.outbox_key, lease.claim_token),
            )
        if cursor.rowcount != 1:
            raise ConflictError("Outbox lease was lost before renewal")

    def succeed_outbox(self, lease: OutboxLease, *, receipt: str) -> None:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE channel_outbox
                SET state = 'succeeded', remote_receipt = ?, claim_token = NULL,
                    claim_expires_at = NULL, last_error = NULL, updated_at = ?
                WHERE outbox_key = ? AND state = 'delivering' AND claim_token = ?
                """,
                (receipt, _now(), lease.outbox_key, lease.claim_token),
            )
        if cursor.rowcount != 1:
            raise ConflictError("Outbox lease was lost before receipt recording")

    def delivery_allowed(self, lease: OutboxLease) -> bool:
        with self._lock:
            control = self.connection.execute(
                "SELECT state, generation FROM session_controls WHERE session_key = ?",
                (lease.session_key,),
            ).fetchone()
        state = str(control["state"]) if control else "bot"
        generation = int(control["generation"]) if control else 0
        expected_state = "human_active" if lease.message_kind == "handoff_ack" else "bot"
        return generation == lease.session_generation and state == expected_state

    def cancel_outbox(self, lease: OutboxLease, *, reason: str) -> None:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE channel_outbox
                SET state = 'cancelled', claim_token = NULL, claim_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE outbox_key = ? AND state = 'delivering' AND claim_token = ?
                """,
                (reason[:300], _now(), lease.outbox_key, lease.claim_token),
            )
        if cursor.rowcount != 1:
            raise ConflictError("Outbox lease was lost before cancellation")

    def cancel_outbox_to_handoff(
        self,
        lease: OutboxLease,
        *,
        reason: str,
        handoff_reason: str = "delivery_cancelled",
    ) -> None:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE channel_outbox
                SET state = 'cancelled', claim_token = NULL, claim_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE outbox_key = ? AND state = 'delivering' AND claim_token = ?
                """,
                (reason[:300], _now(), lease.outbox_key, lease.claim_token),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Outbox lease was lost before handoff cancellation")
            self._activate_handoff(
                lease.session_key,
                lease.event_key,
                handoff_reason,
                {"reason": reason[:100]},
            )

    def requeue_outbox(
        self,
        lease: OutboxLease,
        *,
        reason: str,
        retry_delay_seconds: float = 0,
        max_attempts: int = 3,
    ) -> None:
        terminal = lease.attempt_count >= max_attempts
        state = "dead" if terminal else "ready"
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE channel_outbox
                SET state = ?, claim_token = NULL, claim_expires_at = NULL,
                    next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE outbox_key = ? AND state = 'delivering' AND claim_token = ?
                """,
                (
                    state,
                    time.time() + retry_delay_seconds,
                    reason[:300],
                    _now(),
                    lease.outbox_key,
                    lease.claim_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Outbox lease was lost before retry")
            if terminal:
                self._activate_handoff(
                    lease.session_key,
                    lease.event_key,
                    "delivery_retry_exhausted",
                    {"attempt_count": lease.attempt_count},
                )

    def fail_outbox_to_handoff(self, lease: OutboxLease, *, reason: str) -> None:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE channel_outbox
                SET state = 'unknown', claim_token = NULL, claim_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE outbox_key = ? AND state = 'delivering' AND claim_token = ?
                """,
                (reason[:300], _now(), lease.outbox_key, lease.claim_token),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Outbox lease was lost before handoff")
            self._activate_handoff(
                lease.session_key,
                lease.event_key,
                "ambiguous_delivery",
                {"reason": reason[:100]},
            )

    def resolve_unknown_outbox(
        self,
        outbox_key: str,
        *,
        resolution: str,
        operator_label: str,
    ) -> None:
        """Record a synthetic manual disposition before automation may resume."""

        if resolution not in {"confirmed", "not_sent", "cancelled"}:
            raise ValueError("Unsupported unknown-delivery resolution")
        if not operator_label or len(operator_label) > 128:
            raise ValueError("operator_label must contain 1-128 characters")
        state = "succeeded" if resolution == "confirmed" else "cancelled"
        operator_hash = canonical_hash({"operator_label": operator_label})[:16]
        receipt = f"manual:{operator_hash}" if resolution == "confirmed" else None
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE channel_outbox
                SET state = ?, remote_receipt = ?,
                    last_error = ?, updated_at = ?
                WHERE outbox_key = ? AND state = 'unknown'
                """,
                (state, receipt, f"manual_resolution:{resolution}", _now(), outbox_key),
            )
        if cursor.rowcount != 1:
            raise ConflictError("Only an unknown outbox item can be manually resolved")

    def row(self, table: str, key_name: str, key: str) -> dict[str, Any] | None:
        """Return a test/demo snapshot from a fixed allowlist of queue tables."""

        allowed = {
            ("channel_inbox", "event_key"),
            ("channel_outbox", "outbox_key"),
            ("session_controls", "session_key"),
        }
        if (table, key_name) not in allowed:
            raise ValueError("Unsupported diagnostic lookup")
        with self._lock:
            result = self.connection.execute(
                f"SELECT * FROM {table} WHERE {key_name} = ?",  # noqa: S608 - allowlisted
                (key,),
            ).fetchone()
        return dict(result) if result else None

    def handoff_count(self, session_key: str) -> int:
        with self._lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM handoff_events WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        return int(row["count"]) if row else 0

    @staticmethod
    def _decode_inbox(row: sqlite3.Row, token: str) -> InboxLease:
        attachments = cast(list[str], json.loads(row["attachments_json"]))
        return InboxLease(
            event_key=str(row["event_key"]),
            connector_id=str(row["connector_id"]),
            external_event_id=str(row["external_event_id"]),
            tenant_id=str(row["tenant_id"]),
            channel=str(row["channel"]),
            store_id=str(row["store_id"]),
            conversation_id=str(row["conversation_id"]),
            customer_id=str(row["customer_id"]),
            message_id=str(row["message_id"]),
            text=str(row["text"]),
            attachments=tuple(attachments),
            received_at=str(row["received_at"]),
            binding_fingerprint=str(row["binding_fingerprint"]),
            payload_fingerprint=str(row["payload_fingerprint"]),
            supports_native_idempotency=bool(row["supports_native_idempotency"]),
            supports_receipt_lookup=bool(row["supports_receipt_lookup"]),
            claim_token=token,
            attempt_count=int(row["attempt_count"]),
        )

    def _activate_handoff(
        self,
        session_key: str,
        event_key: str,
        reason: str,
        detail: dict[str, Any],
    ) -> int:
        timestamp = _now()
        current = self.connection.execute(
            "SELECT state, generation FROM session_controls WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        if current is not None and current["state"] == "human_active":
            generation = int(current["generation"])
        else:
            generation = (int(current["generation"]) if current else 0) + 1
        self.connection.execute(
            """
            INSERT INTO session_controls(session_key, state, reason, generation, updated_at)
            VALUES (?, 'human_active', ?, ?, ?)
            ON CONFLICT(session_key) DO UPDATE SET
                state = 'human_active', reason = excluded.reason,
                generation = excluded.generation, updated_at = excluded.updated_at
            """,
            (session_key, reason, generation, timestamp),
        )
        self.connection.execute(
            """
            UPDATE channel_outbox
            SET state = 'cancelled', last_error = 'cancelled_by_handoff',
                updated_at = ?
            WHERE session_key = ? AND state = 'ready'
              AND message_kind = 'auto_reply'
            """,
            (timestamp, session_key),
        )
        self.connection.execute(
            "INSERT INTO handoff_events VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"handoff_{uuid.uuid4().hex}",
                session_key,
                event_key,
                reason,
                json.dumps(detail, ensure_ascii=False),
                timestamp,
            ),
        )
        return generation
