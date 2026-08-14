#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Douyin (飞鸽) worker with local dedup + Ubuntu decision API.

Flow:
1) find pending conversation
2) open conversation and extract latest customer message
3) dedup and basic session gating
4) call Ubuntu decision service
5) send decision reply back to chat
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
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

DEFAULT_PORTS = (9222,)
DEFAULT_STORE_ID = os.environ.get("STORE_ID", "douyin_store_example")
DEFAULT_STORE_NAME = os.environ.get("STORE_NAME", "示例抖音店铺")
DEFAULT_DECISION_URL = os.environ.get("DECISION_URL", "http://127.0.0.1:18080")
DEFAULT_DECISION_KEY = os.environ.get("DECISION_KEY", "")
DEFAULT_DECISION_TIMEOUT_SEC = 75.0
DEFAULT_STATE_DB = str((Path(__file__).resolve().parent / "state" / "douyin_worker.db"))
DEFAULT_FALLBACK_REPLY = "稍等"

PENDING_SECTION_LABELS = [
    '[data-btm="message_group_name_waitReply"]',
    '[data-btm="message_group_name_overtime"]',
]
CONVERSATION_ITEM = 'div[data-kora="conversation"][data-qa-id="qa-conversation-chat-item"]'
TEXTAREA_SELECTOR = 'textarea[data-qa-id="qa-send-message-textarea"]'
SEND_BUTTON_SELECTOR = 'div[data-qa-id="qa-send-message-button"]'
SEND_BUTTON_CLICK_TIMEOUT_MS = 5000
TEXT_INPUT_TIMEOUT_MS = 3000
IMAGE_UPLOAD_INPUT_SELECTORS = [
    'input[type="file"][accept*="image"]',
    'input[type="file"][accept*=".jpg"]',
    'input[type="file"][accept*=".png"]',
    'input[type="file"]',
]
CHANNEL_CAPABILITIES = {
    "channel": "worker",
    "platform": "douyin",
    "send_text": True,
    "send_image": True,
    "send_image_input": "image_url",
    "max_image_bytes": 8 * 1024 * 1024,
}
LOGIN_PAGE_EXACT_MARKERS = (
    "请登录后继续访问",
    "登录抖店",
    "登录飞鸽",
    "登录飞鸽客服系统",
)
LOGIN_PAGE_CONTEXT_MARKERS = (
    "飞鸽",
    "飞鸽客服系统",
    "抖店",
)

POPUP_SELECTORS = [
    ".repeat-interceptor-popup",
    ".auxo-modal",
    ".auxo-message",
]
POPUP_CONFIRM_BUTTONS = [
    ".repeat-interceptor-popup button.el-button--default",
    ".repeat-interceptor-popup button.el-button--primary",
    ".auxo-modal button.auxo-btn-primary",
    ".auxo-modal button.auxo-btn",
    ".auxo-message button",
]

NICKNAME_SELECTORS = [
    '[data-qa-id="qa-chat-user-name"]',
    '[data-qa-id="qa-session-user-name"]',
    ".chat-user-name",
    ".session-user-name",
    ".nickname",
]

INBOUND_MESSAGE_SELECTORS = [
    '[data-qa-id="qa-message-item-left"] [data-qa-id*="qa-message"]',
    '[data-qa-id="qa-message-item-left"]',
    ".message-item-left .message-content",
    ".message-left .message-text",
]

MESSAGE_WRAPPER_SELECTOR = '[data-qa-id="qa-message-warpper"]'
MESSAGE_ROOT_SELECTOR = "[data-id]"
OUTBOUND_MARKER_SELECTOR = ".messageIsMe"
CUSTOMER_TEXT_SELECTOR = ".messageNotMe pre span, .messageNotMe"
IMAGE_MESSAGE_SELECTOR = 'img[alt="图片"]'
AVATAR_SELECTOR = 'img[alt="头像"]'
CUSTOMER_AVATAR_HINTS = ("aweme-avatar", "douyinpic.com/aweme")
SKIP_MESSAGE_ID_PREFIXES = (
    "llm_process_",
    "cs_close_conversation",
)
SKIP_MESSAGE_ID_MARKERS = (
    "_Welcome_",
    "_CsAssign_",
)

