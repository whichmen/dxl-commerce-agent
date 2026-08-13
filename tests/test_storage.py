from __future__ import annotations

import concurrent.futures
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from dxl_agent.domain import ActionState, ConflictError
from dxl_agent.storage import SQLiteStore


class StorageConcurrencyTestCase(unittest.TestCase):
    def test_message_claim_is_fail_fast_and_expired_lease_can_be_recovered(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dxl-message-claim-") as directory:
            database = Path(directory) / "claims.db"
            first_store = SQLiteStore(database)
            second_store = SQLiteStore(database)
            self.addCleanup(first_store.connection.close)
            self.addCleanup(second_store.connection.close)

            first_token, response = first_store.claim_message("message-1", lease_seconds=60)
            self.assertIsNotNone(first_token)
            self.assertIsNone(response)
            with self.assertRaises(ConflictError):
                second_store.claim_message("message-1", lease_seconds=60)

            first_store.connection.execute(
                "UPDATE messages SET claim_expires_at = 0 WHERE dedupe_key = ?",
                ("message-1",),
            )
            first_store.connection.commit()
            second_token, response = second_store.claim_message("message-1", lease_seconds=60)
            self.assertIsNotNone(second_token)
            self.assertNotEqual(first_token, second_token)
            self.assertIsNone(response)
            second_store.complete_message("message-1", str(second_token), {"status": "resolved"})
            token, replay = first_store.claim_message("message-1")
            self.assertIsNone(token)
            self.assertEqual(replay, {"status": "resolved"})

    def test_two_connections_cannot_execute_one_action_twice(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dxl-store-test-") as directory:
            database = Path(directory) / "shared.db"
            first_store = SQLiteStore(database)
            second_store = SQLiteStore(database)
            self.addCleanup(first_store.connection.close)
            self.addCleanup(second_store.connection.close)
            action, _ = first_store.create_action(
                "action_concurrent",
                "business_concurrent",
                {"order_id": "SYN-ORDER-1003", "amount_cents": 300},
                {"outcome": "allow"},
                ActionState.APPROVED,
            )
            barrier = threading.Barrier(2)

            def execute(store: SQLiteStore, key: str) -> str:
                barrier.wait()
                try:
                    return str(store.execute_action(action["action_id"], key)["status"])
                except ConflictError:
                    return "conflict"

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        lambda pair: execute(*pair),
                        ((first_store, "concurrent-key-1"), (second_store, "concurrent-key-2")),
                    )
                )

            self.assertCountEqual(results, ["succeeded", "conflict"])
            with sqlite3.connect(database) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM executions WHERE action_id = ?",
                    (action["action_id"],),
                ).fetchone()[0]
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
