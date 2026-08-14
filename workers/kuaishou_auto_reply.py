#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kuaishou (快手小店) worker with local dedup + Ubuntu decision API.

Flow:
1) find pending conversation (only:
   - 请在3分钟内回复
   - 已超时)
2) open conversation and extract latest customer message
3) dedup and session gating
4) call Ubuntu decision service
5) send decision reply back to chat
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from playwright.async_api import (
    Browser,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

try:
    from .core import DecisionClient, IncomingMessage, LocalStateStore, now_ms
    from .core.worker_health import (
        CATEGORY_FATAL_LOCAL_STATE,
        CATEGORY_MANUAL_ACTION,
        WorkerFatalError,
        WorkerHealthReporter,
        classify_worker_exception,
    )
except ImportError:  # Direct script execution from workers/
    from core import DecisionClient, IncomingMessage, LocalStateStore, now_ms
    from core.worker_health import (
        CATEGORY_FATAL_LOCAL_STATE,
        CATEGORY_MANUAL_ACTION,
        WorkerFatalError,
        WorkerHealthReporter,
        classify_worker_exception,
    )

DEFAULT_PORTS = (9225,)
DEFAULT_STORE_ID = os.environ.get("STORE_ID", "kuaishou_store_example")
DEFAULT_STORE_NAME = os.environ.get("STORE_NAME", "示例快手店铺")
DEFAULT_DECISION_URL = os.environ.get("DECISION_URL", "http://127.0.0.1:18080")
DEFAULT_DECISION_KEY = os.environ.get("DECISION_KEY", "")
DEFAULT_DECISION_TIMEOUT_SEC = 75.0
DEFAULT_STATE_DB = str((Path(__file__).resolve().parent / "state" / "kuaishou_worker.db"))
DEFAULT_FALLBACK_REPLY = "稍等"

PENDING_GROUP_NAMES = (
    "请在3分钟内回复",
    "已超时",
)

GROUP_ITEM_SELECTOR = ".SessionListGroupItem"
GROUP_NAME_SELECTOR = ".SessionListGroupItem-groupName"
GROUP_COUNT_SELECTOR = ".SessionListGroupItem-count"
SESSION_CARD_SELECTOR = ".SessionBaseCard"
SESSION_NAME_SELECTOR = ".SessionBaseCard-topHeadName"

MESSAGE_ROW_SELECTOR = ".kwaishop-cs-LayoutDefaultWrapper_msgMain"
MESSAGE_BEFORE_SELECTOR = ".kwaishop-cs-LayoutDefaultWrapper_beforeMsgBody"
MESSAGE_BODY_SELECTOR = ".kwaishop-cs-LayoutDefaultWrapper_msgBody"
MESSAGE_MIDDLE_BODY_SELECTOR = ".kwaishop-cs-LayoutMiddleWrapper_msgBody"

EDITOR_SELECTOR = "div.ql-editor"
SEND_BUTTON_SELECTOR = "button.ant-btn.ant-btn-primary"

POPUP_SELECTORS = [
    ".ant-modal",
    ".ant-modal-root",
    ".ant-message",
]
POPUP_CONFIRM_BUTTONS = [
    ".ant-modal button.ant-btn-primary",
    ".ant-modal button.ant-btn-dangerous",
    ".ant-modal button.ant-btn",
    ".ant-message button",
]

CHANNEL_CAPABILITIES = {
    "channel": "worker",
    "platform": "kuaishou",
    "send_text": True,
    "send_image": False,
    "send_image_input": "none",
}

SYSTEM_TEXT_MARKERS = (
    "[系统消息]",
    "[系统自动回复消息]",
    "客服超时未回复，会话关闭",
    "买家超时未回复，会话关闭",
    "【防诈贴士】",
)

_PENDING_CURSOR = 0


def _log(msg: str) -> None:
    print(msg, flush=True)


def _score_page_url(url: str) -> int:
    if not url:
        return 0
    if "im.kwaixiaodian.com/workbench" in url:
        return 120
    if "im.kwaixiaodian.com" in url:
        return 100
    if "kwaixiaodian.com" in url:
        return 40
    return 0


async def _pick_target_page(browser: Browser) -> Optional[Page]:
    best_page: Optional[Page] = None
    best_score = -1
    for context in browser.contexts:
        for page in context.pages:
            score = _score_page_url(page.url or "")
            if score > best_score:
                best_page = page
                best_score = score
    return best_page if best_score > 0 else None


async def _collect_browsers(
    playwright: Playwright,
    ports: Iterable[int],
) -> List[Tuple[Browser, Page]]:
    pairs: List[Tuple[Browser, Page]] = []
    for port in ports:
        endpoint = f"http://127.0.0.1:{port}"
        _log(f"[INFO] 尝试连接 CDP 端口 {port} ({endpoint}) ...")
        try:
            browser = await playwright.chromium.connect_over_cdp(endpoint)
        except Exception as exc:
            _log(f"[WARN] 端口 {port} 连接失败: {type(exc).__name__}: {exc}")
            continue

        page = await _pick_target_page(browser)
        if page is None:
            urls: List[str] = []
            for context in browser.contexts:
                for p in context.pages:
                    u = (p.url or "").strip()
                    if u:
                        urls.append(u)
            if urls:
                _log(f"[WARN] 端口 {port} 已连接，但未找到快手客服页。当前标签页: {' | '.join(urls[:5])}")
            else:
                _log(f"[WARN] 端口 {port} 已连接，但浏览器无可用标签页。")
            _disconnect_browser(browser)
            continue

        _log(f"[INFO] 端口 {port} 绑定页面: {page.url}")
        pairs.append((browser, page))
    return pairs


def _normalize_nickname(value: str) -> str:
    text = (value or "").replace("\u200b", "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*下单数\(\d+\)\s*$", "", text).strip()
    return text[:64]


def _extract_order_id_from_text(text: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    patterns = (
        r"(?:订单号|订单编号|平台单号)\s*[:：]?\s*([A-Za-z0-9_-]{8,})",
        r"\b([A-Za-z][0-9]{12,}|[0-9]{12,}|[0-9]{6,}-[0-9]{6,})\b",
    )
    for pattern in patterns:
        m = re.search(pattern, value)
        if m:
            return (m.group(1) or "").strip()
    return ""


def _is_platform_system_text(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    return any(marker in value for marker in SYSTEM_TEXT_MARKERS)


def _parse_group_count(text: str) -> int:
    m = re.search(r"\((\d+)\)", text or "")
    if not m:
        return 0
    try:
        return int(m.group(1))
    except Exception:
        return 0


def _parse_sender(before_text: str) -> str:
    # e.g. "测试顾客2-22 18:55:20" -> "测试顾客"
    value = (before_text or "").strip()
    if not value:
        return ""
    value = re.sub(r"\d{1,2}-\d{1,2}\s+\d{2}:\d{2}:\d{2}\s*$", "", value).strip()
    return _normalize_nickname(value)


def _looks_like_order_card(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    keywords = (
        "订单编号",
        "订单号",
        "查看订单",
        "发送订单",
        "买家正在查看订单",
        "买家正在查看商品",
    )
    return any(k in value for k in keywords)


async def _find_pending_conversation(page: Page) -> Optional[Locator]:
    global _PENDING_CURSOR
    pending_cards: List[Locator] = []

    for group_name in PENDING_GROUP_NAMES:
        group = page.locator(
            f"{GROUP_ITEM_SELECTOR}:has({GROUP_NAME_SELECTOR}:has-text('{group_name}'))"
        ).first
        if await group.count() <= 0:
            continue

        try:
            title = group.locator(".SessionListGroupItem-title").first
            if await title.count() > 0:
                await title.click(timeout=1200)
                await page.wait_for_timeout(120)
        except Exception:
            pass

        count_text = ""
        try:
            count_text = (await group.locator(GROUP_COUNT_SELECTOR).first.inner_text()).strip()
        except Exception:
            count_text = ""
        if _parse_group_count(count_text) <= 0:
            continue

        cards = group.locator(SESSION_CARD_SELECTOR)
        card_count = await cards.count()
        for i in range(card_count):
            card = cards.nth(i)
            try:
                if await card.is_visible():
                    pending_cards.append(card)
            except Exception:
                continue

    if not pending_cards:
        return None

    idx = _PENDING_CURSOR % len(pending_cards)
    _PENDING_CURSOR += 1
    return pending_cards[idx]


async def _wait_for_popup_and_confirm(page: Page) -> None:
    for _ in range(3):
        try:
            await page.wait_for_selector(", ".join(POPUP_SELECTORS), timeout=1000)
        except PlaywrightTimeoutError:
            return

        clicked = False
        for btn_selector in POPUP_CONFIRM_BUTTONS:
            button = await page.query_selector(btn_selector)
            if button and await button.is_visible():
                try:
                    await button.click()
                    clicked = True
                    await page.wait_for_timeout(200)
                    break
                except Exception:
                    continue
        if not clicked:
            break


async def _extract_customer_identity(convo: Locator, page: Page) -> Tuple[str, str, str]:
    nickname = ""
    name_node = convo.locator(SESSION_NAME_SELECTOR).first
    if await name_node.count() > 0:
        try:
            nickname = _normalize_nickname(await name_node.inner_text())
        except Exception:
            nickname = ""
    if not nickname:
        try:
            text = (await convo.inner_text()).strip()
            nickname = _normalize_nickname(text.splitlines()[0] if text else "")
        except Exception:
            nickname = ""
    if not nickname:
        raise ValueError("platform_nickname_required")

    source_ref = nickname
    customer_id = hashlib.sha1(f"kuaishou:{nickname}".encode("utf-8")).hexdigest()[:16]
    return source_ref, customer_id, nickname


def _build_message_id(
    session_key: str,
    msg_text: str,
    msg_id_hint: str,
    media_url: str = "",
) -> str:
    if msg_id_hint:
        return msg_id_hint
    seed = f"{session_key}|{msg_text}|{media_url}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]


async def _extract_latest_customer_message(
    page: Page,
    customer_nickname: str,
    skip_message_ids: Optional[set[str]] = None,
) -> Tuple[str, str, str, str]:
    skip_ids = skip_message_ids or set()
    bodies = page.locator(f"{MESSAGE_BODY_SELECTOR}, {MESSAGE_MIDDLE_BODY_SELECTOR}")
    count = await bodies.count()
    if count <= 0:
        return "", "", "", ""

    candidates: list[dict] = []
    start = max(0, count - 40)
    for idx in range(count - 1, start - 1, -1):
        body_node = bodies.nth(idx)
        if await body_node.count() <= 0:
            continue

        cls = ""
        try:
            cls = (await body_node.get_attribute("class") or "").strip()
        except Exception:
            cls = ""

        text = ""
        try:
            text = (await body_node.inner_text()).strip()
        except Exception:
            text = ""

        media_url = ""
        imgs = body_node.locator("img")
        img_count = await imgs.count()
        for i in range(img_count):
            img = imgs.nth(i)
            try:
                src = (await img.get_attribute("src") or "").strip()
            except Exception:
                src = ""
            if src:
                media_url = src
                break

        if not text and not media_url:
            continue

        msg_id = ""
        for target in (body_node,):
            for attr in ("id", "data-id", "data-key", "data-message-id"):
                try:
                    value = await target.get_attribute(attr)
                except Exception:
                    value = None
                if value:
                    msg_id = value.strip()
                    if msg_id:
                        break
            if msg_id:
                break
        if not msg_id:
            msg_id = hashlib.sha1(f"{idx}|{text}|{media_url}".encode("utf-8")).hexdigest()[:16]

        if msg_id in skip_ids:
            continue

        is_customer = False
        is_order_card = False
        before_text = ""

        if "LayoutDefaultWrapper_msgBody" in cls:
            row = body_node.locator("xpath=ancestor::*[contains(@class,'kwaishop-cs-LayoutDefaultWrapper_msgMain')][1]").first
            if await row.count() > 0:
                before_node = row.locator(MESSAGE_BEFORE_SELECTOR).first
                if await before_node.count() > 0:
                    try:
                        before_text = (await before_node.inner_text()).strip()
                    except Exception:
                        before_text = ""
            sender = _parse_sender(before_text)
            is_customer = sender == customer_nickname
        elif "LayoutMiddleWrapper_msgBody" in cls:
            is_order_card = _looks_like_order_card(text)
            is_customer = is_order_card

        if not is_customer:
            continue
        if _is_platform_system_text(text):
            continue

        message_type = "image" if media_url else "text"
        order_id = _extract_order_id_from_text(text)

        score = 0
        if order_id:
            score += 100
        if is_order_card:
            score += 80
        if media_url:
            score += 50
        if text:
            score += min(len(text), 120) // 20

        candidates.append(
            {
                "idx": idx,
                "score": score,
                "text": text,
                "msg_id": msg_id,
                "message_type": message_type,
                "media_url": media_url,
                "order_id": order_id,
                "is_order_card": is_order_card,
            }
        )

    if not candidates:
        return "", "", "", ""

    candidates.sort(key=lambda x: (x["score"], x["idx"]), reverse=True)
    selected = candidates[0]

    # 快手有时会把“问题文本 + 订单卡片”拆成两条，优先补上更近的一条文本消息。
    selected_text = str(selected["text"] or "").strip()
    if selected.get("order_id"):
        newer_texts = [
            c
            for c in candidates
            if c["idx"] > selected["idx"]
            and c["text"]
            and not c.get("order_id")
            and not c.get("is_order_card")
        ]
        if newer_texts:
            newer_texts.sort(key=lambda x: x["idx"], reverse=True)
            lead = str(newer_texts[0]["text"]).strip()
            if lead and lead not in selected_text:
                selected_text = f"{lead}\n{selected_text}".strip()

    return (
        selected_text[:800],
        str(selected["msg_id"]),
        str(selected["message_type"]),
        str(selected["media_url"]),
    )



async def _send_text_reply(page: Page, editor: Locator, reply_text: str) -> bool:
    try:
        await editor.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(reply_text)
        await page.wait_for_timeout(120)
    except Exception:
        return False

    buttons = page.locator(SEND_BUTTON_SELECTOR)
    count = await buttons.count()
    for i in range(count):
        btn = buttons.nth(i)
        try:
            if not await btn.is_visible():
                continue
            text = (await btn.inner_text()).strip()
            cls = (await btn.get_attribute("class") or "").lower()
        except Exception:
            continue
        if "发送" not in text:
            continue
        if "disabled" in cls:
            continue
        try:
            await btn.click(timeout=2000)
            await _wait_for_popup_and_confirm(page)
            await page.wait_for_timeout(400)
            return True
        except Exception:
            continue

    try:
        await editor.press("Enter")
        await _wait_for_popup_and_confirm(page)
        await page.wait_for_timeout(400)
        return True
    except Exception:
        return False


async def monitor_single_browser(
    page: Page,
    decision_client: DecisionClient,
    state_store: LocalStateStore,
    health_reporter: WorkerHealthReporter,
    tenant_id: str,
    store_id: str,
    store_name: str,
    min_reply_interval_ms: int,
) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(700)
        health_reporter.ready("page_attached")

        while True:
            try:
                body_text = await page.evaluate(
                    "() => (document.body && document.body.innerText || '').slice(0, 1200)"
                )
            except Exception:
                body_text = ""
            if "登录信息已经失效" in body_text:
                health_reporter.manual_action("登录信息已经失效")
                _log("[WARN] 快手登录已失效，请在页面重新登录。")
                await page.wait_for_timeout(5000)
                continue

            convo = await _find_pending_conversation(page)
            if convo is None:
                health_reporter.ready("idle")
                _log("没有待回复（请在3分钟内回复/已超时）的会话，等待中...")
                await page.wait_for_timeout(3000)
                continue

            try:
                await convo.scroll_into_view_if_needed(timeout=1200)
                await convo.click(timeout=3000, force=True)
                await page.wait_for_timeout(350)
            except Exception as exc:
                _log(f"[WARN] 会话点击失败，跳过本轮: {type(exc).__name__}")
                await page.wait_for_timeout(800)
                continue

            editor = page.locator(EDITOR_SELECTOR).first
            if await editor.count() <= 0:
                _log("[WARN] 未找到快手输入框，跳过本轮。")
                await page.wait_for_timeout(1000)
                continue

            try:
                source_ref, customer_id, platform_nickname = await _extract_customer_identity(convo, page)
            except Exception as exc:
                _log(f"[WARN] 顾客身份提取失败，跳过本轮: {type(exc).__name__}")
                await page.wait_for_timeout(900)
                continue

            session_key = f"kuaishou:{store_id}:{platform_nickname}"

            event: Optional[IncomingMessage] = None
            scanned_duplicate_ids: set[str] = set()
            for _ in range(6):
                message_text, message_id_hint, message_type, media_url = await _extract_latest_customer_message(
                    page,
                    customer_nickname=platform_nickname,
                    skip_message_ids=scanned_duplicate_ids,
                )
                if not message_text and not media_url:
                    break

                message_id = _build_message_id(
                    session_key,
                    message_text,
                    message_id_hint,
                    media_url=media_url,
                )
                extracted_order_id = _extract_order_id_from_text(message_text)
                candidate = IncomingMessage(
                    tenant_id=tenant_id,
                    platform="kuaishou",
                    store_id=store_id,
                    store_name=store_name or store_id,
                    customer_id=customer_id,
                    platform_nickname=platform_nickname,
                    message_id=message_id,
                    message_type=message_type or "text",
                    text=message_text,
                    media_url=media_url,
                    raw={
                        "page_url": page.url,
                        "message_id_hint": message_id_hint,
                        "channel_capabilities": dict(CHANNEL_CAPABILITIES),
                        "order_id": extracted_order_id,
                    },
                    role="customer",
                )
                if not state_store.mark_incoming_if_new(candidate):
                    scanned_duplicate_ids.add(message_id)
                    continue
                event = candidate
                break

            if event is None:
                if scanned_duplicate_ids:
                    _log(f"[INFO][{session_key}] 最近消息均已处理，跳过。")
                else:
                    _log(f"[INFO][{session_key}] 未提取到新顾客消息，跳过。")
                health_reporter.ready("idle")
                await page.wait_for_timeout(1000)
                continue

            health_reporter.ready("processing", progress=True)
            _log(
                f"[INFO][{event.session_key()}] 收到消息 type={event.message_type}, "
                f"id={event.message_id}, text={event.text[:40]!r}, "
                f"media={'yes' if bool(event.media_url) else 'no'}"
            )

            current_mode = state_store.get_mode(event)
            if current_mode != "auto":
                _log(f"[INFO][{event.session_key()}] 当前为 {current_mode} 模式，跳过自动回复。")
                await page.wait_for_timeout(1000)
                continue

            now_ts = now_ms()
            if not state_store.can_auto_reply(event, now_ts, min_reply_interval_ms):
                state_store.unmark_incoming(event)
                _log(f"[INFO][{event.session_key()}] 回复节流生效，跳过。")
                await page.wait_for_timeout(1000)
                continue

            decision = await decision_client.decide(event)
            _log(
                f"[INFO][{event.session_key()}] decision action={decision.action}, "
                f"reason={decision.reason}"
            )

            if decision.action == "manual":
                state_store.set_mode(event, "manual", now_ms())
                await page.wait_for_timeout(1000)
                continue

            reply_text = (decision.reply_text or "").strip()
            if decision.action != "send" or (not reply_text):
                if decision.action == "send":
                    state_store.unmark_incoming(event)
                await page.wait_for_timeout(800)
                continue

            sent_ok = await _send_text_reply(page, editor, reply_text)
            if sent_ok:
                state_store.mark_reply(event, now_ms())
                await decision_client.ack_delivery(
                    msg=event,
                    decision=decision,
                    sent_text=reply_text,
                    sent_image_urls=[],
                    status="sent",
                )
                _log(f"[INFO][{event.session_key()}] 已发送回复。")
            else:
                state_store.unmark_incoming(event)
                await decision_client.ack_delivery(
                    msg=event,
                    decision=decision,
                    sent_text="",
                    sent_image_urls=[],
                    status="failed",
                )
                _log(f"[WARN][{event.session_key()}] 回复发送失败，等待下轮重试。")

    except Exception as exc:
        category, detail = classify_worker_exception(exc)
        if category == CATEGORY_FATAL_LOCAL_STATE:
            health_reporter.fatal(detail)
            raise WorkerFatalError(detail) from exc
        if category == CATEGORY_MANUAL_ACTION:
            health_reporter.manual_action(detail)
        else:
            health_reporter.degraded(detail)
        _log(f"[ERROR] 浏览器页面监控异常: {exc}")


async def run_loop(
    ports: Iterable[int],
    decision_client: DecisionClient,
    state_store: LocalStateStore,
    health_reporter: WorkerHealthReporter,
    tenant_id: str,
    store_id: str,
    store_name: str,
    min_reply_interval_ms: int,
) -> None:
    _log(f"[INFO] Worker 启动，待连接端口: {list(ports)}")
    async with async_playwright() as playwright:
        while True:
            browser_page_pairs = await _collect_browsers(playwright, ports)
            if not browser_page_pairs:
                health_reporter.degraded("page_not_found")
                _log("[WARN] 未找到快手客服页，3秒后重试。")
                await asyncio.sleep(3)
                continue

            health_reporter.ready("page_bound")
            tasks = []
            try:
                for browser, page in browser_page_pairs:
                    task = asyncio.create_task(
                        monitor_single_browser(
                            page=page,
                            decision_client=decision_client,
                            state_store=state_store,
                            health_reporter=health_reporter,
                            tenant_id=tenant_id,
                            store_id=store_id,
                            store_name=store_name,
                            min_reply_interval_ms=min_reply_interval_ms,
                        )
                    )
                    tasks.append((task, browser))

                await asyncio.gather(*(t for t, _ in tasks))
                health_reporter.degraded("monitor_task_exited")
                _log("[WARN] 监控任务已退出，1秒后自动重连。")
            finally:
                for task, browser in tasks:
                    if not task.done():
                        task.cancel()
                    _disconnect_browser(browser)
            await asyncio.sleep(1)


def _disconnect_browser(browser: Browser) -> None:
    connection = getattr(browser, "_connection", None)
    if connection and hasattr(connection, "close"):
        try:
            connection.close()
            return
        except Exception:
            pass
    close_coro = getattr(browser, "close", None)
    if callable(close_coro):
        try:
            result = close_coro()
            if asyncio.iscoroutine(result):
                try:
                    asyncio.create_task(result)
                except RuntimeError:
                    pass
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="快手客服 Worker（接 Ubuntu 决策）")
    parser.add_argument(
        "--ports",
        type=int,
        nargs="+",
        default=list(DEFAULT_PORTS),
        help="Chrome 远程调试端口，默认 9225",
    )
    parser.add_argument("--tenant-id", default="default", help="租户 ID")
    parser.add_argument("--store-id", default=DEFAULT_STORE_ID, help="店铺 ID")
    parser.add_argument("--store-name", default=DEFAULT_STORE_NAME, help="店铺展示名")
    parser.add_argument(
        "--decision-url",
        default=DEFAULT_DECISION_URL,
        help="Ubuntu 决策服务地址，不含路径",
    )
    parser.add_argument("--decision-key", default=DEFAULT_DECISION_KEY, help="决策服务 API Key")
    parser.add_argument(
        "--fallback-text",
        default=DEFAULT_FALLBACK_REPLY,
        help="决策服务不可用时的兜底回复文本",
    )
    parser.add_argument(
        "--state-db",
        default=DEFAULT_STATE_DB,
        help="本地 SQLite 状态文件",
    )
    parser.add_argument(
        "--min-reply-interval-sec",
        type=float,
        default=0.8,
        help="同会话最小回复间隔（秒）",
    )
    parser.add_argument(
        "--decision-timeout-sec",
        type=float,
        default=DEFAULT_DECISION_TIMEOUT_SEC,
        help="决策服务超时时间（秒）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _log(
        "[INFO] 启动参数: "
        f"ports={args.ports}, store_id={args.store_id}, store_name={args.store_name}, "
        f"decision_url={args.decision_url}, state_db={args.state_db}, "
        f"timeout_sec={args.decision_timeout_sec}"
    )

    state_store = LocalStateStore(args.state_db)
    decision_client = DecisionClient(
        base_url=args.decision_url,
        api_key=args.decision_key,
        fallback_text=args.fallback_text,
        timeout_sec=args.decision_timeout_sec,
    )
    health_reporter = WorkerHealthReporter.from_env(
        worker_key=f"kuaishou_{args.store_id}",
        worker_kind="kuaishou",
        store_id=args.store_id,
        store_name=args.store_name or args.store_id,
    )
    min_reply_interval_ms = int(args.min_reply_interval_sec * 1000)

    health_reporter.start("booting")
    try:
        asyncio.run(
            run_loop(
                ports=args.ports,
                decision_client=decision_client,
                state_store=state_store,
                health_reporter=health_reporter,
                tenant_id=args.tenant_id,
                store_id=args.store_id,
                store_name=args.store_name,
                min_reply_interval_ms=min_reply_interval_ms,
            )
        )
    except WorkerFatalError as exc:
        health_reporter.fatal(exc.detail)
        _log(f"[ERROR] Worker 致命异常: {exc.detail}")
        raise SystemExit(exc.exit_code)
    except Exception as exc:
        detail = f"unexpected:{type(exc).__name__}: {exc}"
        health_reporter.fatal(detail)
        raise
    else:
        health_reporter.stopped("exited")


if __name__ == "__main__":
    main()