_PENDING_CURSOR = 0
_FALLBACK_CURSOR = 0


def _log(msg: str) -> None:
    print(msg, flush=True)


def _score_page_url(url: str) -> int:
    """Rank candidate pages so we attach to 飞鸽 workspace first."""
    if not url:
        return 0
    if "im.jinritemai.com/pc_seller_v2/main/workspace" in url:
        return 100
    if "im.jinritemai.com/pc_seller_v2/main" in url:
        return 90
    if "im.jinritemai.com" in url and "workspace" in url:
        return 80
    if "im.jinritemai.com" in url:
        return 70
    if "jinritemai.com" in url and "im" in url:
        return 60
    if "jinritemai.com" in url:
        return 20
    return 0


def _looks_like_login_page(*, title: str, body_text: str, page_url: str) -> bool:
    normalized = re.sub(r"\s+", "", "\n".join(part for part in (title, body_text) if part))
    lowered_url = str(page_url or "").lower()
    if not normalized:
        return False
    if any(marker in normalized for marker in LOGIN_PAGE_EXACT_MARKERS):
        return True
    if "扫码登录" in normalized and any(marker in normalized for marker in LOGIN_PAGE_CONTEXT_MARKERS):
        return True
    if "验证码登录" in normalized and any(marker in normalized for marker in LOGIN_PAGE_CONTEXT_MARKERS):
        return True
    if "手机号登录" in normalized and ("扫码登录" in normalized or "验证码登录" in normalized):
        return True
    if "登录" in normalized and any(token in lowered_url for token in ("login", "passport")):
        if any(token in normalized for token in ("扫码", "验证码", "手机号", "飞鸽", "抖店")):
            return True
    return False


async def _read_page_text(page: Page, *, max_len: int = 2400) -> str:
    try:
        body = await page.locator("body").inner_text(timeout=1200)
    except Exception:
        return ""
    return str(body or "").strip()[:max_len]


async def _detect_login_page_detail(page: Page) -> str:
    try:
        title = await page.title()
    except Exception:
        title = ""
    body_text = await _read_page_text(page)
    page_url = str(page.url or "").strip()
    normalized_body = re.sub(r"\s+", "", body_text)
    blocking_details: List[str] = []
    if "登录过期" in normalized_body and "重新登录" in normalized_body:
        blocking_details.append("抖音登录已过期，需要重新登录")
    if "当前的账号暂无会话权限" in normalized_body or "当前账号暂无会话权限" in normalized_body:
        blocking_details.append("当前抖音账号暂无会话权限，需要主账号开通飞鸽客服权限")

    if blocking_details:
        detail = "；".join(blocking_details)
    elif _looks_like_login_page(title=str(title or ""), body_text=body_text, page_url=page_url):
        detail = "页面显示抖音登录页，需要重新登录"
    else:
        return ""
    if page_url:
        detail = f"{detail} ({page_url[:120]})"
    return detail


async def _find_browser_login_detail(browser: Browser) -> str:
    for context in browser.contexts:
        for page in context.pages:
            detail = await _detect_login_page_detail(page)
            if detail:
                return detail
    return ""


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
    playwright: Playwright, ports: Iterable[int]
) -> Tuple[List[Tuple[Browser, Page]], List[str]]:
    pairs: List[Tuple[Browser, Page]] = []
    login_details: List[str] = []
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
            login_detail = await _find_browser_login_detail(browser)
            if login_detail:
                login_details.append(f"端口 {port}: {login_detail}")
                _log(f"[WARN] 端口 {port} 已连接，但检测到抖音登录页。")
            urls: List[str] = []
            for context in browser.contexts:
                for p in context.pages:
                    url = (p.url or "").strip()
                    if url:
                        urls.append(url)
            if urls:
                preview = " | ".join(urls[:5])
                _log(f"[WARN] 端口 {port} 已连接，但未找到抖音客服页。当前标签页: {preview}")
            else:
                _log(f"[WARN] 端口 {port} 已连接，但浏览器无可用标签页。")
            _disconnect_browser(browser)
            continue
        _log(f"[INFO] 端口 {port} 绑定页面: {page.url}")
        pairs.append((browser, page))
    return pairs, login_details


