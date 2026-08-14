from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dxl_agent.connectors import (  # noqa: E402
    BrowserConnector,
    ConnectorBinding,
    ConnectorProtocolError,
    DeliveryDisposition,
    MobileConnector,
    OutboundMessage,
    ReceiptDisposition,
    SyntheticInboundEvent,
)


class ConnectorTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.browser_binding = ConnectorBinding(
            connector_id="browser-demo-01",
            tenant_id="demo",
            store_id="STORE-001",
            channel="synthetic-browser",
        )
        self.mobile_binding = ConnectorBinding(
            connector_id="mobile-demo-01",
            tenant_id="demo",
            store_id="STORE-002",
            channel="synthetic-mobile",
        )

    @staticmethod
    def outbound(
        *,
        business_key: str = "reply:event-0001",
        idempotency_key: str = "idem-0001",
        text: str = "这是完全合成的客服回复。",
    ) -> OutboundMessage:
        return OutboundMessage(
            business_key=business_key,
            conversation_id="SYN-CONVERSATION-0001",
            text=text,
            idempotency_key=idempotency_key,
        )

    async def test_binding_is_authoritative_and_normalization_redacts(self) -> None:
        occurred_at = datetime(2026, 8, 14, 1, 2, 3, tzinfo=UTC)
        raw = SyntheticInboundEvent(
            external_message_id="SYN-MESSAGE-0001",
            conversation_id="SYN-CONVERSATION-0001",
            customer_ref="SYN-CUSTOMER-0001",
            text=(
                "请联系 13900000000 或 synthetic.user@example.invalid，api_key=syntheticsecret123"
            ),
            source_sequence=7,
            occurred_at=occurred_at,
            untrusted_metadata={
                "tenant_id": "attacker-tenant",
                "store_id": "ATTACKER-STORE",
            },
        )
        connector = BrowserConnector(self.browser_binding, (raw,))

        first = await connector.poll()
        replay = await connector.poll("0")
        event = first.events[0]

        self.assertEqual(first.next_cursor, "1")
        self.assertEqual(event.event_id, replay.events[0].event_id)
        self.assertEqual(event.tenant_id, "demo")
        self.assertEqual(event.store_id, "STORE-001")
        self.assertEqual(event.channel, "synthetic-browser")
        self.assertEqual(event.external_event_id, raw.external_message_id)
        self.assertEqual(event.customer_id, raw.customer_ref)
        self.assertEqual(event.message_id, raw.external_message_id)
        self.assertEqual(event.attachments, raw.attachment_refs)
        self.assertEqual(event.received_at, occurred_at)
        self.assertEqual(connector.connector_id, self.browser_binding.connector_id)
        self.assertNotIn("attacker", repr(event))
        self.assertIn("[REDACTED_PHONE]", event.text)
        self.assertIn("[REDACTED_EMAIL]", event.text)
        self.assertIn("[REDACTED_SECRET]", event.text)
        incoming = event.to_incoming_message()
        self.assertEqual(incoming.tenant_id, self.browser_binding.tenant_id)
        self.assertEqual(incoming.store_id, self.browser_binding.store_id)
        self.assertEqual(incoming.message_id, raw.external_message_id)

    async def test_poll_cursor_and_capabilities_are_explicit(self) -> None:
        first = SyntheticInboundEvent(
            external_message_id="SYN-MESSAGE-0001",
            conversation_id="SYN-CONVERSATION-0001",
            customer_ref="SYN-CUSTOMER-0001",
            text="第一条",
        )
        second = SyntheticInboundEvent(
            external_message_id="SYN-MESSAGE-0002",
            conversation_id="SYN-CONVERSATION-0001",
            customer_ref="SYN-CUSTOMER-0001",
            text="第二条",
        )
        browser = BrowserConnector(self.browser_binding, (first, second))
        mobile = MobileConnector(self.mobile_binding)

        batch = await browser.poll(limit=1)
        next_batch = await browser.poll(batch.next_cursor, limit=1)
        self.assertEqual(batch.events[0].external_message_id, "SYN-MESSAGE-0001")
        self.assertEqual(next_batch.events[0].external_message_id, "SYN-MESSAGE-0002")
        self.assertTrue(browser.capabilities.supports_native_idempotency)
        self.assertTrue(browser.capabilities.supports_receipt_lookup)
        self.assertFalse(mobile.capabilities.supports_native_idempotency)
        self.assertFalse(mobile.capabilities.supports_receipt_lookup)
        self.assertLess(
            mobile.capabilities.max_outbound_chars,
            browser.capabilities.max_outbound_chars,
        )
        with self.assertRaises(ConnectorProtocolError):
            await browser.poll("not-a-cursor")

    async def test_browser_idempotency_prevents_duplicate_remote_send(self) -> None:
        connector = BrowserConnector(self.browser_binding)
        message = self.outbound()

        first = await connector.send(message)
        replay = await connector.send(message)

        self.assertEqual(first.disposition, DeliveryDisposition.CONFIRMED)
        self.assertTrue(first.remote_attempted)
        self.assertEqual(replay.disposition, DeliveryDisposition.CONFIRMED)
        self.assertFalse(replay.remote_attempted)
        self.assertTrue(replay.deduplicated)
        self.assertEqual(first.provider_operation_id, replay.provider_operation_id)
        self.assertEqual(connector.remote_attempt_count, 1)

        conflicting = self.outbound(text="同一个幂等键下的不同内容")
        with self.assertRaises(ConnectorProtocolError):
            await connector.send(conflicting)

    async def test_browser_timeout_after_commit_is_reconciled_by_receipt(self) -> None:
        connector = BrowserConnector(self.browser_binding)
        connector.simulate_timeout_after_commit_once()
        message = self.outbound()

        attempt = await connector.send(message)
        receipt = await connector.lookup_receipt(idempotency_key=message.idempotency_key)
        replay = await connector.send(message)

        self.assertEqual(attempt.disposition, DeliveryDisposition.UNKNOWN)
        self.assertTrue(attempt.remote_attempted)
        self.assertEqual(receipt.disposition, ReceiptDisposition.CONFIRMED)
        self.assertIsNotNone(receipt.provider_operation_id)
        self.assertEqual(replay.disposition, DeliveryDisposition.CONFIRMED)
        self.assertTrue(replay.deduplicated)
        self.assertFalse(replay.remote_attempted)
        self.assertEqual(connector.remote_attempt_count, 1)

    async def test_browser_missing_receipt_is_known_safe_to_retry(self) -> None:
        connector = BrowserConnector(self.browser_binding)
        receipt = await connector.lookup_receipt(idempotency_key="missing-idem-0001")
        self.assertEqual(receipt.disposition, ReceiptDisposition.NOT_FOUND_SAFE_TO_RETRY)

    async def test_mobile_offline_and_foreground_loss_do_not_attempt_send(self) -> None:
        connector = MobileConnector(self.mobile_binding)
        message = self.outbound()

        connector.set_online(False)
        offline = await connector.send(message)
        self.assertEqual(offline.disposition, DeliveryDisposition.RETRYABLE)
        self.assertEqual(offline.safe_code, "synthetic_device_offline")
        self.assertFalse(offline.remote_attempted)
        self.assertFalse((await connector.health()).ready)

        connector.set_online(True)
        connector.set_foreground_available(False)
        foreground_lost = await connector.send(message)
        self.assertEqual(foreground_lost.disposition, DeliveryDisposition.RETRYABLE)
        self.assertEqual(foreground_lost.safe_code, "synthetic_foreground_lease_lost")
        self.assertFalse(foreground_lost.remote_attempted)
        self.assertEqual(connector.remote_attempt_count, 0)

        connector.set_foreground_available(True)
        confirmed = await connector.send(message)
        self.assertEqual(confirmed.disposition, DeliveryDisposition.CONFIRMED)
        self.assertEqual(connector.remote_attempt_count, 1)

    async def test_mobile_unknown_result_latches_business_key_without_blind_retry(self) -> None:
        connector = MobileConnector(self.mobile_binding)
        first_message = self.outbound()
        connector.simulate_unknown_result_once()

        unknown = await connector.send(first_message)
        receipt = await connector.lookup_receipt(idempotency_key=first_message.idempotency_key)
        same_key_replay = await connector.send(first_message)
        new_transport_key = self.outbound(idempotency_key="idem-0002")
        changed_key_replay = await connector.send(new_transport_key)

        self.assertEqual(unknown.disposition, DeliveryDisposition.UNKNOWN)
        self.assertTrue(unknown.remote_attempted)
        self.assertEqual(receipt.disposition, ReceiptDisposition.UNKNOWN)
        for replay in (same_key_replay, changed_key_replay):
            self.assertEqual(replay.disposition, DeliveryDisposition.UNKNOWN)
            self.assertFalse(replay.remote_attempted)
            self.assertTrue(replay.deduplicated)
            self.assertIn("no_blind_retry", replay.safe_code)
        self.assertEqual(connector.remote_attempt_count, 1)

        unrelated = self.outbound(
            business_key="reply:event-0002",
            idempotency_key="idem-0003",
        )
        delivered = await connector.send(unrelated)
        self.assertEqual(delivered.disposition, DeliveryDisposition.CONFIRMED)
        self.assertEqual(connector.remote_attempt_count, 2)

    async def test_outbound_limits_and_key_validation_fail_closed(self) -> None:
        mobile = MobileConnector(self.mobile_binding)
        with self.assertRaises(ConnectorProtocolError):
            OutboundMessage(
                business_key="reply:event-0001",
                conversation_id="SYN-CONVERSATION-0001",
                text="hello",
                idempotency_key="short",
            )
        with self.assertRaises(ConnectorProtocolError):
            await mobile.send(self.outbound(text="x" * 1001))


if __name__ == "__main__":
    unittest.main()
