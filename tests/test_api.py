from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dxl_agent.api import create_app  # noqa: E402
from dxl_agent.config import Settings  # noqa: E402
from dxl_agent.domain import IncomingMessage  # noqa: E402
from dxl_agent.runtime import AgentRuntime  # noqa: E402


class ApiTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="dxl-api-test-")
        self.addCleanup(self.temporary_directory.cleanup)
        settings = Settings(
            database_path=Path(self.temporary_directory.name) / "api.db",
            fixture_dir=PROJECT_ROOT / "demo" / "fixtures",
            policy_path=PROJECT_ROOT / "policies" / "default.toml",
            operator_key="test-operator-key",
        )
        self.runtime = AgentRuntime.from_settings(settings)
        self.addCleanup(self.runtime.store.connection.close)
        self.app = create_app(self.runtime)
        self.operator_headers = {"X-DXL-Operator-Key": "test-operator-key"}

    async def test_message_trace_approval_and_idempotent_execution(self) -> None:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            self.assertEqual((await client.get("/health")).status_code, 200)
            proposal_response = await client.post(
                "/v1/messages",
                json={
                    "tenant_id": "demo",
                    "channel": "sandbox",
                    "store_id": "STORE-001",
                    "customer_id": "CUST-0042",
                    "message_id": "api-refund-1",
                    "text": "SYN-ORDER-1001 请退款10元，原因：不想要",
                },
            )
            self.assertEqual(proposal_response.status_code, 200)
            proposal = proposal_response.json()
            self.assertEqual(proposal["status"], "pending_approval")
            action_id = proposal["pending_action"]["action_id"]

            unauthorized_trace = await client.get(f"/v1/traces/{proposal['trace_id']}")
            self.assertEqual(unauthorized_trace.status_code, 401)
            trace_response = await client.get(
                f"/v1/traces/{proposal['trace_id']}", headers=self.operator_headers
            )
            self.assertEqual(trace_response.status_code, 200)
            self.assertEqual(trace_response.json()["trace_id"], proposal["trace_id"])

            blocked = await client.post(
                f"/v1/actions/{action_id}/execute",
                headers={
                    **self.operator_headers,
                    "Idempotency-Key": "api-idem-0001",
                },
            )
            self.assertEqual(blocked.status_code, 409)

            approved = await client.post(
                f"/v1/actions/{action_id}/approve",
                json={"approver": "reviewer.demo"},
                headers=self.operator_headers,
            )
            self.assertEqual(approved.status_code, 200)
            self.assertEqual(approved.json()["state"], "approved")

            first = await client.post(
                f"/v1/actions/{action_id}/execute",
                headers={
                    **self.operator_headers,
                    "Idempotency-Key": "api-idem-0001",
                },
            )
            replay = await client.post(
                f"/v1/actions/{action_id}/execute",
                headers={
                    **self.operator_headers,
                    "Idempotency-Key": "api-idem-0001",
                },
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(replay.status_code, 200)
            self.assertFalse(first.json()["deduplicated"])
            self.assertTrue(replay.json()["deduplicated"])

    async def test_validation_and_missing_resources_fail_safely(self) -> None:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            invalid = await client.post(
                "/v1/messages",
                json={
                    "store_id": "STORE-001",
                    "customer_id": "CUST-0042",
                    "message_id": "",
                    "text": "hello",
                },
            )
            self.assertEqual(invalid.status_code, 422)
            invalid_evidence = await client.post(
                "/v1/messages",
                json={
                    "store_id": "STORE-001",
                    "customer_id": "CUST-1001",
                    "message_id": "invalid-evidence",
                    "text": "SYN-ORDER-1003 请退款3元，原因：商品破损",
                    "attachments": ["anything"],
                },
            )
            self.assertEqual(invalid_evidence.status_code, 422)
            missing = await client.get("/v1/traces/missing", headers=self.operator_headers)
            self.assertEqual(missing.status_code, 404)

    async def test_inflight_duplicate_returns_retryable_conflict(self) -> None:
        message = IncomingMessage(
            tenant_id="demo",
            channel="sandbox",
            store_id="STORE-001",
            customer_id="CUST-0042",
            message_id="inflight",
            text="你好",
        )
        token, response = self.runtime.store.claim_message(message.dedupe_key)
        self.assertIsNotNone(token)
        self.assertIsNone(response)
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            result = await client.post(
                "/v1/messages",
                json={
                    "store_id": "STORE-001",
                    "customer_id": "CUST-0042",
                    "message_id": "inflight",
                    "text": "你好",
                },
            )
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.headers["Retry-After"], "1")


if __name__ == "__main__":
    unittest.main()
