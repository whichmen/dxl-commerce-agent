from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dxl_agent.channel_store import ChannelStore, InboxRecord
from dxl_agent.domain import ConflictError, scoped_key


class ChannelStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="dxl-channel-store-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = ChannelStore(Path(self.temporary_directory.name) / "channel.db")
        self.addCleanup(self.store.connection.close)

    @staticmethod
    def record(event_id: str = "evt-1") -> InboxRecord:
        return InboxRecord(
            event_key=scoped_key("channel_event", "browser-demo", event_id),
            connector_id="browser-demo",
            external_event_id=event_id,
            tenant_id="demo",
            channel="browser",
            store_id="STORE-001",
            conversation_id="SYN-CONVERSATION-0001",
            customer_id="CUST-0042",
            message_id=event_id,
            text="SKU-MUG-BLUE 的材质",
            attachments=(),
            received_at="2026-01-01T00:00:00+00:00",
            binding_fingerprint="synthetic-binding-fingerprint",
            payload_fingerprint=f"synthetic-payload-{event_id}",
            supports_native_idempotency=True,
            supports_receipt_lookup=True,
        )

    def test_poll_cursor_and_event_are_committed_idempotently(self) -> None:
        record = self.record()
        self.assertEqual(
            self.store.record_poll(
                connector_id=record.connector_id,
                expected_cursor=None,
                next_cursor="cursor-1",
                records=[record],
            ),
            1,
        )
        self.assertEqual(
            self.store.record_poll(
                connector_id=record.connector_id,
                expected_cursor="cursor-1",
                next_cursor="cursor-2",
                records=[record],
            ),
            0,
        )
        self.assertEqual(self.store.cursor(record.connector_id), "cursor-2")

        lease = self.store.claim_inbox()
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertEqual(lease.tenant_id, "demo")
        self.store.complete_with_reply(
            lease,
            reply="合成回复",
            decision={"status": "resolved"},
        )
        self.assertIsNone(self.store.claim_inbox())

    def test_expired_inbox_lease_is_fenced(self) -> None:
        record = self.record()
        self.store.record_poll(
            connector_id=record.connector_id,
            expected_cursor=None,
            next_cursor="cursor-1",
            records=[record],
        )
        first = self.store.claim_inbox(lease_seconds=60)
        self.assertIsNotNone(first)
        assert first is not None
        self.store.connection.execute(
            "UPDATE channel_inbox SET claim_expires_at = 0 WHERE event_key = ?",
            (record.event_key,),
        )
        self.store.connection.commit()
        second = self.store.claim_inbox(lease_seconds=60)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertNotEqual(first.claim_token, second.claim_token)
        with self.assertRaises(ConflictError):
            self.store.complete_with_reply(first, reply="stale", decision={})
        self.store.complete_with_reply(second, reply="current", decision={})

    def test_cursor_cas_and_duplicate_payload_conflict_fail_closed(self) -> None:
        record = self.record("evt-cas")
        self.store.record_poll(
            connector_id=record.connector_id,
            expected_cursor=None,
            next_cursor="cursor-1",
            records=[record],
        )
        with self.assertRaises(ConflictError):
            self.store.record_poll(
                connector_id=record.connector_id,
                expected_cursor=None,
                next_cursor="stale-cursor",
                records=[],
            )
        with self.assertRaises(ConflictError):
            self.store.record_poll(
                connector_id=record.connector_id,
                expected_cursor="cursor-1",
                next_cursor="cursor-2",
                records=[replace(record, payload_fingerprint="changed-payload")],
            )
        self.assertEqual(self.store.cursor(record.connector_id), "cursor-1")

    def test_same_session_inbox_claims_are_ordered_and_renewable(self) -> None:
        first_record = self.record("evt-ordered-1")
        second_record = self.record("evt-ordered-2")
        self.store.record_poll(
            connector_id=first_record.connector_id,
            expected_cursor=None,
            next_cursor="cursor-2",
            records=[first_record, second_record],
        )
        second_store = ChannelStore(self.store.path)
        self.addCleanup(second_store.connection.close)
        first = self.store.claim_inbox(lease_seconds=1)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.event_key, first_record.event_key)
        self.store.renew_inbox(first, lease_seconds=60)
        self.assertIsNone(second_store.claim_inbox())
        self.store.complete_with_reply(first, reply="first", decision={})
        second = second_store.claim_inbox()
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.event_key, second_record.event_key)
        second_store.complete_with_reply(second, reply="second", decision={})

        first_delivery = self.store.claim_outbox()
        self.assertIsNotNone(first_delivery)
        assert first_delivery is not None
        self.store.requeue_outbox(
            first_delivery,
            reason="synthetic_pre_attempt_retry",
            retry_delay_seconds=60,
        )
        self.assertIsNone(second_store.claim_outbox())
        self.store.connection.execute(
            "UPDATE channel_outbox SET next_attempt_at = 0 WHERE outbox_key = ?",
            (first_delivery.outbox_key,),
        )
        self.store.connection.commit()
        retried_first = second_store.claim_outbox()
        self.assertIsNotNone(retried_first)
        assert retried_first is not None
        self.assertEqual(retried_first.outbox_key, first_delivery.outbox_key)
        second_store.succeed_outbox(retried_first, receipt="synthetic-first-receipt")
        later_delivery = self.store.claim_outbox()
        self.assertIsNotNone(later_delivery)
        assert later_delivery is not None
        self.assertNotEqual(later_delivery.outbox_key, first_delivery.outbox_key)

    def test_expired_non_idempotent_delivery_requires_manual_resolution(self) -> None:
        record = replace(
            self.record("evt-mobile-expired"),
            binding_fingerprint="synthetic-mobile-binding",
            supports_native_idempotency=False,
            supports_receipt_lookup=False,
        )
        self.store.record_poll(
            connector_id=record.connector_id,
            expected_cursor=None,
            next_cursor="cursor-1",
            records=[record],
        )
        inbox = self.store.claim_inbox()
        assert inbox is not None
        outbox_key = self.store.complete_with_reply(inbox, reply="reply", decision={})
        assert outbox_key is not None
        delivery = self.store.claim_outbox(lease_seconds=1)
        self.assertIsNotNone(delivery)
        assert delivery is not None
        self.store.connection.execute(
            "UPDATE channel_outbox SET claim_expires_at = 0 WHERE outbox_key = ?",
            (outbox_key,),
        )
        self.store.connection.commit()

        self.assertIsNone(self.store.claim_outbox())
        outbox = self.store.row("channel_outbox", "outbox_key", outbox_key)
        assert outbox is not None
        self.assertEqual(outbox["state"], "unknown")
        self.assertEqual(self.store.session_state(inbox.session_key), "human_active")
        with self.assertRaises(ConflictError):
            self.store.release_handoff(inbox.session_key)
        self.store.resolve_unknown_outbox(
            outbox_key,
            resolution="not_sent",
            operator_label="synthetic-reviewer",
        )
        self.store.release_handoff(inbox.session_key)
        self.assertEqual(self.store.session_state(inbox.session_key), "bot")

    def test_outbox_delivery_is_leased_and_receipted_once(self) -> None:
        record = self.record()
        self.store.record_poll(
            connector_id=record.connector_id,
            expected_cursor=None,
            next_cursor="cursor-1",
            records=[record],
        )
        inbox = self.store.claim_inbox()
        assert inbox is not None
        outbox_key = self.store.complete_with_reply(
            inbox,
            reply="合成回复",
            decision={"status": "resolved"},
        )
        delivery = self.store.claim_outbox()
        self.assertIsNotNone(delivery)
        assert delivery is not None
        self.assertEqual(delivery.outbox_key, outbox_key)
        self.store.succeed_outbox(delivery, receipt="synthetic-receipt")
        self.assertIsNone(self.store.claim_outbox())
        row = self.store.row("channel_outbox", "outbox_key", outbox_key)
        assert row is not None
        self.assertEqual(row["state"], "succeeded")

    def test_explicit_handoff_parks_later_messages_until_release(self) -> None:
        first_record = self.record("evt-handoff")
        self.store.record_poll(
            connector_id=first_record.connector_id,
            expected_cursor=None,
            next_cursor="cursor-1",
            records=[first_record],
        )
        first = self.store.claim_inbox()
        assert first is not None
        self.store.handoff_with_ack(first, reason="customer_requested", acknowledgement="已转人工")
        self.assertEqual(self.store.session_state(first.session_key), "human_active")
        self.assertEqual(self.store.handoff_count(first.session_key), 1)
        acknowledgement = self.store.claim_outbox()
        self.assertIsNotNone(acknowledgement)
        assert acknowledgement is not None
        with self.assertRaises(ConflictError):
            self.store.release_handoff(first.session_key)
        self.store.succeed_outbox(acknowledgement, receipt="synthetic-handoff-ack")

        second_record = replace(self.record("evt-later"), customer_id=first.customer_id)
        self.store.record_poll(
            connector_id=second_record.connector_id,
            expected_cursor="cursor-1",
            next_cursor="cursor-2",
            records=[second_record],
        )
        second = self.store.claim_inbox()
        assert second is not None
        self.store.park_for_human(second)
        parked = self.store.row("channel_inbox", "event_key", second.event_key)
        assert parked is not None
        self.assertEqual(parked["state"], "parked")
        self.store.release_handoff(first.session_key)
        self.assertEqual(self.store.session_state(first.session_key), "bot")
        resumed = self.store.claim_inbox()
        self.assertIsNotNone(resumed)
        assert resumed is not None
        self.assertEqual(resumed.event_key, second.event_key)


if __name__ == "__main__":
    unittest.main()
