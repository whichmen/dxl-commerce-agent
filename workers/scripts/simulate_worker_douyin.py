#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interactive simulator for worker -> FastAPI decision flow.

Usage:
  cd /path/to/dxl-commerce-agent/workers
  python3 scripts/simulate_worker_douyin.py

This script does NOT send messages to platform UI.
It only simulates worker incoming events and prints decision responses.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import DecisionClient, IncomingMessage, now_ms  # noqa: E402


DEFAULT_DECISION_URL = os.environ.get("DECISION_URL", "http://127.0.0.1:18080")
DEFAULT_DECISION_KEY = os.environ.get("DECISION_KEY", "")
DEFAULT_TIMEOUT_SEC = 75.0
DEFAULT_TENANT_ID = os.environ.get("TENANT_ID", "tenant_example")

CHANNEL_CAPABILITIES = {
    "channel": "worker",
    "send_text": True,
    "send_image": True,
    "send_image_input": "image_url",
    "max_image_bytes": 8 * 1024 * 1024,
}


@dataclass
class TestContext:
    alias: str
    platform: str
    store_id: str
    store_name: str
    platform_nickname: str
    customer_id: str


# 公开版只保留虚构上下文；可按本地测试需要替换这些示例值。
TEST_CONTEXTS: List[TestContext] = [
    TestContext(
        alias="douyin_demo_a",
        platform="douyin",
        store_id="douyin_store_example",
        store_name="示例抖音店铺",
        platform_nickname="顾客甲",
        customer_id="sim_customer_a",
    ),
    TestContext(
        alias="douyin_demo_b",
        platform="douyin",
        store_id="douyin_store_example",
        store_name="示例抖音店铺",
        platform_nickname="顾客乙",
        customer_id="sim_customer_b",
    ),
    TestContext(
        alias="pdd_demo_a",
        platform="pdd",
        store_id="pdd_store_example",
        store_name="示例拼多多店铺",
        platform_nickname="顾客丙",
        customer_id="sim_customer_c",
    ),
    TestContext(
        alias="xiaohongshu_demo_a",
        platform="xiaohongshu",
        store_id="xiaohongshu_store_example",
        store_name="示例小红书店铺",
        platform_nickname="顾客丁",
        customer_id="sim_customer_d",
    ),
    TestContext(
        alias="weixin_demo_a",
        platform="weixin",
        store_id="weixin_store_example",
        store_name="示例视频号店铺",
        platform_nickname="顾客戊",
        customer_id="sim_customer_e",
    ),
    TestContext(
        alias="kuaishou_demo_a",
        platform="kuaishou",
        store_id="kuaishou_store_example",
        store_name="示例快手店铺",
        platform_nickname="顾客己",
        customer_id="sim_customer_f",
    ),
]


ORDER_PATTERNS = (
    # e.g. "订单号: P786444306705260121" or pure numeric ids.
    re.compile(
        r"订单(?:号|编号)?\s*[:：]?\s*([A-Za-z0-9](?=[A-Za-z0-9_-]{8,})(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{8,})",
        re.IGNORECASE,
    ),
    # general ids like P786..., 260220-404..., AAHt_Qu...
    re.compile(
        r"(?<![A-Za-z0-9_-])([A-Za-z0-9](?=[A-Za-z0-9_-]{8,})(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{8,})(?![A-Za-z0-9_-])"
    ),
    # fallback: long numeric ids
    re.compile(r"(?<![0-9])([0-9]{10,30})(?![0-9])"),
)
USE_CMD_RE = re.compile(r"^/(?:use|u)(?:\s+|\u3000)?(.*)$", re.IGNORECASE)


def extract_order_id(text: str) -> str:
    value = text or ""
    for pattern in ORDER_PATTERNS:
        match = pattern.search(value)
        if match:
            return str(match.group(1) or "").strip()
    return ""


def print_help() -> None:
    print(
        "\nCommands:\n"
        "  /list                     列出全部测试顾客\n"
        "  /use <alias|index|昵称>   切换当前顾客（支持 /u顾客乙、/use 顾客乙）\n"
        "  /show                     显示当前顾客上下文\n"
        "  /image <url> [text]       发送一条图片消息（可带文本）\n"
        "  /help                     显示帮助\n"
        "  /quit                     退出\n"
    )


def context_to_line(idx: int, ctx: TestContext, active: bool) -> str:
    mark = "*" if active else " "
    return (
        f"[{mark}] {idx}: alias={ctx.alias}, platform={ctx.platform}, "
        f"store_id={ctx.store_id}, store_name={ctx.store_name}, "
        f"nickname={ctx.platform_nickname}"
    )


def _normalize_token(token: str) -> str:
    return (token or "").replace("\u3000", " ").strip()