async def _find_pending_conversation(page: Page) -> Optional[Locator]:
    global _PENDING_CURSOR, _FALLBACK_CURSOR
    for label_selector in PENDING_SECTION_LABELS:
        section = page.locator(label_selector).first
        if await section.count() == 0:
            continue
        container = section.locator("xpath=ancestor::div[contains(@class,'list_items')]")
        if await container.count() == 0:
            continue
        convos = container.locator(CONVERSATION_ITEM)
        convo_count = await convos.count()
        if convo_count > 0:
            idx = _PENDING_CURSOR % convo_count
            _PENDING_CURSOR += 1
            return convos.nth(idx)
    # Fallback: rotate across visible conversations instead of always selecting the first one.
    fallback = page.locator(CONVERSATION_ITEM)
    fallback_count = await fallback.count()
    if fallback_count > 0:
        idx = _FALLBACK_CURSOR % fallback_count
        _FALLBACK_CURSOR += 1
        return fallback.nth(idx)
    return None


async def _wait_for_popup_and_confirm(page: Page) -> None:
    for _ in range(3):
        try:
            await page.wait_for_selector(", ".join(POPUP_SELECTORS), timeout=1000)
        except PlaywrightTimeoutError:
            return

        clicked = False
        for btn in POPUP_CONFIRM_BUTTONS:
            button = await page.query_selector(btn)
            if button:
                await button.click()
                clicked = True
                await page.wait_for_timeout(200)
                break
        if not clicked:
            break


async def _extract_platform_nickname(page: Page) -> str:
    for selector in NICKNAME_SELECTORS:
        node = page.locator(selector).first
        if await node.count() == 0:
            continue
        try:
            text = _normalize_nickname(await node.inner_text())
        except Exception:
            continue
        if text:
            return text
    return ""


def _normalize_avatar_src(src: str) -> str:
    """Keep avatar identity stable by removing expiring query params."""
    value = (src or "").strip()
    if not value:
        return ""
    return value.split("?", 1)[0]


def _normalize_nickname(value: str) -> str:
    text = (value or "").replace("\u200b", "").strip()
    if not text:
        return ""
    # Conversation item may contain multiple lines (nickname + preview + time).
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Skip obvious time labels.
        if len(line) <= 16 and any(
            token in line for token in ("分钟前", "刚刚", "今天", "昨天", "月", "日", ":", "秒")
        ) and any(ch.isdigit() for ch in line):
            continue
        return line[:64]
    return ""


def _is_ignored_message_id(data_id: str) -> bool:
    if not data_id:
        return False
    return data_id.startswith(SKIP_MESSAGE_ID_PREFIXES) or any(
        marker in data_id for marker in SKIP_MESSAGE_ID_MARKERS
    )


