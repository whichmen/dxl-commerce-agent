from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from .channel_store import ChannelStore
from .config import Settings
from .connectors import (
    BrowserConnector,
    ConnectorBinding,
    MobileConnector,
    SyntheticInboundEvent,
)
from .runtime import AgentRuntime
from .workers import (
    ConnectorIngestor,
    ConnectorRegistry,
    ConnectorSupervisor,
    ConversationWorker,
    DeliveryWorker,
)


async def run_demo() -> dict[str, Any]:
    """Run an isolated browser + mobile customer-service pipeline."""

    with tempfile.TemporaryDirectory(prefix="dxl-channel-demo-") as directory:
        database = Path(directory) / "demo.db"
        settings = replace(
            Settings.from_env(),
            database_path=database,
            planner_mode="rules",
            demo_mode=True,
            operator_key=None,
        )
        runtime = AgentRuntime.from_settings(settings)
        channel_store = ChannelStore(database)
        browser = BrowserConnector(
            ConnectorBinding(
                connector_id="browser-demo",
                tenant_id="demo",
                store_id="STORE-001",
                channel="synthetic-browser",
            ),
            (
                SyntheticInboundEvent(
                    external_message_id="SYN-BROWSER-MESSAGE-0001",
                    conversation_id="SYN-BROWSER-CONVERSATION-0001",
                    customer_ref="CUST-0042",
                    text="SKU-MUG-BLUE 的材质和价格",
                ),
            ),
        )
        mobile = MobileConnector(
            ConnectorBinding(
                connector_id="mobile-demo",
                tenant_id="demo",
                store_id="STORE-001",
                channel="synthetic-mobile",
            ),
            (
                SyntheticInboundEvent(
                    external_message_id="SYN-MOBILE-MESSAGE-0001",
                    conversation_id="SYN-MOBILE-CONVERSATION-0001",
                    customer_ref="CUST-1001",
                    text="SYN-ORDER-1003 的物流到哪里了",
                ),
            ),
        )
        registry = ConnectorRegistry([browser, mobile])
        try:
            ingestion = [
                await ConnectorIngestor(store=channel_store, connector=browser).poll_once(),
                await ConnectorIngestor(store=channel_store, connector=mobile).poll_once(),
            ]
            conversation_worker = ConversationWorker(store=channel_store, runtime=runtime)
            decisions = [
                await conversation_worker.run_once(),
                await conversation_worker.run_once(),
            ]

            browser.simulate_timeout_after_commit_once()
            delivery_worker = DeliveryWorker(store=channel_store, connectors=registry)
            deliveries = [
                await delivery_worker.run_once(),
                await delivery_worker.run_once(),
            ]
            health = await ConnectorSupervisor(registry).check(
                [browser.connector_id, mobile.connector_id]
            )
            counts = channel_store.connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM channel_inbox WHERE state = 'completed') AS inbox,
                  (SELECT COUNT(*) FROM channel_outbox WHERE state = 'succeeded') AS outbox
                """
            ).fetchone()
            if counts is None:
                raise RuntimeError("Unable to read demo pipeline counts")
            return {
                "mode": "fully_synthetic_clean_room",
                "ingestion": [item.status for item in ingestion],
                "decisions": [item.status for item in decisions],
                "deliveries": [item.status for item in deliveries],
                "completed_inbox": int(counts["inbox"]),
                "succeeded_outbox": int(counts["outbox"]),
                "browser_remote_attempts": browser.remote_attempt_count,
                "mobile_remote_attempts": mobile.remote_attempt_count,
                "health": health,
            }
        finally:
            channel_store.connection.close()
            runtime.store.connection.close()


def main() -> None:
    print(json.dumps(asyncio.run(run_demo()), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
