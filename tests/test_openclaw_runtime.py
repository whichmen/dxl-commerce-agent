from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
WORKERS_ROOT = PROJECT_ROOT / "workers"
for source_root in (SRC_ROOT, WORKERS_ROOT):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from core.decision_client import DecisionClient  # noqa: E402
from core.models import DecisionResult, IncomingMessage  # noqa: E402
from kefu_clawbot.app import create_app  # noqa: E402
from kefu_clawbot.openclaw_agent import (  # noqa: E402
    OpenClawAgentInvoker,
    OpenClawReply,
)


class OpenClawRuntimeTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="dxl-openclaw-test-")
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.env = {
            "CLAWBOT_API_KEY": "test-decision-key",
            "CLAWBOT_TOOL_API_KEY": "test-tool-key",
            "CLAWBOT_POLICY_FILE": str(PROJECT_ROOT / "config" / "policy.yaml"),
            "CLAWBOT_DB_PATH": str(root / "clawbot.db"),
            "CLAWBOT_MEDIA_ROOT": str(root / "media"),
            "CLAWBOT_EXPORT_ROOT": str(root / "exports"),
            "CLAWBOT_OPENCLAW_BIN": "openclaw",
            "CLAWBOT_OPENCLAW_AGENT_ID": "kefu_ops",
            "CLAWBOT_KUAIMAI_ERP_HOST": "https://erp.example.com",
            "CLAWBOT_KUAIMAI_COMPANY_NAME": "",
            "CLAWBOT_KUAIMAI_ACCOUNT": "",
            "CLAWBOT_KUAIMAI_PASSWORD": "",
            "CLAWBOT_IDENTITY_BASE_URL": "",
            "CLAWBOT_WECOM_ENABLED": "0",
            "CLAWBOT_ADB_CAPTURE_ENABLED": "0",
            "CLAWBOT_IMAGE_INSPECT_ENABLED": "0",
            "CLAWBOT_ADMIN_TOOLS_ENABLED": "0",
            "CLAWBOT_ENABLE_FILE_READ_TOOL": "0",
            "CLAWBOT_ENABLE_SHELL_EXEC_TOOL": "0",
            "CLAWBOT_FILE_READ_ROOTS": "",
            "CLAWBOT_SHELL_ALLOWED_COMMANDS": "",
            "DECISION_ALERT_ENABLED": "0",
        }

    @staticmethod
    def event(message_id: str = "message-001") -> dict[str, object]:
        return {
            "tenant_id": "tenant_example",
            "platform": "douyin",
            "store_id": "douyin_store_example",
            "store_name": "示例抖音店铺",
            "customer_id": "customer_example",
            "platform_nickname": "guest_example",
            "message_id": message_id,
            "message_type": "text",
            "text": "请问订单什么时候发货？",
            "raw": {},
        }

    async def test_agent_is_default_path_and_duplicate_uses_cache(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_run_prompt(
            invoker: OpenClawAgentInvoker,
            *,
            session_key: str,
            prompt: str,
            attachments: list[dict[str, str]] | None = None,
        ) -> OpenClawReply:
            self.assertFalse(attachments)
            calls.append((session_key, prompt))
            return OpenClawReply(
                text="可以的，我先帮您核对订单和发货进度，请稍等。",
                session_key=session_key,
            )

        with patch.dict("os.environ", self.env, clear=True), patch.object(
            OpenClawAgentInvoker,
            "run_prompt",
            fake_run_prompt,
        ):
            app = create_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                health = await client.get("/health")
                first = await client.post(
                    "/v1/decide",
                    json={"event": self.event()},
                    headers={"X-Api-Key": "test-decision-key"},
                )
                replay = await client.post(
                    "/v1/decide",
                    json={"event": self.event()},
                    headers={"X-Api-Key": "test-decision-key"},
                )
                ack_event = self.event()
                ack_event["session_key"] = "douyin:douyin_store_example:guest_example"
                ack_payload = {
                    "event": ack_event,
                    "trace_id": first.json()["extra"]["trace_id"],
                    "status": "sent",
                    "sent_text": first.json()["reply_text"],
                    "sent_at_ms": 1_700_000_000_000,
                }
                first_ack = await client.post(
                    "/v1/worker/ack",
                    json=ack_payload,
                    headers={"X-Api-Key": "test-decision-key"},
                )
                replay_ack = await client.post(
                    "/v1/worker/ack",
                    json=ack_payload,
                    headers={"X-Api-Key": "test-decision-key"},
                )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(health.json()["decision_mode"], "agent")
        self.assertEqual(first.json()["reply_text"], "可以的，我先帮您核对订单和发货进度，请稍等。")
        self.assertEqual(first.json()["reason"], "agent_single_reply")
        self.assertEqual(first.json()["extra"]["strategy"], "agent_single")
        self.assertIn("请按你已加载的系统规则与技能处理这条消息", calls[0][1])
        self.assertEqual(len(calls), 1)
        self.assertTrue(replay.json()["extra"]["cache_hit"])
        self.assertTrue(first_ack.json()["ok"])
        self.assertTrue(replay_ack.json()["ok"])
        history = app.state.service.store.recent_dialogue(
            "douyin:douyin_store_example:guest_example"
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["agent"], first.json()["reply_text"])

    async def test_policy_mode_is_only_an_explicit_fallback(self) -> None:
        env = {**self.env, "CLAWBOT_DECISION_MODE": "policy"}
        with patch.dict("os.environ", env, clear=False):
            app = create_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/v1/decide",
                    json={"event": self.event("message-policy")},
                    headers={"X-Api-Key": "test-decision-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "send")
        self.assertNotEqual(response.json()["reason"], "agent_single_reply")

    async def test_tool_auth_admin_gate_and_delivery_ack(self) -> None:
        env = {**self.env, "CLAWBOT_DECISION_MODE": "policy"}
        event = self.event("message-ack")
        event["session_key"] = "douyin:douyin_store_example:guest_example"
        with patch.dict("os.environ", env, clear=False):
            app = create_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                denied = await client.post(
                    "/v1/tools/file_read",
                    json={"args": {"path": "/tmp/example.txt"}, "event": event},
                )
                gated = await client.post(
                    "/v1/tools/file_read",
                    json={"args": {"path": "/tmp/example.txt"}, "event": event},
                    headers={"X-Api-Key": "test-tool-key"},
                )
                ack = await client.post(
                    "/v1/worker/ack",
                    json={
                        "event": event,
                        "trace_id": "trace-example",
                        "status": "sent",
                        "sent_text": "已发送的示例回复",
                    },
                    headers={"X-Api-Key": "test-decision-key"},
                )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(gated.status_code, 200)
        self.assertEqual(gated.json()["message"], "admin_tools_disabled")
        self.assertEqual(ack.json()["message"], "ack_recorded")

    async def test_file_read_flag_does_not_enable_shell_endpoint(self) -> None:
        allowed_root = Path(self.temp_dir.name) / "allowed-files"
        allowed_root.mkdir()
        target = allowed_root / "report.txt"
        target.write_text("safe report", encoding="utf-8")
        env = {
            **self.env,
            "CLAWBOT_DECISION_MODE": "policy",
            "CLAWBOT_ENABLE_FILE_READ_TOOL": "1",
            "CLAWBOT_FILE_READ_ROOTS": str(allowed_root),
            "CLAWBOT_ENABLE_SHELL_EXEC_TOOL": "0",
            "CLAWBOT_SHELL_ALLOWED_COMMANDS": "*",
        }
        with patch.dict("os.environ", env, clear=False):
            app = create_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                read = await client.post(
                    "/v1/tools/file_read",
                    json={"args": {"path": str(target)}},
                    headers={"X-Api-Key": "test-tool-key"},
                )
                shell = await client.post(
                    "/v1/tools/shell_exec",
                    json={"args": {"command": "true"}},
                    headers={"X-Api-Key": "test-tool-key"},
                )

        self.assertTrue(read.json()["ok"])
        self.assertEqual(read.json()["data"]["content"], "safe report")
        self.assertEqual(shell.json()["message"], "admin_tools_disabled")

    def test_openclaw_cli_receives_agent_session_and_prompt(self) -> None:
        payload = {"payloads": [{"text": "这是模型最终生成的回复。"}]}
        completed = CompletedProcess(
            args=["openclaw"],
            returncode=0,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
        )
        invoker = OpenClawAgentInvoker(
            openclaw_bin="openclaw",
            agent_id="kefu_ops",
            timeout_sec=10,
            use_local=True,
        )
        with patch("kefu_clawbot.openclaw_agent.subprocess.run", return_value=completed) as run:
            reply = invoker.run_prompt(
                session_key="agent:kefu_ops:douyin:store:guest",
                prompt="读取上下文并处理这条客服消息",
            )

        self.assertEqual(reply.text, "这是模型最终生成的回复。")
        command = run.call_args.args[0]
        params = json.loads(command[command.index("--params") + 1])
        self.assertEqual(params["agentId"], "kefu_ops")
        self.assertEqual(params["sessionKey"], "agent:kefu_ops:douyin:store:guest")
        self.assertEqual(params["message"], "读取上下文并处理这条客服消息")

    async def test_worker_decide_and_ack_protocol(self) -> None:
        message = IncomingMessage(
            tenant_id="tenant_example",
            platform="pdd",
            store_id="pdd_store_example",
            store_name="示例拼多多店铺",
            customer_id="customer_example",
            platform_nickname="guest_example",
            message_id="worker-message-001",
            message_type="text",
            text="物流到哪里了？",
        )
        client = DecisionClient("http://127.0.0.1:18080", api_key="test-key")
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_post(path: str, payload: dict[str, object]) -> dict[str, object]:
            calls.append((path, payload))
            if path == "/v1/decide":
                return {
                    "action": "send",
                    "reply_text": "我帮您查一下物流进度。",
                    "reason": "agent_single_reply",
                    "extra": {"trace_id": "trace-worker-example"},
                }
            return {"ok": True, "message": "ack_recorded"}

        with patch.dict("os.environ", {"DECISION_ALERT_ENABLED": "0"}, clear=False), patch.object(
            client,
            "_post_json",
            side_effect=fake_post,
        ):
            decision = await client.decide(message)
            acknowledged = await client.ack_delivery(
                msg=message,
                decision=decision,
                sent_text=decision.reply_text,
                sent_image_urls=[],
            )

        self.assertIsInstance(decision, DecisionResult)
        self.assertEqual(decision.reason, "agent_single_reply")
        self.assertTrue(acknowledged)
        self.assertEqual([path for path, _ in calls], ["/v1/decide", "/v1/worker/ack"])

    async def test_worker_rejects_negative_ack_response(self) -> None:
        message = IncomingMessage(
            tenant_id="tenant_example",
            platform="pdd",
            store_id="pdd_store_example",
            store_name="示例拼多多店铺",
            customer_id="customer_example",
            platform_nickname="guest_example",
            message_id="worker-message-negative-ack",
            message_type="text",
            text="测试回执",
        )
        client = DecisionClient("http://127.0.0.1:18080", api_key="test-key")
        with patch.object(client, "_post_json", return_value={"ok": False}):
            acknowledged = await client.ack_delivery(
                msg=message,
                decision=DecisionResult(
                    action="send",
                    reply_text="示例回复",
                    extra={"trace_id": "trace-negative-ack"},
                ),
                sent_text="示例回复",
                sent_image_urls=[],
            )
        self.assertFalse(acknowledged)


if __name__ == "__main__":
    unittest.main()