def _looks_like_order_card(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    keywords = (
        "订单号",
        "订单编号",
        "用户正在查看订单",
        "查看详情",
        "共1件",
        "待发货",
        "备货中",
    )
    return any(k in value for k in keywords)


def _normalize_card_text(text: str, max_len: int = 220) -> str:
    lines = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lines.append(line)
    joined = " | ".join(lines)
    return joined[:max_len]


def _extract_order_id_from_text(text: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    patterns = (
        r"订单(?:号|编号)\s*[:：]?\s*([A-Za-z0-9_-]{8,})",
        r"\b([0-9]{12,})\b",
    )
    for pattern in patterns:
        m = re.search(pattern, value)
        if m:
            return (m.group(1) or "").strip()
    return ""


async def _extract_latest_customer_message(
    page: Page,
    skip_message_ids: Optional[set[str]] = None,
) -> Tuple[str, str, str, str]:
    skip_ids = skip_message_ids or set()
    wraps = page.locator(MESSAGE_WRAPPER_SELECTOR)
    wrap_count = await wraps.count()
    if wrap_count > 0:
        for idx in range(wrap_count - 1, -1, -1):
            wrap = wraps.nth(idx)
            root = wrap.locator(MESSAGE_ROOT_SELECTOR).first
            if await root.count() == 0:
                root = wrap

            data_id = ""
            try:
                data_id = (await root.get_attribute("data-id") or "").strip()
            except Exception:
                data_id = ""
            if data_id and data_id in skip_ids:
                continue
            if _is_ignored_message_id(data_id):
                continue

            # Outbound/self messages should be ignored.
            if await root.locator(OUTBOUND_MARKER_SELECTOR).count() > 0:
                continue

            text = ""
            text_node = root.locator(CUSTOMER_TEXT_SELECTOR).first
            if await text_node.count() > 0:
                try:
                    text = (await text_node.inner_text()).strip()
                except Exception:
                    text = ""

            media_url = ""
            image_node = root.locator(IMAGE_MESSAGE_SELECTOR).first
            if await image_node.count() > 0:
                try:
                    media_url = (await image_node.get_attribute("src") or "").strip()
                except Exception:
                    media_url = ""

            # Prefer an explicit customer-side marker when available.
            has_customer_text_marker = await root.locator(".messageNotMe").count() > 0
            if media_url and not has_customer_text_marker:
                uuid_like_id = data_id.count("-") >= 3 and len(data_id) >= 24
                avatar = root.locator(AVATAR_SELECTOR).first
                if await avatar.count() > 0:
                    try:
                        avatar_src = (await avatar.get_attribute("src") or "").strip()
                    except Exception:
                        avatar_src = ""
                    if avatar_src and not any(
                        hint in avatar_src for hint in CUSTOMER_AVATAR_HINTS
                    ) and not uuid_like_id:
                        # Unknown/noisy image block (e.g. cards). Keep scanning.
                        continue

            if media_url:
                return text, data_id, "image", media_url
            if text:
                return text, data_id, "text", ""

            # Non-text order card: keep as a separate structured message type.
            try:
                inner_text = (await root.inner_text()).strip()
            except Exception:
                inner_text = ""
            if _looks_like_order_card(inner_text):
                order_media_url = ""
                try:
                    img_nodes = root.locator("img")
                    img_count = await img_nodes.count()
                    for i in range(img_count):
                        img = img_nodes.nth(i)
                        alt = ((await img.get_attribute("alt")) or "").strip()
                        src = ((await img.get_attribute("src")) or "").strip()
                        if not src:
                            continue
                        if alt == "头像" or any(hint in src for hint in CUSTOMER_AVATAR_HINTS):
                            continue
                        order_media_url = src
                        break
                except Exception:
                    order_media_url = ""
                card_text = _normalize_card_text(inner_text)
                order_id = _extract_order_id_from_text(card_text)
                if order_id and f"订单号:{order_id}" not in card_text:
                    card_text = f"订单号:{order_id} | {card_text}"
                return card_text, data_id, "order_card", order_media_url

    # Legacy fallback: text-only selectors.
    for selector in INBOUND_MESSAGE_SELECTORS:
        nodes = page.locator(selector)
        count = await nodes.count()
        if count == 0:
            continue

        latest = nodes.nth(count - 1)
        try:
            text = (await latest.inner_text()).strip()
        except Exception:
            text = ""
        if not text:
            continue

        msg_id = ""
        for attr in ("data-message-id", "data-id", "data-mid", "id"):
            try:
                value = await latest.get_attribute(attr)
            except Exception:
                value = None
            if value:
                msg_id = value.strip()
                if msg_id:
                    break
        if msg_id and msg_id in skip_ids:
            continue
        return text, msg_id, "text", ""
    return "", "", "", ""


async def _extract_customer_identity(
    convo: Locator, page: Page
) -> Tuple[str, str, str]:
    attrs = await convo.evaluate(
        """el => ({
            dataConversationId: el.getAttribute('data-conversation-id') || '',
            dataId: el.getAttribute('data-id') || '',
            dataUid: el.getAttribute('data-uid') || '',
            text: (el.innerText || '').trim(),
            ownTitle: (el.getAttribute('title') || '').trim(),
            title: (el.querySelector('div[title]')?.getAttribute('title') || '').trim(),
            nickname: (el.querySelector('span[class], [title]')?.textContent || '').trim(),
            nameText: (el.querySelector('[data-qa-id*="conversation"][title], [data-qa-id*="name"], .name')?.textContent || '').trim(),
            avatarSrc: (el.querySelector('img[alt="头像"]')?.getAttribute('src') || '').trim()
        })"""
    )

    source_ref = (
        (attrs.get("dataConversationId") or "").strip()
        or (attrs.get("dataId") or "").strip()
        or (attrs.get("dataUid") or "").strip()
    )
    customer_id = (attrs.get("dataUid") or "").strip()
    nickname = ""
    for candidate in (
        attrs.get("ownTitle"),
        attrs.get("title"),
        attrs.get("nameText"),
        attrs.get("nickname"),
        attrs.get("text"),
    ):
        nickname = _normalize_nickname(str(candidate or ""))
        if nickname:
            break
    if not nickname:
        nickname = await _extract_platform_nickname(page)
    avatar_key = _normalize_avatar_src((attrs.get("avatarSrc") or "").strip())

    if not source_ref:
        # Build from stable identity only; avoid dynamic inner text like "4分钟".
        page_key = (page.url or "").split("?", 1)[0]
        stable_identity = "|".join(part for part in (nickname, avatar_key, page_key) if part)
        seed = stable_identity or page_key or f"{page.url}|{attrs.get('text', '')}"
        source_ref = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]
    if not customer_id:
        customer_seed = "|".join(part for part in (nickname, avatar_key) if part) or source_ref
        customer_id = hashlib.sha1(customer_seed.encode("utf-8")).hexdigest()[:16]
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


def _download_image_to_temp_file(image_url: str) -> Optional[Path]:
    url = str(image_url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        return None
    req = urllib.request.Request(url=url, method="GET")
    req.add_header("User-Agent", "Mozilla/5.0")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = str(resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            payload = resp.read()
    except Exception as exc:
        _log(f"[WARN] 下载图片失败: {type(exc).__name__}: {url}")
        return None

    if not payload:
        _log(f"[WARN] 下载图片为空: {url}")
        return None
    if len(payload) > 8 * 1024 * 1024:
        _log(f"[WARN] 图片过大，跳过发送: {len(payload)} bytes, url={url}")
        return None

    suffix = ".jpg"
    if content_type == "image/png":
        suffix = ".png"
    elif content_type == "image/gif":
        suffix = ".gif"
    elif content_type == "image/webp":
        suffix = ".webp"
    else:
        parsed_path = urllib.parse.urlparse(url).path
        m = re.search(r"\.(jpg|jpeg|png|gif|webp)(?:$|\?)", parsed_path, flags=re.IGNORECASE)
        if m:
            suffix = "." + m.group(1).lower().replace("jpeg", "jpg")

    tmp_dir = Path(__file__).resolve().parent / "state" / "tmp_images"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(prefix="reply_img_", suffix=suffix, delete=False, dir=str(tmp_dir))
    try:
        tmp.write(payload)
        tmp.flush()
        return Path(tmp.name)
    finally:
        tmp.close()


async def _send_image_reply(page: Page, image_url: str) -> bool:
    image_path = _download_image_to_temp_file(image_url)
    if image_path is None:
        return False
    sent = False
    try:
        for selector in IMAGE_UPLOAD_INPUT_SELECTORS:
            locator = page.locator(selector).first
            try:
                if await locator.count() <= 0:
                    continue
                await locator.set_input_files(str(image_path), timeout=3000)
                await page.wait_for_timeout(350)
                try:
                    await page.click(SEND_BUTTON_SELECTOR, timeout=2000)
                except Exception:
                    # Some UIs auto-send after upload.
                    pass
                await _wait_for_popup_and_confirm(page)
                await page.wait_for_timeout(700)
                sent = True
                break
            except Exception:
                continue
        if not sent:
            _log(f"[WARN] 未找到可用图片上传控件，发送失败: {image_url}")
        return sent
    finally:
        try:
            image_path.unlink(missing_ok=True)
        except Exception:
            pass


async def _click_send_button(page: Page) -> bool:
    try:
        await page.click(SEND_BUTTON_SELECTOR, timeout=SEND_BUTTON_CLICK_TIMEOUT_MS)
        return True
    except Exception as exc:
        _log(f"[WARN] 点击发送按钮失败，等待下轮重试: {type(exc).__name__}: {exc}")
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
            login_page_detail = await _detect_login_page_detail(page)
            if login_page_detail:
                health_reporter.manual_action(login_page_detail)
                _log(f"[WARN] 页面登录态异常: {login_page_detail}")
                await page.wait_for_timeout(5000)
                continue

            convo = await _find_pending_conversation(page)
            if convo is None:
                health_reporter.ready("idle")
                print("没有待回复的会话，等待中...")
                await page.wait_for_timeout(3000)
                continue

            try:
                await convo.click(timeout=5000)
            except Exception as exc:
                print(f"[WARN] 会话点击失败，跳过本轮: {type(exc).__name__}")
                await page.wait_for_timeout(800)
                continue
            try:
                textarea = await page.wait_for_selector(TEXTAREA_SELECTOR, timeout=3000)
            except PlaywrightTimeoutError as exc:
                print("[WARN] 未找到消息输入框，跳过本轮。")
                await page.wait_for_timeout(1000)
                continue

            source_ref, customer_id, platform_nickname = await _extract_customer_identity(
                convo, page
            )
            if not platform_nickname:
                platform_nickname = await _extract_platform_nickname(page)
            if not platform_nickname:
                _log(f"[WARN][{source_ref}] 未提取到顾客昵称，跳过此会话。")
                await page.wait_for_timeout(1000)
                continue
            session_key = f"douyin:{store_id}:{platform_nickname}"
            event: Optional[IncomingMessage] = None
            scanned_duplicate_ids: set[str] = set()
            for _ in range(6):
                (
                    message_text,
                    message_id_hint,
                    message_type,
                    media_url,
                ) = await _extract_latest_customer_message(
                    page,
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
                    platform="douyin",
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
                    print(
                        f"[INFO][{session_key}] 最近消息均已处理，跳过。"
                    )
                else:
                    print(f"[WARN][{session_key}] 未提取到顾客消息内容，跳过。")
                health_reporter.ready("idle")
                await page.wait_for_timeout(1000)
                continue
            health_reporter.ready("processing", progress=True)
            print(
                f"[INFO][{event.session_key()}] 收到消息 type={event.message_type}, "
                f"id={event.message_id}, text={event.text[:40]!r}, "
                f"media={'yes' if bool(event.media_url) else 'no'}"
            )

            current_mode = state_store.get_mode(event)
            if current_mode != "auto":
                print(f"[INFO][{event.session_key()}] 当前为 {current_mode} 模式，跳过自动回复。")
                await page.wait_for_timeout(1000)
                continue

            now_ts = now_ms()
            if not state_store.can_auto_reply(event, now_ts, min_reply_interval_ms):
                # Do not consume this message; retry after cooldown.
                state_store.unmark_incoming(event)
                print(f"[INFO][{event.session_key()}] 回复节流生效，跳过。")
                await page.wait_for_timeout(1000)
                continue

            decision = await decision_client.decide(event)
            print(
                f"[INFO][{event.session_key()}] decision action={decision.action}, "
                f"reason={decision.reason}"
            )

            if decision.action == "manual":
                state_store.set_mode(event, "manual", now_ms())
                await page.wait_for_timeout(1000)
                continue
            reply_text = (decision.reply_text or "").strip()
            image_urls = [str(url or "").strip() for url in decision.reply_image_urls if str(url or "").strip()]
            if decision.action != "send" or (not reply_text and not image_urls):
                if decision.action == "send":
                    # Do not drop this message silently; retry in next loop.
                    state_store.unmark_incoming(event)
                await page.wait_for_timeout(800)
                continue

            sent_any = False
            sent_text = ""
            sent_images: list[str] = []
            try:
                if reply_text:
                    await textarea.click(timeout=TEXT_INPUT_TIMEOUT_MS)
                    await textarea.fill("", timeout=TEXT_INPUT_TIMEOUT_MS)
                    await textarea.fill(reply_text, timeout=TEXT_INPUT_TIMEOUT_MS)
                    await page.wait_for_timeout(120)
                    if not await _click_send_button(page):
                        raise RuntimeError("send_button_click_failed")
                    await _wait_for_popup_and_confirm(page)
                    await page.wait_for_timeout(500)
                    sent_any = True
                    sent_text = reply_text

                for image_url in image_urls:
                    ok = await _send_image_reply(page, image_url)
                    print(
                        f"[INFO][{event.session_key()}] 发送图片 url={image_url[:120]!r}, ok={ok}"
                    )
                    sent_any = sent_any or ok
                    if ok:
                        sent_images.append(image_url)

                if (not sent_any) and image_urls and (not reply_text):
                    # Avoid silent failures when decision expects image-only sending.
                    await textarea.click(timeout=TEXT_INPUT_TIMEOUT_MS)
                    await textarea.fill("", timeout=TEXT_INPUT_TIMEOUT_MS)
                    await textarea.fill(DEFAULT_FALLBACK_REPLY, timeout=TEXT_INPUT_TIMEOUT_MS)
                    await page.wait_for_timeout(120)
                    if not await _click_send_button(page):
                        raise RuntimeError("send_button_click_failed")
                    await _wait_for_popup_and_confirm(page)
                    await page.wait_for_timeout(500)
                    sent_any = True
                    sent_text = DEFAULT_FALLBACK_REPLY
            except Exception as exc:
                sent_any = False
                sent_text = ""
                sent_images = []
                _log(
                    f"[WARN][{event.session_key()}] 回复发送流程异常，等待下轮重试: "
                    f"{type(exc).__name__}: {exc}"
                )

            if sent_any:
                state_store.mark_reply(event, now_ms())
                await decision_client.ack_delivery(
                    msg=event,
                    decision=decision,
                    sent_text=sent_text,
                    sent_image_urls=sent_images,
                    status="sent",
                )
                print(f"[INFO][{event.session_key()}] 已发送回复。")
            else:
                state_store.unmark_incoming(event)
                await decision_client.ack_delivery(
                    msg=event,
                    decision=decision,
                    sent_text="",
                    sent_image_urls=[],
                    status="failed",
                )
                print(f"[WARN][{event.session_key()}] 回复发送失败，等待下轮重试。")
    except Exception as exc:
        category, detail = classify_worker_exception(exc)
        if category == CATEGORY_FATAL_LOCAL_STATE:
            health_reporter.fatal(detail)
            raise WorkerFatalError(detail) from exc
        if category == CATEGORY_MANUAL_ACTION:
            health_reporter.manual_action(detail)
        else:
            health_reporter.degraded(detail)
        print(f"[ERROR] 浏览器页面监控异常: {exc}")


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
            browser_page_pairs, login_details = await _collect_browsers(playwright, ports)
            if not browser_page_pairs:
                if login_details:
                    detail = "；".join(login_details[:2])
                    health_reporter.manual_action(detail)
                    _log(f"[WARN] 未绑定到抖音客服工作台，但检测到登录页: {detail}")
                    await asyncio.sleep(5)
                else:
                    health_reporter.degraded("page_not_found")
                    _log("[WARN] 未找到抖音客服页，3秒后重试。")
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
                _log("[WARN] 监控任务已退出，等待重新绑定页面。")
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
    parser = argparse.ArgumentParser(description="抖音客服 Worker（接 Ubuntu 决策）")
    parser.add_argument(
        "--ports",
        type=int,
        nargs="+",
        default=list(DEFAULT_PORTS),
        help="Chrome 远程调试端口，默认 9222",
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
        default=2.0,
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
        f"ports={args.ports}, store_id={args.store_id}, store_name={args.store_name or args.store_id}, "
        f"decision_url={args.decision_url}, state_db={args.state_db}, timeout_sec={args.decision_timeout_sec}"
    )
    decision_client = DecisionClient(
        base_url=args.decision_url,
        api_key=args.decision_key,
        timeout_sec=args.decision_timeout_sec,
        fallback_text=args.fallback_text,
    )
    state_store = LocalStateStore(args.state_db)
    health_reporter = WorkerHealthReporter.from_env(
        worker_key=f"douyin_{args.store_id}",
        worker_kind="douyin",
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
