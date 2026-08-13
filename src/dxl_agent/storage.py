from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .domain import ActionState, ConflictError, InvalidTransitionError, NotFoundError


def _now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class SQLiteStore:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self.connection:
            self.connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA busy_timeout=5000;
                CREATE TABLE IF NOT EXISTS messages (
                    dedupe_key TEXT PRIMARY KEY,
                    response_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'completed',
                    claim_token TEXT,
                    claim_expires_at REAL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY,
                    trace_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY,
                    business_key TEXT NOT NULL UNIQUE,
                    action_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    approved_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executions (
                    idempotency_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS executions_action_id_idx
                    ON executions(action_id);
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            # A small forward migration keeps local demo databases created by an
            # earlier checkout usable. It contains no production data.
            columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(actions)")}
            if "business_key" not in columns:
                self.connection.execute("ALTER TABLE actions ADD COLUMN business_key TEXT")
                self.connection.execute(
                    "UPDATE actions SET business_key = 'legacy:' || action_id "
                    "WHERE business_key IS NULL"
                )
                self.connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS actions_business_key_idx "
                    "ON actions(business_key)"
                )

            message_columns = {
                row["name"] for row in self.connection.execute("PRAGMA table_info(messages)")
            }
            if "state" not in message_columns:
                self.connection.execute(
                    "ALTER TABLE messages ADD COLUMN state TEXT NOT NULL DEFAULT 'completed'"
                )
            if "claim_token" not in message_columns:
                self.connection.execute("ALTER TABLE messages ADD COLUMN claim_token TEXT")
            if "claim_expires_at" not in message_columns:
                self.connection.execute("ALTER TABLE messages ADD COLUMN claim_expires_at REAL")

    def health(self) -> bool:
        with self._lock:
            row = self.connection.execute("SELECT 1 AS ok").fetchone()
        return bool(row and row["ok"] == 1)

    def get_message_response(self, dedupe_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT response_json FROM messages WHERE dedupe_key = ? AND state = 'completed'",
                (dedupe_key,),
            ).fetchone()
        return json.loads(row["response_json"]) if row else None

    def claim_message(
        self, dedupe_key: str, *, lease_seconds: float = 60.0
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Atomically lease a message, return its result, or fail fast if in flight."""

        now = time.time()
        token = uuid.uuid4().hex
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    """
                    SELECT response_json, state, claim_expires_at
                    FROM messages WHERE dedupe_key = ?
                    """,
                    (dedupe_key,),
                ).fetchone()
                if row is None:
                    self.connection.execute(
                        """
                        INSERT INTO messages(
                            dedupe_key, response_json, state, claim_token,
                            claim_expires_at, created_at
                        ) VALUES (?, '{}', 'processing', ?, ?, ?)
                        """,
                        (dedupe_key, token, now + lease_seconds, _now()),
                    )
                    self.connection.commit()
                    return token, None
                if row["state"] == "completed":
                    response = cast(dict[str, Any], json.loads(row["response_json"]))
                    self.connection.commit()
                    return None, response
                expires_at = float(row["claim_expires_at"] or 0)
                if expires_at <= now:
                    cursor = self.connection.execute(
                        """
                        UPDATE messages SET claim_token = ?, claim_expires_at = ?
                        WHERE dedupe_key = ? AND state = 'processing'
                          AND COALESCE(claim_expires_at, 0) <= ?
                        """,
                        (token, now + lease_seconds, dedupe_key, now),
                    )
                    if cursor.rowcount == 1:
                        self.connection.commit()
                        return token, None
                self.connection.commit()
                raise ConflictError("Duplicate message is still processing; retry later")
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def complete_message(self, dedupe_key: str, claim_token: str, response: dict[str, Any]) -> None:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE messages
                SET response_json = ?, state = 'completed', claim_token = NULL,
                    claim_expires_at = NULL
                WHERE dedupe_key = ? AND state = 'processing' AND claim_token = ?
                """,
                (json.dumps(response, ensure_ascii=False), dedupe_key, claim_token),
            )
        if cursor.rowcount != 1:
            raise ConflictError("Message claim was lost before completion")

    def finalize_message(
        self,
        *,
        dedupe_key: str,
        claim_token: str,
        session_key: str,
        user_content: str,
        assistant_content: str,
        trace_id: str,
        trace: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        """Atomically persist the completed turn, trace, and cached response."""

        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE messages
                SET response_json = ?, state = 'completed', claim_token = NULL,
                    claim_expires_at = NULL
                WHERE dedupe_key = ? AND state = 'processing' AND claim_token = ?
                """,
                (json.dumps(response, ensure_ascii=False), dedupe_key, claim_token),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Message claim was lost before completion")
            timestamp = _now()
            self.connection.executemany(
                """
                INSERT INTO turns(session_key, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (session_key, "user", user_content, timestamp),
                    (session_key, "assistant", assistant_content, timestamp),
                ),
            )
            self.connection.execute(
                "INSERT INTO traces VALUES (?, ?, ?)",
                (trace_id, json.dumps(trace, ensure_ascii=False), timestamp),
            )
            self._append_audit_event(
                "trace_recorded",
                trace_id,
                {"intent": trace["intent"], "status": trace["status"]},
            )

    def release_message_claim(self, dedupe_key: str, claim_token: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                """
                DELETE FROM messages
                WHERE dedupe_key = ? AND state = 'processing' AND claim_token = ?
                """,
                (dedupe_key, claim_token),
            )

    def add_turn(self, session_key: str, role: str, content: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO turns(session_key, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_key, role, content, _now()),
            )

    def recent_turns(self, session_key: str, limit: int) -> list[dict[str, str]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT role, content, created_at FROM turns
                WHERE session_key = ? ORDER BY id DESC LIMIT ?
                """,
                (session_key, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def save_trace(self, trace_id: str, trace: dict[str, Any]) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO traces VALUES (?, ?, ?)",
                (trace_id, json.dumps(trace, ensure_ascii=False), _now()),
            )
            self._append_audit_event(
                "trace_recorded",
                trace_id,
                {"intent": trace["intent"], "status": trace["status"]},
            )

    def get_trace(self, trace_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT trace_json FROM traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
        if not row:
            raise NotFoundError("Trace not found")
        return cast(dict[str, Any], json.loads(row["trace_json"]))

    def create_action(
        self,
        action_id: str,
        business_key: str,
        payload: dict[str, Any],
        policy: dict[str, Any],
        state: ActionState,
    ) -> tuple[dict[str, Any], bool]:
        timestamp = _now()
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO actions VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    action_id,
                    business_key,
                    "refund",
                    state.value,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(policy, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
            row = self.connection.execute(
                "SELECT action_id FROM actions WHERE business_key = ?", (business_key,)
            ).fetchone()
            if not row:  # Defensive: INSERT OR IGNORE must leave an existing row.
                raise RuntimeError("Unable to persist action")
            persisted_id = str(row["action_id"])
            if cursor.rowcount == 1:
                self._append_audit_event(
                    "action_proposed",
                    persisted_id,
                    {"action_type": "refund", "state": state.value},
                )
        return self.get_action(persisted_id), cursor.rowcount == 0

    def get_action(self, action_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        if not row:
            raise NotFoundError("Action not found")
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["policy"] = json.loads(item.pop("policy_json"))
        return item

    def approve_action(self, action_id: str, approver: str) -> dict[str, Any]:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE actions SET state = ?, approved_by = ?, updated_at = ?
                WHERE action_id = ? AND state = ?
                """,
                (
                    ActionState.APPROVED.value,
                    approver,
                    _now(),
                    action_id,
                    ActionState.PENDING_APPROVAL.value,
                ),
            )
            if cursor.rowcount == 1:
                self._append_audit_event(
                    "action_approved",
                    action_id,
                    {"approver_label": approver},
                )
        action = self.get_action(action_id)
        if action["state"] == ActionState.APPROVED.value:
            return action
        raise InvalidTransitionError(f"Cannot approve action in state {action['state']}")

    def execute_action(self, action_id: str, idempotency_key: str) -> dict[str, Any]:
        with self._lock:
            try:
                # BEGIN IMMEDIATE coordinates independent SQLite connections, unlike
                # the in-process lock. The unique action index is a second backstop.
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    "SELECT * FROM actions WHERE action_id = ?", (action_id,)
                ).fetchone()
                if not row:
                    raise NotFoundError("Action not found")
                action = self._decode_action(row)
                request_hash = canonical_hash(
                    {"action_id": action_id, "payload": action["payload"]}
                )
                previous = self.connection.execute(
                    "SELECT * FROM executions WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise ConflictError(
                            "Idempotency key was already used for a different request"
                        )
                    result = cast(dict[str, Any], json.loads(previous["result_json"]))
                    result["deduplicated"] = True
                    self.connection.commit()
                    return result
                if action["state"] == ActionState.SUCCEEDED.value:
                    raise ConflictError(
                        "Action already succeeded; reuse the original idempotency key"
                    )
                if action["state"] != ActionState.APPROVED.value:
                    raise InvalidTransitionError("Action must be approved before execution")

                result = {
                    "action_id": action_id,
                    "status": "succeeded",
                    "sandbox": True,
                    "refund_id": f"SYN-REFUND-{action_id[-8:].upper()}",
                    "deduplicated": False,
                }
                self.connection.execute(
                    "INSERT INTO executions VALUES (?, ?, ?, ?, ?)",
                    (
                        idempotency_key,
                        request_hash,
                        action_id,
                        json.dumps(result, ensure_ascii=False),
                        _now(),
                    ),
                )
                cursor = self.connection.execute(
                    """
                    UPDATE actions SET state = ?, updated_at = ?
                    WHERE action_id = ? AND state = ?
                    """,
                    (
                        ActionState.SUCCEEDED.value,
                        _now(),
                        action_id,
                        ActionState.APPROVED.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("Action execution lost its state transition")
                self._append_audit_event(
                    "action_executed",
                    action_id,
                    {"status": "succeeded", "sandbox": True},
                )
                self.connection.commit()
                return result
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    @staticmethod
    def _decode_action(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["policy"] = json.loads(item.pop("policy_json"))
        return item

    def _append_audit_event(
        self, event_type: str, resource_id: str, detail: dict[str, Any]
    ) -> None:
        event_id = canonical_hash(
            {
                "event_type": event_type,
                "resource_id": resource_id,
                "detail": detail,
                "created_at": _now(),
            }
        )
        self.connection.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?)",
            (
                event_id,
                event_type,
                resource_id,
                json.dumps(detail, ensure_ascii=False),
                _now(),
            ),
        )