def _resolve_context_index(token: str) -> Optional[int]:
    key = _normalize_token(token)
    if not key:
        return None
    if key.isdigit():
        idx = int(key)
        if 0 <= idx < len(TEST_CONTEXTS):
            return idx
        return None
    key_lower = key.lower()

    # Exact match first.
    for i, ctx in enumerate(TEST_CONTEXTS):
        if key == ctx.alias:
            return i
        if key == ctx.platform_nickname:
            return i
        if key == ctx.store_id:
            return i
        if key == ctx.store_name:
            return i

    # Then case-insensitive alias/store_id contains.
    for i, ctx in enumerate(TEST_CONTEXTS):
        if key_lower in ctx.alias.lower():
            return i
        if key_lower in ctx.store_id.lower():
            return i
        if key in ctx.platform_nickname:
            return i
        if key in ctx.store_name:
            return i
    return None


def build_message(
    *,
    ctx: TestContext,
    text: str,
    media_url: str,
    seq: int,
    tenant_id: str,
) -> IncomingMessage:
    message_type = "image" if media_url else "text"
    order_id = extract_order_id(text)
    if (not order_id) and media_url:
        order_id = ""

    return IncomingMessage(
        tenant_id=tenant_id,
        platform=ctx.platform,
        store_id=ctx.store_id,
        store_name=ctx.store_name,
        customer_id=ctx.customer_id,
        platform_nickname=ctx.platform_nickname,
        message_id=f"sim_{ctx.alias}_{seq}_{uuid4().hex[:8]}",
        message_type=message_type,
        text=text,
        media_url=media_url,
        raw={
            "simulator": True,
            "channel_capabilities": dict(CHANNEL_CAPABILITIES, platform=ctx.platform),
            "order_id": order_id,
        },
        role="customer",
    )


async def run_repl(args: argparse.Namespace) -> None:
    if not TEST_CONTEXTS:
        raise RuntimeError("TEST_CONTEXTS is empty.")

    client = DecisionClient(
        base_url=args.decision_url,
        api_key=args.decision_key,
        timeout_sec=args.timeout_sec,
        fallback_text=args.fallback_text,
    )

    active_index = 0
    seq = 0

    print("=== Worker Simulator ===")
    print(f"decision_url={args.decision_url}, timeout={args.timeout_sec}s")
    print_help()
    for i, ctx in enumerate(TEST_CONTEXTS):
        print(context_to_line(i, ctx, i == active_index))

    while True:
        try:
            raw = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            return

        if not raw:
            continue
        if raw == "/quit":
            print("bye.")
            return
        if raw == "/help":
            print_help()
            continue
        if raw == "/list":
            for i, ctx in enumerate(TEST_CONTEXTS):
                print(context_to_line(i, ctx, i == active_index))
            continue
        if raw == "/show":
            print(context_to_line(active_index, TEST_CONTEXTS[active_index], True))
            continue
        use_match = USE_CMD_RE.match(raw)
        if use_match:
            token = _normalize_token(use_match.group(1))
            if not token:
                for i, ctx in enumerate(TEST_CONTEXTS):
                    print(context_to_line(i, ctx, i == active_index))
                continue
            selected = _resolve_context_index(token)
            if selected is None:
                print(f"invalid target: {token!r}")
                print("tip: /use 1  或 /use 顾客乙  或 /use douyin_demo_b")
                continue
            active_index = selected
            print(context_to_line(active_index, TEST_CONTEXTS[active_index], True))
            continue
        media_url = ""
        text = raw
        if raw.startswith("/image "):
            payload = raw[7:].strip()
            if not payload:
                print("usage: /image <url> [text]")
                continue
            parts = payload.split(maxsplit=1)
            media_url = parts[0].strip()
            text = parts[1].strip() if len(parts) > 1 else ""
            if not (media_url.startswith("http://") or media_url.startswith("https://")):
                print("image url must start with http:// or https://")
                continue

        seq += 1
        ctx = TEST_CONTEXTS[active_index]
        msg = build_message(
            ctx=ctx,
            text=text,
            media_url=media_url,
            seq=seq,
            tenant_id=args.tenant_id,
        )

        print(
            f"[send] alias={ctx.alias}, session={msg.session_key()}, "
            f"type={msg.message_type}, message_id={msg.message_id}"
        )
        if msg.text:
            print(f"[send] text={msg.text}")
        if msg.media_url:
            print(f"[send] media_url={msg.media_url}")

        decision = await client.decide(msg)
        print(f"[recv] action={decision.action}, reason={decision.reason}")

        sent_text = ""
        sent_images: List[str] = []
        if decision.reply_text:
            sent_text = decision.reply_text
            print(f"bot> {decision.reply_text}")
        if decision.reply_image_urls:
            sent_images = list(decision.reply_image_urls)
            for url in decision.reply_image_urls:
                print(f"bot_image> {url}")
        if (not sent_text) and (not sent_images):
            print("bot> (empty reply)")

        ack_ok = await client.ack_delivery(
            msg=msg,
            decision=decision,
            sent_text=sent_text,
            sent_image_urls=sent_images,
            status="sent" if decision.action == "send" else "skipped",
        )
        print(f"[ack] {'ok' if ack_ok else 'failed'}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate worker event flow.")
    parser.add_argument("--decision-url", default=DEFAULT_DECISION_URL)
    parser.add_argument("--decision-key", default=DEFAULT_DECISION_KEY)
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID)
    parser.add_argument("--fallback-text", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run_repl(args))


if __name__ == "__main__":
    main()
