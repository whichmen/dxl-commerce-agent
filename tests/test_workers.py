from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dxl_agent.channel_store import ChannelStore
from dxl_agent.config import Settings
from dxl_agent.connectors import (
    BrowserConnector,
    ConnectorBinding,
    DeliveryAttempt,
    DeliveryDisposition,
    MobileConnector,
    OutboundCommand,
    SyntheticInboundEvent,
)
from dxl_agent.domain import scoped_key
from dxl_agent.runtime import AgentRuntime
from dxl_agent.workers import (
    ConnectorIngestor,
    ConnectorRegistry,
    ConversationWorker,
    DeliveryWorker,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WorkerPipelineTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="channel-workers-")
        self.addCleanup(self.temporary_directory.cleanup)
        database = Path(self.temporary_directory.name) / "pipeline.db"
        self.channel_store = ChannelStore(database)
        self.addCleanup(self.channel_store.connection.close)
        self.runtime = AgentRuntime.from_settings(
            Settings(
                database_path=database,
                fixture_dir=PROJECT_ROOT / "demo" / "fixtures",
                policy_path=PROJECT_ROOT / "policies" / "default.toml",
                planner_mode="rules",
                demo_mode=True,
            )
        )
        self.addCleanup(self.runtime.store.connection.close)

    @staticmethod
    def browser(event: SyntheticInboundEvent) -> BrowserConnector:
        return BrowserConnector(
            ConnectorBinding(
                connector_id="browser-demo",
                tenant_id="demo",
                store_id="STORE-001",
                channel="synthetic-browser",
            ),
            (event,),
        )

    @staticmethod
    def mobile(event: SyntheticInboundEvent) -> MobileConnector:
        return MobileConnector(
            ConnectorBinding(
                connector_id="mobile-demo",
                tenant_id="demo",
                store_id="STORE-001",
                channel="synthetic-mobile",
            ),
            (event,),
        )

    async def test_browser_question_runs_end_to_end_and_reconciles_timeout(self) -> None:
        event = SyntheticInboundEvent(
            external_message_id="browser-message-1",
            conversation_id="browser-conversation-1",
            customer_ref="CUST-0042",
            text="SKU-MUG-BLUE 的材质和价格",
        )
        connector = self.browser(event)
        connector.simulate_timeout_after_commit_once()

        ingested = await ConnectorIngestor(
            store=self.channel_store,
            connector=connector,
        ).poll_once()
        decided = await ConversationWorker(
            store=self.channel_store,
            runtime=self.runtime,
        ).run_once()
        delivered = await DeliveryWorker(
            store=self.channel_store,
            connectors=ConnectorRegistry([connector]),
        ).run_once()

        self.assertEqual(ingested.status, "ingested:1")
        self.assertEqual(decided.status, "resolved")
        self.assertEqual(delivered.status, "reconciled_delivered")
        self.assertEqual(connector.remote_attempt_count, 1)
        event_key = scoped_key(
            "connector_event",
            connector.connector_id,
            event.external_message_id,
        )
        outbox_key = scoped_key("outbox", connector.connector_id, event_key)
        outbox = self.channel_store.row("channel_outbox", "outbox_key", outbox_key)
        assert outbox is not None
        self.assertEqual(outbox["state"], "succeeded")
        decision = json.loads(outbox["decision_json"])
        self.assertEqual(decision["intent"], "product_question")
        self.assertIn("¥39.99", outbox["body"])

    async def test_mobile_offline_retries_before_any_remote_attempt(self) -> None:
        event = SyntheticInboundEvent(
            external_message_id="mobile-message-1",
            conversation_id="mobile-conversation-1",
            customer_ref="CUST-1001",
            text="SYN-ORDER-1003 的物流到哪里了",
        )
        connector = self.mobile(event)
        await ConnectorIngestor(store=self.channel_store, connector=connector).poll_once()
        await ConversationWorker(store=self.channel_store, runtime=self.runtime).run_once()
        delivery = DeliveryWorker(
            store=self.channel_store,
            connectors=ConnectorRegistry([connector]),
        )

        connector.set_online(False)
        first = await delivery.run_once()
        self.assertEqual(first.status, "delivery_retry_scheduled")
        self.assertEqual(connector.remote_attempt_count, 0)

        connector.set_online(True)
        self.channel_store.connection.execute(
            "UPDATE channel_outbox SET next_attempt_at = 0 WHERE outbox_key = ?",
            (first.item_key,),
        )
        self.channel_store.connection.commit()
        second = await delivery.run_once()
        self.assertEqual(second.status, "delivered")
        self.assertEqual(connector.remote_attempt_count, 1)

    async def test_mobile_unknown_result_hands_off_without_blind_retry(self) -> None:
        event = SyntheticInboundEvent(
            external_message_id="mobile-message-unknown",
            conversation_id="mobile-conversation-unknown",
            customer_ref="CUST-1001",
            text="SYN-ORDER-1003 订单状态",
        )
        connector = self.mobile(event)
        connector.simulate_unknown_result_once()
        await ConnectorIngestor(store=self.channel_store, connector=connector).poll_once()
        await ConversationWorker(store=self.channel_store, runtime=self.runtime).run_once()
        delivery = DeliveryWorker(
            store=self.channel_store,
            connectors=ConnectorRegistry([connector]),
        )

        first = await delivery.run_once()
        second = await delivery.run_once()

        self.assertEqual(first.status, "ambiguous_handoff")
        self.assertEqual(second.status, "idle")
        self.assertEqual(connector.remote_attempt_count, 1)
        session_key = scoped_key(
            "session",
            "demo",
            "synthetic-mobile",
            "STORE-001",
            "CUST-1001",
        )
        self.assertEqual(self.channel_store.session_state(session_key), "human_active")

    async def test_customer_requested_handoff_parks_follow_up(self) -> None:
        first_event = SyntheticInboundEvent(
            external_message_id="handoff-message-1",
            conversation_id="handoff-conversation",
            customer_ref="CUST-0042",
            text="请帮我转人工客服",
        )
        connector = self.browser(first_event)
        await ConnectorIngestor(store=self.channel_store, connector=connector).poll_once()
        worker = ConversationWorker(store=self.channel_store, runtime=self.runtime)
        first = await worker.run_once()
        self.assertEqual(first.status, "human_handoff")

        connector.append_synthetic_event(
            SyntheticInboundEvent(
                external_message_id="handoff-message-2",
                conversation_id="handoff-conversation",
                customer_ref="CUST-0042",
                text="这是转人工后的补充消息",
            )
        )
        await ConnectorIngestor(store=self.channel_store, connector=connector).poll_once()
        second = await worker.run_once()
        self.assertEqual(second.status, "parked_for_human")

    async def test_decision_completion_crash_replays_cache_without_duplicate_turn(self) -> None:
        event = SyntheticInboundEvent(
            external_message_id="crash-message-1",
            conversation_id="crash-conversation-1",
            customer_ref="CUST-0042",
            text="SKU-MUG-BLUE 的材质",
        )
        connector = self.browser(event)
        await ConnectorIngestor(store=self.channel_store, connector=connector).poll_once()
        worker = ConversationWorker(store=self.channel_store, runtime=self.runtime)

        with patch.object(
            self.channel_store,
            "complete_with_reply",
            side_effect=RuntimeError("synthetic crash before outbox commit"),
        ):
            first = await worker.run_once()
        second = await worker.run_once()

        self.assertEqual(first.status, "decision_retry_scheduled")
        self.assertEqual(second.status, "resolved")
        session_key = scoped_key(
            "session",
            "demo",
            "synthetic-browser",
            "STORE-001",
            "CUST-0042",
        )
        turns = self.runtime.store.recent_turns(session_key, 10)
        self.assertEqual(len(turns), 2)
        row = self.runtime.store.connection.execute("SELECT COUNT(*) FROM turns").fetchone()
        assert row is not None
        self.assertEqual(int(row[0]), 2)
        event_key = scoped_key(
            "connector_event",
            connector.connector_id,
            event.external_message_id,
        )
        outbox_key = scoped_key("outbox", connector.connector_id, event_key)
        outbox = self.channel_store.row("channel_outbox", "outbox_key", outbox_key)
        assert outbox is not None
        decision = json.loads(outbox["decision_json"])
        self.assertTrue(decision["deduplicated"])

    async def test_connector_rebinding_cancels_delivery_before_remote_attempt(self) -> None:
        event = SyntheticInboundEvent(
            external_message_id="binding-message-1",
            conversation_id="binding-conversation-1",
            customer_ref="CUST-0042",
            text="SKU-MUG-BLUE 的材质",
        )
        original = self.browser(event)
        await ConnectorIngestor(store=self.channel_store, connector=original).poll_once()
        await ConversationWorker(store=self.channel_store, runtime=self.runtime).run_once()
        rebound = BrowserConnector(
            ConnectorBinding(
                connector_id=original.connector_id,
                tenant_id="demo",
                store_id="STORE-002",
                channel="synthetic-browser",
            )
        )

        result = await DeliveryWorker(
            store=self.channel_store,
            connectors=ConnectorRegistry([rebound]),
        ).run_once()

        self.assertEqual(result.status, "human_handoff")
        self.assertEqual(rebound.remote_attempt_count, 0)
        assert result.item_key is not None
        outbox = self.channel_store.row("channel_outbox", "outbox_key", result.item_key)
        assert outbox is not None
        self.assertEqual(outbox["state"], "cancelled")

    async def test_retryable_after_remote_attempt_is_not_blindly_retried(self) -> None:
        class UnsafeRetryMobile(MobileConnector):
            async def send(self, message: OutboundCommand) -> DeliveryAttempt:
                return DeliveryAttempt(
                    disposition=DeliveryDisposition.RETRYABLE,
                    idempotency_key=message.idempotency_key,
                    provider_operation_id=None,
                    safe_code="synthetic_retryable_after_attempt",
                    remote_attempted=True,
                )

        event = SyntheticInboundEvent(
            external_message_id="retry-after-attempt",
            conversation_id="retry-after-attempt-conversation",
            customer_ref="CUST-1001",
            text="SYN-ORDER-1003 订单状态",
        )
        connector = UnsafeRetryMobile(self.mobile(event).binding, (event,))
        await ConnectorIngestor(store=self.channel_store, connector=connector).poll_once()
        await ConversationWorker(store=self.channel_store, runtime=self.runtime).run_once()
        delivery = DeliveryWorker(
            store=self.channel_store,
            connectors=ConnectorRegistry([connector]),
        )

        first = await delivery.run_once()
        second = await delivery.run_once()

        self.assertEqual(first.status, "ambiguous_handoff")
        self.assertEqual(second.status, "idle")

    async def test_mismatched_delivery_key_cannot_confirm_an_outbox(self) -> None:
        class WrongKeyBrowser(BrowserConnector):
            async def send(self, message: OutboundCommand) -> DeliveryAttempt:
                return DeliveryAttempt(
                    disposition=DeliveryDisposition.CONFIRMED,
                    idempotency_key="wrong-delivery-key",
                    provider_operation_id="SYN-WRONG-OP",
                    safe_code="synthetic_wrong_key",
                    remote_attempted=True,
                )

        event = SyntheticInboundEvent(
            external_message_id="wrong-key-message",
            conversation_id="wrong-key-conversation",
            customer_ref="CUST-0042",
            text="SKU-MUG-BLUE 的材质",
        )
        connector = WrongKeyBrowser(self.browser(event).binding, (event,))
        await ConnectorIngestor(store=self.channel_store, connector=connector).poll_once()
        await ConversationWorker(store=self.channel_store, runtime=self.runtime).run_once()
        result = await DeliveryWorker(
            store=self.channel_store,
            connectors=ConnectorRegistry([connector]),
        ).run_once()

        self.assertEqual(result.status, "ambiguous_handoff")
        assert result.item_key is not None
        outbox = self.channel_store.row("channel_outbox", "outbox_key", result.item_key)
        assert outbox is not None
        self.assertEqual(outbox["state"], "unknown")

    async def test_tampered_outbox_payload_hash_is_cancelled_before_send(self) -> None:
        event = SyntheticInboundEvent(
            external_message_id="tampered-payload-message",
            conversation_id="tampered-payload-conversation",
            customer_ref="CUST-0042",
            text="SKU-MUG-BLUE 的材质",
        )
        connector = self.browser(event)
        await ConnectorIngestor(store=self.channel_store, connector=connector).poll_once()
        await ConversationWorker(store=self.channel_store, runtime=self.runtime).run_once()
        self.channel_store.connection.execute(
            "UPDATE channel_outbox SET payload_hash = 'tampered'",
        )
        self.channel_store.connection.commit()

        result = await DeliveryWorker(
            store=self.channel_store,
            connectors=ConnectorRegistry([connector]),
        ).run_once()

        self.assertEqual(result.status, "human_handoff")
        self.assertEqual(connector.remote_attempt_count, 0)
        assert result.item_key is not None
        outbox = self.channel_store.row("channel_outbox", "outbox_key", result.item_key)
        assert outbox is not None
        self.assertEqual(outbox["state"], "cancelled")

    async def test_handoff_cancels_queued_auto_reply_and_only_sends_ack(self) -> None:
        normal = SyntheticInboundEvent(
            external_message_id="before-handoff-message",
            conversation_id="handoff-barrier-conversation",
            customer_ref="CUST-0042",
            text="SKU-MUG-BLUE 的材质",
        )
        handoff = SyntheticInboundEvent(
            external_message_id="handoff-barrier-message",
            conversation_id="handoff-barrier-conversation",
            customer_ref="CUST-0042",
            text="请帮我转人工客服",
        )
        connector = BrowserConnector(self.browser(normal).binding, (normal, handoff))
        await ConnectorIngestor(store=self.channel_store, connector=connector).poll_once()
        worker = ConversationWorker(store=self.channel_store, runtime=self.runtime)
        self.assertEqual((await worker.run_once()).status, "resolved")
        claimed_auto_reply = self.channel_store.claim_outbox()
        self.assertIsNotNone(claimed_auto_reply)
        assert claimed_auto_reply is not None
        self.assertEqual((await worker.run_once()).status, "human_handoff")

        normal_event_key = scoped_key(
            "connector_event",
            connector.connector_id,
            normal.external_message_id,
        )
        normal_outbox_key = scoped_key("outbox", connector.connector_id, normal_event_key)
        normal_outbox = self.channel_store.row(
            "channel_outbox",
            "outbox_key",
            normal_outbox_key,
        )
        assert normal_outbox is not None
        self.assertEqual(normal_outbox["state"], "delivering")
        self.assertFalse(self.channel_store.delivery_allowed(claimed_auto_reply))
        self.channel_store.cancel_outbox(
            claimed_auto_reply,
            reason="session_fence_changed",
        )

        delivery = await DeliveryWorker(
            store=self.channel_store,
            connectors=ConnectorRegistry([connector]),
        ).run_once()
        self.assertEqual(delivery.status, "delivered")
        self.assertEqual(connector.remote_attempt_count, 1)


if __name__ == "__main__":
    unittest.main()
