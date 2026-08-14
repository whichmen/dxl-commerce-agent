#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QianNiu mobile worker (Appium + UiAutomator2).

Key properties:
- No OCR, no adb uiautomator dump polling loop.
- Use Appium page source + element actions.
- Same worker protocol: IncomingMessage -> /v1/decide -> ack.
- Session key strictly: taobao:{store_id}:{platform_nickname}
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from .core import DecisionClient, IncomingMessage, LocalStateStore, now_ms
except ImportError:  # Direct script execution from workers/
    from core import DecisionClient, IncomingMessage, LocalStateStore, now_ms

try:
    from appium import webdriver
    from appium.options.android import UiAutomator2Options
    from appium.webdriver.common.appiumby import AppiumBy
except Exception as exc:  # pragma: no cover
    webdriver = None
    UiAutomator2Options = None
    AppiumBy = None
    _APPIUM_IMPORT_ERROR = exc
else:
    _APPIUM_IMPORT_ERROR = None


DEFAULT_STORE_ID = os.environ.get("STORE_ID", "taobao_store_example")
DEFAULT_STORE_NAME = os.environ.get("STORE_NAME", "示例淘宝店铺")
DEFAULT_DECISION_URL = os.environ.get("DECISION_URL", "http://127.0.0.1:18080")
DEFAULT_DECISION_KEY = os.environ.get("DECISION_KEY", "")
DEFAULT_DECISION_TIMEOUT_SEC = 75.0
DEFAULT_STATE_DB = str((Path(__file__).resolve().parent / "state" / "qianniu_mobile_worker.db"))
DEFAULT_MEDIA_DIR = str((Path(__file__).resolve().parent / "state" / "media" / "qianniu"))
DEFAULT_FALLBACK_REPLY = "稍等"
DEFAULT_APPIUM_SERVER_URL = "http://127.0.0.1:4723"
DEFAULT_APP_PACKAGE = "com.taobao.qianniu"
DEFAULT_APP_ACTIVITY = "com.qianniu.launcher.business.main.MainActivity"

INPUT_ID = "com.taobao.qianniu:id/msgcenter_panel_input_edit"
TAB_LAYOUT_ID = "com.taobao.qianniu:id/tabLayout"
CHAT_TEXT_ID = "com.taobao.qianniu:id/tv_chat_text"
CHAT_USER_NAME_ID = "com.taobao.qianniu:id/tv_user_name"
CHAT_WRAPPER_ID = "com.taobao.qianniu:id/chat_msg_item_wrapper"

LIST_IGNORE_TEXT = {
    "在线",
    "星标",
    "工作台",
    "消息",
    "营销",
    "头条",
    "服务",
    "未处理的离线消息",
    "点击查看近2天未处理的离线消息",
    "[已读]",
    "[未读]",
    "稍等",
    "[草稿]",
    "对方正在输入…",
    "对方正在输入...",
    "正在输入",
}

CHAT_IGNORE_TEXT = {
    "头像",
    "设置",
    "返回上一页",
    "相册",
    "小额打款",
    "邀请下单",
    "已读",
    "对方正在输入…",
    "对方正在输入...",
    "正在输入",
}

CHANNEL_CAPABILITIES = {
    "channel": "worker",
    "platform": "taobao",
    "send_text": True,
    "send_image": False,
    "send_image_input": "none",
}


@dataclass(frozen=True)
class Bounds:
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def cx(self) -> int:
        return (self.x1 + self.x2) // 2

    @property
    def cy(self) -> int:
        return (self.y1 + self.y2) // 2


@dataclass(frozen=True)
class UiNode:
    text: str
    resource_id: str
    class_name: str
    content_desc: str
    clickable: bool
    bounds: Bounds


@dataclass(frozen=True)
class PendingConversation:
    nickname: str
    unread: int
    tap_x: int
    tap_y: int
    y: int
    preview_text: str = ""
    time_text: str = ""
    time_need_reply: bool = False
    time_age_sec: int = 0


@dataclass(frozen=True)
class ExtractedMessage:
    message_type: str
    text: str
    message_id_hint: str
    order_id: str
    side: str = "left"
    send_time_text: str = ""
    wrapper_bounds_sig: str = ""
    image_bounds_sig: str = ""
    media_url: str = ""


class AppiumSession:
    def __init__(
        self,
        *,
        server_url: str,
        udid: str,
        app_package: str,
        app_activity: str,
        new_command_timeout_sec: int = 180,
    ) -> None:
        self.server_url = str(server_url or "").strip()
        self.udid = str(udid or "").strip()
        self.app_package = str(app_package or "").strip()
        self.app_activity = str(app_activity or "").strip()
        self.new_command_timeout_sec = max(60, int(new_command_timeout_sec))
        self.driver = None

    def connect(self) -> None:
        if webdriver is None or UiAutomator2Options is None:
            raise RuntimeError(f"appium_python_client_missing:{_APPIUM_IMPORT_ERROR}")
        if not self.server_url:
            raise RuntimeError("appium_server_url_required")
        if not self.udid:
            raise RuntimeError("mobile_udid_required")

        options = UiAutomator2Options()
        options.load_capabilities(
            {
                "platformName": "Android",
                "automationName": "UiAutomator2",
                "udid": self.udid,
                "deviceName": self.udid,
                "appPackage": self.app_package,
                "appActivity": self.app_activity,
                "noReset": True,
                "newCommandTimeout": self.new_command_timeout_sec,
                "disableWindowAnimation": True,
                "ignoreHiddenApiPolicyError": True,
            }
        )
        self.driver = webdriver.Remote(self.server_url, options=options)
        self.driver.implicitly_wait(1.0)

    def quit(self) -> None:
        if self.driver is None:
            return
        try:
            self.driver.quit()
        except Exception:
            pass
        self.driver = None

    def reconnect(self) -> None:
        self.quit()
        self.connect()

    def ensure_ready(self) -> None:
        if self.driver is None:
            self.connect()
        assert self.driver is not None
        try:
            if self.driver.is_locked():
                self.driver.unlock()
        except Exception:
            pass
        try:
            current_pkg = str(self.driver.current_package or "")
        except Exception:
            current_pkg = ""
        if current_pkg != self.app_package:
            self.driver.activate_app(self.app_package)
            time.sleep(0.8)

    def page_nodes(self) -> list[UiNode]:
        assert self.driver is not None
        source = self.driver.page_source
        return _parse_nodes(source)

    def tap(self, x: int, y: int) -> None:
        assert self.driver is not None
        self.driver.execute_script("mobile: clickGesture", {"x": int(x), "y": int(y)})

    def back(self) -> None:
        assert self.driver is not None
        self.driver.back()

    def find_input_element(self):
        assert self.driver is not None and AppiumBy is not None
        return self.driver.find_element(AppiumBy.ID, INPUT_ID)

    def find_send_button(self):
        assert self.driver is not None and AppiumBy is not None
        btns = self.driver.find_elements(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().text("发送")')
        for b in btns:
            try:
                if b.is_displayed():
                    return b
            except Exception:
                continue
        return None

    def press_backspace(self, count: int) -> None:
        assert self.driver is not None
        n = max(1, min(int(count), 600))
        for _ in range(n):
            self.driver.press_keycode(67)

    def screenshot_png(self) -> bytes:
        assert self.driver is not None
        return self.driver.get_screenshot_as_png()

    def scroll_chat_to_bottom(self) -> None:
        assert self.driver is not None
        try:
            self.driver.execute_script(
                "mobile: scrollGesture",
                {
                    "left": 20,
                    "top": 240,
                    "width": 1040,
                    "height": 1500,
                    "direction": "down",
                    "percent": 0.88,
                },
            )
        except Exception:
            pass


class WorkerRuntime:
    def __init__(
        self,
        *,
        appium_session: AppiumSession,
        decision_client: DecisionClient,
        state_store: LocalStateStore,
        tenant_id: str,
        store_id: str,
        store_name: str,
        min_reply_interval_ms: int,
        poll_interval_sec: float,
        open_chat_wait_sec: float,
        back_to_list_wait_sec: float,
        media_dir: str,
    ) -> None:
        self.appium = appium_session
        self.decision_client = decision_client
        self.state_store = state_store
        self.tenant_id = tenant_id
        self.store_id = store_id
        self.store_name = store_name
        self.min_reply_interval_ms = min_reply_interval_ms
        self.poll_interval_sec = poll_interval_sec
        self.open_chat_wait_sec = open_chat_wait_sec
        self.back_to_list_wait_sec = back_to_list_wait_sec

        self.last_page = ""
        self.preview_seen_sig: dict[str, str] = {}
        self.list_cursor = 0
        self.expected_chat_nickname = ""
        self.expected_list_preview_sig = ""
        self.unknown_rounds = 0
        self.media_dir = Path(media_dir).expanduser()
        try:
            self.media_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    async def run(self) -> None:
        _log(f"[INFO] QianNiu Appium worker 启动: udid={self.appium.udid}")

        while True:
            try:
                self.appium.ensure_ready()
                nodes = self.appium.page_nodes()
            except Exception as exc:
                _log(f"[WARN] Appium/UI 失败: {type(exc).__name__}: {exc}")
                await asyncio.sleep(2.0)
                try:
                    self.appium.reconnect()
                except Exception:
                    await asyncio.sleep(2.0)
                continue

            page_type = _detect_page_type(nodes)
            if page_type != self.last_page:
                _log(f"[INFO] 页面状态: {page_type}")
                self.last_page = page_type

            if page_type == "unknown":
                self.unknown_rounds += 1
            else:
                self.unknown_rounds = 0

            if _has_offline_popup(nodes):
                _log("[INFO] 检测到“未处理离线消息”弹层，自动关闭。")
                try:
                    self.appium.tap(1004, 152)
                except Exception:
                    pass
                await asyncio.sleep(0.6)
                continue

            if page_type == "list":
                await self._on_list(nodes)
                continue
            if page_type == "chat":
                await self._on_chat()
                continue

            # Unknown page: bring app to front and retry.
            if self.unknown_rounds >= 3:
                self._try_open_message_tab(nodes)
            try:
                self.appium.ensure_ready()
            except Exception:
                pass
            await asyncio.sleep(0.9)

    async def _on_list(self, nodes: list[UiNode]) -> None:
        visible = _extract_visible_conversations(nodes)
        if not visible:
            await asyncio.sleep(self.poll_interval_sec)
            return

        time_targets = [s for s in visible if s.time_need_reply]
        if time_targets:
            time_targets.sort(key=lambda x: (-x.time_age_sec, x.y))
            target = time_targets[0]
            _log(
                f"[INFO] 时间信号触发: {target.nickname} time={target.time_text!r} "
                f"age_sec={target.time_age_sec}"
            )
        else:
            target = self._pick_preview_target(visible)
            if target is None:
                await asyncio.sleep(self.poll_interval_sec)
                return
            _log(f"[INFO] 预览触发: {target.nickname} preview={target.preview_text[:40]!r}")

        self.expected_chat_nickname = target.nickname
        session_key = f"taobao:{self.store_id}:{target.nickname}"
        self.expected_list_preview_sig = _norm_for_compare(target.preview_text)[:120]
        if self.expected_list_preview_sig:
            self.preview_seen_sig[session_key] = self.expected_list_preview_sig
        try:
            self.appium.tap(target.tap_x, target.tap_y)
        except Exception as exc:
            _log(f"[WARN] 点击会话失败: {type(exc).__name__}: {exc}")
            self.expected_chat_nickname = ""
            self.expected_list_preview_sig = ""
            await asyncio.sleep(0.6)
            return

        await asyncio.sleep(self.open_chat_wait_sec)

    async def _on_chat(self) -> None:
        # Keep processing this chat until no new customer message appears twice.
        empty_round = 0
        read_fail_round = 0
        while empty_round < 2:
            try:
                nodes = self.appium.page_nodes()
            except Exception as exc:
                _log(f"[WARN] 会话抓取失败: {type(exc).__name__}: {exc}")
                read_fail_round += 1
                err = str(exc or "").lower()
                if read_fail_round <= 2 and (
                    "socket hang up" in err
                    or "instrumentation process is not running" in err
                    or "could not proxy command" in err
                ):
                    try:
                        self.appium.reconnect()
                    except Exception:
                        pass
                    await asyncio.sleep(0.8)
                    continue
                break

            platform_nickname = _extract_chat_nickname(nodes)
            if not platform_nickname:
                has_input = any(n.resource_id == INPUT_ID for n in nodes)
                if has_input and self.expected_chat_nickname:
                    platform_nickname = self.expected_chat_nickname
                    _log(
                        f"[INFO] 会话昵称缺失，使用列表目标昵称兜底: {platform_nickname!r}"
                    )
                else:
                    _log("[WARN] 会话内未识别昵称，重试。")
                    read_fail_round += 1
                    if read_fail_round <= 2:
                        await asyncio.sleep(0.35)
                        continue
                    _log("[WARN] 会话内多次未识别昵称，返回列表。")
                    break
            read_fail_round = 0
            if self.expected_chat_nickname and platform_nickname != self.expected_chat_nickname:
                _log(
                    f"[WARN] 打开会话与目标不一致: expected={self.expected_chat_nickname!r}, "
                    f"actual={platform_nickname!r}"
                )
                break

            # 先滚到会话底部，避免停留在历史区域导致看不到最新顾客消息。
            if empty_round == 0:
                self.appium.scroll_chat_to_bottom()
                await asyncio.sleep(0.12)
                try:
                    nodes = self.appium.page_nodes()
                except Exception:
                    pass

            session_key = f"taobao:{self.store_id}:{platform_nickname}"
            extracted_list = _extract_recent_customer_messages(
                nodes=nodes,
                nickname=platform_nickname,
                limit=8,
            )
            if not extracted_list:
                if empty_round == 0:
                    wrappers_cnt = len([n for n in nodes if n.resource_id == CHAT_WRAPPER_ID])
                    _log(
                        f"[INFO][{session_key}] 会话提取为空，wrappers={wrappers_cnt}，"
                        "继续重试一次。"
                    )
                empty_round += 1
                await asyncio.sleep(0.45)
                continue

            event, extracted = self._build_new_event(
                session_key=session_key,
                platform_nickname=platform_nickname,
                extracted_list=extracted_list,
            )
            if event is None or extracted is None:
                empty_round += 1
                await asyncio.sleep(0.45)
                continue

            empty_round = 0

            now_ts = now_ms()
            if not self.state_store.can_auto_reply(event, now_ts, self.min_reply_interval_ms):
                self.state_store.unmark_incoming(event)
                _log(f"[INFO][{event.session_key()}] 回复节流，跳过。")
                await asyncio.sleep(0.35)
                continue

            _log(
                f"[INFO][{event.session_key()}] 收到消息 type={event.message_type}, "
                f"id={event.message_id}, text={event.text[:80]!r}"
            )

            decision = await self.decision_client.decide(event)
            _log(f"[INFO][{event.session_key()}] decision action={decision.action}, reason={decision.reason}")

            if decision.action == "manual":
                self.state_store.set_mode(event, "manual", now_ms())
                break

            reply_text = (decision.reply_text or "").strip()
            if decision.action != "send" or not reply_text:
                if decision.action == "send":
                    self.state_store.unmark_incoming(event)
                await asyncio.sleep(0.3)
                continue

            sent_ok = await self._send_text_reply(
                nodes=nodes,
                platform_nickname=platform_nickname,
                reply_text=reply_text,
            )
            if sent_ok:
                self.state_store.mark_reply(event, now_ms(), reply_text=reply_text)
                await self.decision_client.ack_delivery(
                    msg=event,
                    decision=decision,
                    sent_text=reply_text,
                    sent_image_urls=[],
                    status="sent",
                )
                _log(f"[INFO][{event.session_key()}] 已发送回复。")
            else:
                self.state_store.unmark_incoming(event)
                await self.decision_client.ack_delivery(
                    msg=event,
                    decision=decision,
                    sent_text="",
                    sent_image_urls=[],
                    status="failed",
                )
                _log(f"[WARN][{event.session_key()}] 发送失败，等待下轮重试。")
                # Send failure: leave chat to avoid local infinite spin.
                break

            # Race guard: short wait, then check again in same chat before leaving.
            await asyncio.sleep(0.45)

        self._back_to_list()
        self.expected_chat_nickname = ""
        self.expected_list_preview_sig = ""
        await asyncio.sleep(self.back_to_list_wait_sec)

    def _pick_preview_target(self, visible: list[PendingConversation]) -> Optional[PendingConversation]:
        if not visible:
            return None

        n = len(visible)
        start = self.list_cursor % n
        for step in range(n):
            conv = visible[(start + step) % n]
            if not conv.preview_text:
                continue

            session_key = f"taobao:{self.store_id}:{conv.nickname}"
            last_reply = self.state_store.get_last_reply_text(
                tenant_id=self.tenant_id,
                platform="taobao",
                store_id=self.store_id,
                session_key=session_key,
            )
            if _preview_is_self_reply(conv.preview_text, last_reply):
                if conv.preview_text:
                    self.preview_seen_sig[session_key] = _norm_for_compare(conv.preview_text)[:120]
                continue

            preview_sig = _norm_for_compare(conv.preview_text)[:120]
            if preview_sig and self.preview_seen_sig.get(session_key) == preview_sig:
                continue

            self.list_cursor = (start + step + 1) % n
            return conv
        return None

    def _build_new_event(
        self,
        *,
        session_key: str,
        platform_nickname: str,
        extracted_list: list[ExtractedMessage],
    ) -> tuple[Optional[IncomingMessage], Optional[ExtractedMessage]]:
        if not extracted_list:
            return None, None

        # 只处理最新一条顾客消息，避免历史堆积消息被逐条重复触发。
        cand = extracted_list[-1]

        media_url = (cand.media_url or "").strip()
        if cand.message_type == "image" and not media_url:
            media_url = self._capture_image_media(cand)
        message_id = _build_message_id(session_key, cand)
        event = IncomingMessage(
            tenant_id=self.tenant_id,
            platform="taobao",
            store_id=self.store_id,
            store_name=self.store_name or self.store_id,
            customer_id=platform_nickname,
            platform_nickname=platform_nickname,
            message_id=message_id,
            message_type=cand.message_type,
            text=cand.text,
            media_url=media_url,
            raw={
                "channel_capabilities": dict(CHANNEL_CAPABILITIES),
                "message_id_hint": cand.message_id_hint,
                "order_id": cand.order_id,
                "row_signature": cand.message_id_hint,
                "send_time_text": cand.send_time_text,
                "wrapper_bounds": cand.wrapper_bounds_sig,
                "image_bounds": cand.image_bounds_sig,
            },
            role="customer",
        )
        if not self.state_store.mark_incoming_if_new(event):
            return None, None
        return event, cand

    def _capture_image_media(self, extracted: ExtractedMessage) -> str:
        try:
            png = self.appium.screenshot_png()
        except Exception as exc:
            _log(f"[WARN] 图片截图失败: {type(exc).__name__}: {exc}")
            return ""

        day = time.strftime("%Y%m%d")
        out_dir = self.media_dir / day
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return ""

        sig = hashlib.md5(extracted.message_id_hint.encode("utf-8", errors="ignore")).hexdigest()[:10]
        ts = now_ms()
        full_path = out_dir / f"qn_{ts}_{sig}_full.png"
        try:
            full_path.write_bytes(png)
        except Exception as exc:
            _log(f"[WARN] 保存图片失败: {type(exc).__name__}: {exc}")
            return ""

        if extracted.image_bounds_sig:
            crop_path = out_dir / f"qn_{ts}_{sig}_crop.png"
            if self._crop_png_by_bounds(full_path=full_path, out_path=crop_path, bounds_sig=extracted.image_bounds_sig):
                return str(crop_path)

        return str(full_path)

    def _crop_png_by_bounds(self, *, full_path: Path, out_path: Path, bounds_sig: str) -> bool:
        parts = [p.strip() for p in str(bounds_sig or "").split(",")]
        if len(parts) != 4:
            return False
        try:
            x1, y1, x2, y2 = [int(p) for p in parts]
        except Exception:
            return False
        if x2 <= x1 or y2 <= y1:
            return False

        try:
            from PIL import Image
        except Exception:
            return False

        try:
            with Image.open(full_path) as im:
                w, h = im.size
                left = max(0, min(x1, w - 1))
                top = max(0, min(y1, h - 1))
                right = max(left + 1, min(x2, w))
                bottom = max(top + 1, min(y2, h))
                if right - left < 32 or bottom - top < 32:
                    return False
                crop = im.crop((left, top, right, bottom))
                crop.save(out_path, format="PNG")
            return True
        except Exception:
            return False

    async def _send_text_reply(
        self,
        *,
        nodes: list[UiNode],
        platform_nickname: str,
        reply_text: str,
    ) -> bool:
        before_count = _count_agent_reply_matches(
            nodes,
            reply_text,
            platform_nickname=platform_nickname,
        )
        reply_norm = _norm_for_compare(reply_text)

        try:
            input_el = self.appium.find_input_element()
        except Exception as exc:
            _log(f"[WARN] 未找到输入框: {type(exc).__name__}: {exc}")
            return False

        try:
            input_el.click()
        except Exception:
            pass

        try:
            input_el.clear()
            await asyncio.sleep(0.15)
        except Exception:
            pass

        # If draft still exists, clear by DEL key.
        try:
            left_text = _norm_for_compare(str(input_el.text or ""))
            if left_text:
                self.appium.press_backspace(min(max(len(left_text) + 40, 120), 500))
                await asyncio.sleep(0.15)
        except Exception:
            pass

        typed = False
        for _ in range(2):
            try:
                input_el.send_keys(reply_text)
                await asyncio.sleep(0.2)
                now_text = _norm_for_compare(str(input_el.text or ""))
                if now_text and (reply_norm in now_text or now_text in reply_norm):
                    typed = True
                    break
            except Exception:
                pass

        # Some OEM keyboards return empty input_el.text even when text is actually entered.
        # Don't hard fail here; still try send click.
        if not typed:
            _log("[WARN] 输入框文本回读不稳定，继续尝试点击发送。")

        clicked = False
        try:
            btn = self.appium.find_send_button()
            if btn is not None:
                btn.click()
                clicked = True
        except Exception:
            clicked = False
        if not clicked:
            try:
                self.appium.tap(1000, 1950)
                clicked = True
            except Exception:
                pass
        if not clicked:
            return False

        for _ in range(8):
            await asyncio.sleep(0.35)
            try:
                check_nodes = self.appium.page_nodes()
            except Exception:
                continue
            after_count = _count_agent_reply_matches(
                check_nodes,
                reply_text,
                platform_nickname=platform_nickname,
            )
            if after_count > before_count:
                return True

            # Fallback success signal: input box is cleared.
            in_text = _extract_input_text(check_nodes)
            if reply_norm and not _norm_for_compare(in_text):
                return True

        _log("[WARN] 点击发送后未检测到回显/输入框清空。")
        return False

    def _back_to_list(self) -> None:
        for _ in range(3):
            try:
                nodes = self.appium.page_nodes()
                if _detect_page_type(nodes) == "list":
                    return
            except Exception:
                pass
            try:
                self.appium.back()
            except Exception:
                return
            time.sleep(self.back_to_list_wait_sec)

    def _try_open_message_tab(self, nodes: list[UiNode]) -> None:
        candidates: list[UiNode] = []
        for n in nodes:
            val = (n.text or n.content_desc or "").strip()
            if val != "消息":
                continue
            if n.bounds.cy < 1600:
                continue
            candidates.append(n)
        if not candidates:
            return
        candidates.sort(key=lambda x: (x.bounds.cy, x.bounds.cx))
        target = candidates[-1]
        try:
            self.appium.tap(target.bounds.cx, target.bounds.cy)
            _log("[INFO] unknown 页面自动点击“消息”Tab。")
        except Exception:
            pass


def _log(msg: str) -> None:
    print(msg, flush=True)


def _parse_bounds(raw: str) -> Optional[Bounds]:
    m = re.match(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$", raw or "")
    if not m:
        return None
    x1, y1, x2, y2 = (int(m.group(i)) for i in range(1, 5))
    return Bounds(x1=x1, y1=y1, x2=x2, y2=y2)


def _parse_nodes(xml_text: str) -> list[UiNode]:
    root = ET.fromstring(xml_text)
    nodes: list[UiNode] = []
    for node in root.iter():
        bounds = _parse_bounds(node.attrib.get("bounds", ""))
        if bounds is None:
            continue
        nodes.append(
            UiNode(
                text=(node.attrib.get("text") or "").strip(),
                resource_id=(node.attrib.get("resource-id") or "").strip(),
                class_name=((node.attrib.get("class") or "").strip() or str(node.tag)),
                content_desc=(node.attrib.get("content-desc") or "").strip(),
                clickable=(node.attrib.get("clickable") == "true"),
                bounds=bounds,
            )
        )
    return nodes


def _has_offline_popup(nodes: list[UiNode]) -> bool:
    return any((n.text == "未处理离线消息") or (n.content_desc == "未处理离线消息") for n in nodes)


def _detect_page_type(nodes: list[UiNode]) -> str:
    rid_set = {n.resource_id for n in nodes if n.resource_id}
    if INPUT_ID in rid_set:
        return "chat"
    if TAB_LAYOUT_ID in rid_set:
        return "list"
    return "unknown"


def _is_time_like(text: str) -> bool:
    v = (text or "").strip()
    if not v:
        return False
    if re.match(r"^\d{1,2}:\d{2}$", v):
        return True
    if re.match(r"^(今天|昨天)\s+\d{1,2}:\d{2}$", v):
        return True
    if re.match(r"^星期[一二三四五六日天]\s+\d{1,2}:\d{2}$", v):
        return True
    if re.match(r"^周[一二三四五六日天]\s+\d{1,2}:\d{2}$", v):
        return True
    if re.match(r"^\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}$", v):
        return True
    if re.match(r"^\d{1,2}-\d{1,2}\s+\d{2}:\d{2}(:\d{2})?$", v):
        return True
    if re.match(r"^\d+\s*秒$", v):
        return True
    if re.match(r"^\d+\s*分钟$", v):
        return True
    if re.match(r"^\d+\s*分$", v):
        return True
    if v == "刚刚":
        return True
    if v.endswith("分钟前") or v.endswith("小时前"):
        return True
    return False


def _parse_need_reply_time(text: str) -> tuple[bool, int]:
    v = (text or "").strip()
    if not v:
        return False, 0
    if v == "刚刚":
        return True, 1
    m_sec = re.match(r"^(\d+)\s*秒$", v)
    if m_sec:
        return True, max(1, int(m_sec.group(1)))
    m_min = re.match(r"^(\d+)\s*分钟$", v)
    if m_min:
        return True, max(1, int(m_min.group(1)) * 60)
    m_m = re.match(r"^(\d+)\s*分$", v)
    if m_m:
        return True, max(1, int(m_m.group(1)) * 60)
    return False, 0


def _norm_for_compare(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip())


def _preview_is_self_reply(preview_text: str, last_reply_text: str) -> bool:
    preview = _norm_for_compare(preview_text)
    reply = _norm_for_compare(last_reply_text)
    if not preview or not reply:
        return False
    if any(0xE000 <= ord(ch) <= 0xF8FF for ch in preview):
        return False
    # 仅做“严格一致”判定，避免把顾客短消息（如“你好/在吗”）误判成我方已回。
    return preview == reply


def _iter_list_nickname_nodes(nodes: list[UiNode]) -> list[UiNode]:
    y_min = 260
    y_max = 1750
    out: list[UiNode] = []
    for n in nodes:
        if n.bounds.y1 < y_min or n.bounds.y2 > y_max:
            continue
        val = (n.content_desc or n.text or "").strip()
        if not val:
            continue
        if n.bounds.x1 < 150:
            continue
        if n.bounds.x1 > 460:
            continue
        if n.bounds.x2 > 560:
            continue
        if val in LIST_IGNORE_TEXT:
            continue
        if val in {"[未读]", "[已读]", "未读", "已读"}:
            continue
        if re.match(r"^\[[^\]]+\]$", val):
            continue
        if _is_time_like(val):
            continue
        if val.isdigit() and n.bounds.x2 <= 240:
            continue
        if len(val) > 48:
            continue
        if len(val) == 1 and ord(val[0]) >= 0xE000:
            continue
        out.append(n)
    return out


def _iter_list_preview_nodes(nodes: list[UiNode]) -> list[UiNode]:
    y_min = 260
    y_max = 1750
    out: list[UiNode] = []
    for n in nodes:
        if n.bounds.y1 < y_min or n.bounds.y2 > y_max:
            continue
        val = (n.content_desc or n.text or "").strip()
        if not val:
            continue
        if n.bounds.x1 < 160 or n.bounds.x1 > 950:
            continue
        if val in LIST_IGNORE_TEXT:
            continue
        if any(0xE000 <= ord(ch) <= 0xF8FF for ch in val):
            continue
        if _is_time_like(val):
            continue
        if val.isdigit():
            continue
        if re.match(r"^[\[\]（）()\-—_·•:：.，,。!！?？~～]+$", val):
            continue
        if len(val) > 120:
            continue
        out.append(n)
    return out


def _iter_list_time_nodes(nodes: list[UiNode]) -> list[UiNode]:
    y_min = 240
    y_max = 1760
    out: list[UiNode] = []
    for n in nodes:
        if n.bounds.y1 < y_min or n.bounds.y2 > y_max:
            continue
        val = (n.content_desc or n.text or "").strip()
        if not val:
            continue
        if n.bounds.x1 < 700:
            continue
        if n.bounds.x2 > 1080:
            continue
        if not _is_time_like(val):
            continue
        out.append(n)
    return out


def _iter_unread_badge_nodes(nodes: list[UiNode]) -> list[UiNode]:
    y_min = 260
    y_max = 1750
    out: list[UiNode] = []
    for n in nodes:
        if n.bounds.y1 < y_min or n.bounds.y2 > y_max:
            continue
        val = (n.content_desc or n.text or "").strip()
        if not val:
            continue
        if val in {"[未读]", "未读"}:
            out.append(n)
            continue
        if not val.isdigit():
            continue
        if n.bounds.x2 > 240:
            continue
        try:
            count = int(val)
        except Exception:
            continue
        if 0 < count <= 99:
            out.append(n)
    return out


def _extract_visible_conversations(nodes: list[UiNode]) -> list[PendingConversation]:
    nickname_nodes = _iter_list_nickname_nodes(nodes)
    unread_nodes = _iter_unread_badge_nodes(nodes)
    preview_nodes = _iter_list_preview_nodes(nodes)
    time_nodes = _iter_list_time_nodes(nodes)

    sessions: list[PendingConversation] = []
    sorted_nicks = sorted(nickname_nodes, key=lambda n: n.bounds.cy)
    for idx, nick in enumerate(sorted_nicks):
        nickname = (nick.content_desc or nick.text or "").strip()
        if not nickname:
            continue

        unread = 0
        for u in unread_nodes:
            if abs(nick.bounds.cy - u.bounds.cy) <= 70:
                uv = (u.content_desc or u.text or "").strip()
                if uv in {"[未读]", "未读"}:
                    unread = max(unread, 1)
                    continue
                try:
                    c = int(uv or "0")
                except Exception:
                    c = 0
                unread = max(unread, c)

        row_top = max(260, nick.bounds.y1 - 30)
        next_y = sorted_nicks[idx + 1].bounds.y1 if idx + 1 < len(sorted_nicks) else 1800
        row_bottom = min(1760, max(nick.bounds.y2 + 30, next_y - 20))

        preview_text = ""
        cands: list[UiNode] = []
        for p in preview_nodes:
            if p.bounds.cy < row_top or p.bounds.cy > row_bottom:
                continue
            val = (p.content_desc or p.text or "").strip()
            if not val or val == nickname:
                continue
            cands.append(p)
        if cands:
            cands.sort(key=lambda x: (x.bounds.cy, x.bounds.x1, x.bounds.x2))
            preview_text = (cands[-1].content_desc or cands[-1].text or "").strip()

        time_text = ""
        time_need_reply = False
        time_age_sec = 0
        tcands: list[UiNode] = []
        for t in time_nodes:
            if t.bounds.cy < row_top or t.bounds.cy > row_bottom:
                continue
            tcands.append(t)
        if tcands:
            tcands.sort(key=lambda x: (abs(x.bounds.cy - nick.bounds.cy), -x.bounds.x1))
            tval = (tcands[0].content_desc or tcands[0].text or "").strip()
            if tval:
                time_text = tval
                need_reply, age_sec = _parse_need_reply_time(tval)
                if need_reply:
                    time_need_reply = True
                    time_age_sec = age_sec

        sessions.append(
            PendingConversation(
                nickname=nickname,
                unread=unread,
                tap_x=nick.bounds.cx,
                tap_y=nick.bounds.cy,
                y=nick.bounds.cy,
                preview_text=preview_text,
                time_text=time_text,
                time_need_reply=time_need_reply,
                time_age_sec=time_age_sec,
            )
        )

    dedup: dict[str, PendingConversation] = {}
    for s in sorted(sessions, key=lambda x: x.y):
        if s.nickname and s.nickname not in dedup:
            dedup[s.nickname] = s
    return list(dedup.values())


def _extract_pending_conversations(nodes: list[UiNode]) -> list[PendingConversation]:
    visible = _extract_visible_conversations(nodes)
    out = [s for s in visible if s.unread > 0]
    out.sort(key=lambda x: (-x.unread, x.y))
    return out


def _extract_chat_nickname(nodes: list[UiNode]) -> str:
    cands: list[tuple[int, str]] = []
    for n in nodes:
        if n.bounds.y2 > 230:
            continue
        val = (n.content_desc or n.text or "").strip()
        if not val:
            continue
        if val in CHAT_IGNORE_TEXT or val in LIST_IGNORE_TEXT:
            continue
        if _is_time_like(val):
            continue
        if val.isdigit():
            continue
        if len(val) > 40:
            continue
        if n.bounds.x1 < 120 or n.bounds.x2 > 860:
            continue
        cands.append((n.bounds.x2 - n.bounds.x1, val))

    if not cands:
        return ""
    cands.sort(key=lambda x: (-x[0], len(x[1])))
    return cands[0][1]


def _extract_order_id_from_text(text: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    patterns = (
        r"(?:订单号|订单编号|平台单号)\s*[:：]?\s*([A-Za-z0-9_-]{8,})",
        r"\b([A-Za-z][0-9]{12,}|[0-9]{12,}|[0-9]{6,}-[0-9]{6,}|[0-9]{16,20})\b",
    )
    for p in patterns:
        m = re.search(p, value)
        if m:
            return (m.group(1) or "").strip()
    return ""


def _is_card_desc(desc: str, nickname: str) -> bool:
    v = (desc or "").strip()
    if not v:
        return False
    if v in CHAT_IGNORE_TEXT or v in LIST_IGNORE_TEXT:
        return False
    if v == nickname:
        return False
    if v in {"今天", "昨天"}:
        return False
    if _is_time_like(v):
        return False
    if any(k in v for k in ("商品详情页", "订单详情", "订单号", "退款", "物流", "发货", "售后")):
        return True
    if any(k in v for k in ("拍", "下单", "链接", "收货", "退货")) and len(v) >= 3:
        return True
    if re.match(r"^[¥￥]?\d+(\.\d+)?$", v):
        return True
    return False


def _in_wrapper_bounds(node: UiNode, wrapper: UiNode) -> bool:
    return (
        node.bounds.x1 >= wrapper.bounds.x1
        and node.bounds.x2 <= wrapper.bounds.x2
        and node.bounds.y1 >= wrapper.bounds.y1
        and node.bounds.y2 <= wrapper.bounds.y2
    )


def _looks_like_message_image(node: UiNode, wrapper: UiNode) -> bool:
    cls = (node.class_name or "").lower()
    rid = (node.resource_id or "").lower()
    desc = (node.content_desc or node.text or "").strip().lower()
    if "imageview" not in cls and "image" not in rid:
        return False
    w = max(0, node.bounds.x2 - node.bounds.x1)
    h = max(0, node.bounds.y2 - node.bounds.y1)
    # Skip avatar / tiny icons.
    if w < 90 and h < 90:
        return False
    # In left message wrapper, bubble image is usually to the right of avatar.
    if node.bounds.x1 <= wrapper.bounds.x1 + 55:
        return False
    # Avoid obvious decoration hints.
    if "头像" in desc:
        return False
    return True


def _bounds_sig(bounds: Bounds) -> str:
    return f"{bounds.x1},{bounds.y1},{bounds.x2},{bounds.y2}"


def _stable_row_bounds_sig(bounds: Bounds) -> str:
    w = max(0, bounds.x2 - bounds.x1)
    h = max(0, bounds.y2 - bounds.y1)
    return f"{bounds.x1},{bounds.x2},{w},{h}"


def _normalize_sendtime_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip())


def _extract_wrapper_sendtime(nodes: list[UiNode], wrapper: UiNode) -> str:
    inside: list[UiNode] = []
    above: list[UiNode] = []
    for n in nodes:
        val = (n.content_desc or n.text or "").strip()
        if not _is_time_like(val):
            continue
        if _in_wrapper_bounds(n, wrapper):
            inside.append(n)
            continue
        if n.bounds.cy > wrapper.bounds.y1:
            continue
        if wrapper.bounds.y1 - n.bounds.cy > 220:
            continue
        if n.bounds.cx < 260 or n.bounds.cx > 860:
            continue
        above.append(n)

    if inside:
        inside.sort(key=lambda x: x.bounds.cy, reverse=True)
        return _normalize_sendtime_text(inside[0].content_desc or inside[0].text or "")
    if above:
        above.sort(key=lambda x: x.bounds.cy, reverse=True)
        return _normalize_sendtime_text(above[0].content_desc or above[0].text or "")
    return ""


def _guess_wrapper_side(wrapper: UiNode, sender: str, nickname: str, nodes: list[UiNode]) -> str:
    if sender:
        return "left" if sender == nickname else "right"
    # Fallback: wrapper 本身通常是全宽容器（x1=0,x2=1080），不能用 wrapper.x1 判断左右。
    # 改为用“行内可见气泡内容”的横向坐标判断。
    content_nodes: list[UiNode] = []
    for n in nodes:
        if not _in_wrapper_bounds(n, wrapper):
            continue
        val = (n.text or n.content_desc or "").strip()
        rid = (n.resource_id or "").lower()
        cls = (n.class_name or "").lower()
        if val:
            if val in CHAT_IGNORE_TEXT or val in LIST_IGNORE_TEXT:
                continue
            if _is_time_like(val):
                continue
            content_nodes.append(n)
            continue
        if "imageview" in cls or "image" in rid:
            # 允许无文字的图片气泡参与左右判定
            content_nodes.append(n)
    if not content_nodes:
        return "left"
    min_x = min(n.bounds.x1 for n in content_nodes)
    max_x = max(n.bounds.x2 for n in content_nodes)
    if min_x >= 360:
        return "right"
    if max_x <= 760:
        return "left"
    # 中间值保守按左侧，避免误把顾客消息当客服消息漏回。
    return "left"


def _extract_recent_customer_messages(
    *,
    nodes: list[UiNode],
    nickname: str,
    limit: int = 8,
) -> list[ExtractedMessage]:
    y_min = 220
    y_max = 1785

    wrappers = [n for n in nodes if n.resource_id == CHAT_WRAPPER_ID and y_min <= n.bounds.y1 <= y_max]
    wrappers.sort(key=lambda n: n.bounds.cy)

    extracted: list[tuple[int, int, ExtractedMessage]] = []
    right_wrapper_indexes: set[int] = set()

    for idx, wrapper in enumerate(wrappers):
        sender = ""
        sender_nodes = [
            n for n in nodes if n.resource_id == CHAT_USER_NAME_ID and _in_wrapper_bounds(n, wrapper)
        ]
        if sender_nodes:
            sender_nodes.sort(key=lambda n: (n.bounds.y1, n.bounds.x1))
            sender = (sender_nodes[0].text or sender_nodes[0].content_desc or "").strip()
        side = _guess_wrapper_side(wrapper, sender, nickname, nodes)
        if side == "right":
            right_wrapper_indexes.add(idx)
        if side != "left":
            continue
        wrapper_sig = _stable_row_bounds_sig(wrapper.bounds)
        send_time_text = _extract_wrapper_sendtime(nodes, wrapper)
        row_signature_base = f"side={side}|sendtime={send_time_text or '-'}|bounds={wrapper_sig}"

        text_nodes = [
            n for n in nodes if n.resource_id == CHAT_TEXT_ID and _in_wrapper_bounds(n, wrapper)
        ]
        text_nodes.sort(key=lambda n: (n.bounds.y1, n.bounds.x1))

        text_parts: list[str] = []
        for tnode in text_nodes:
            v = (tnode.text or tnode.content_desc or "").strip()
            if not v:
                continue
            if v in CHAT_IGNORE_TEXT:
                continue
            if v == sender or v == nickname:
                continue
            if _is_time_like(v):
                continue
            if v not in text_parts:
                text_parts.append(v)

        # Fallback text extraction for cases where CHAT_TEXT_ID is absent/changed.
        if not text_parts:
            generic_text_nodes: list[UiNode] = []
            for cnode in nodes:
                if not _in_wrapper_bounds(cnode, wrapper):
                    continue
                cls = (cnode.class_name or "").lower()
                if "textview" not in cls:
                    continue
                v = (cnode.text or cnode.content_desc or "").strip()
                if not v:
                    continue
                if v in CHAT_IGNORE_TEXT:
                    continue
                if v == sender or v == nickname:
                    continue
                if _is_time_like(v):
                    continue
                if len(v) > 180:
                    continue
                generic_text_nodes.append(cnode)

            generic_text_nodes.sort(key=lambda n: (n.bounds.y1, n.bounds.x1))
            for g in generic_text_nodes:
                gv = (g.text or g.content_desc or "").strip()
                if gv and gv not in text_parts:
                    text_parts.append(gv)

        # Card fallback (within same wrapper)
        card_parts: list[str] = []
        if not text_parts:
            for cnode in nodes:
                if not _in_wrapper_bounds(cnode, wrapper):
                    continue
                v = (cnode.content_desc or "").strip()
                if not _is_card_desc(v, nickname):
                    continue
                if v not in card_parts:
                    card_parts.append(v)

        image_hits = 0
        image_best: Optional[UiNode] = None
        image_best_area = 0
        for inode in nodes:
            if not _in_wrapper_bounds(inode, wrapper):
                continue
            if _looks_like_message_image(inode, wrapper):
                image_hits += 1
                w = max(0, inode.bounds.x2 - inode.bounds.x1)
                h = max(0, inode.bounds.y2 - inode.bounds.y1)
                area = w * h
                if area > image_best_area:
                    image_best_area = area
                    image_best = inode

        if text_parts:
            merged_text = " | ".join(text_parts).strip()
            if merged_text:
                extracted.append(
                    (
                        wrapper.bounds.cy,
                        idx,
                        ExtractedMessage(
                            message_type="text",
                            text=merged_text,
                            message_id_hint=(
                                f"{row_signature_base}|type=text|text="
                                f"{hashlib.md5(merged_text.encode('utf-8', errors='ignore')).hexdigest()[:8]}"
                            ),
                            order_id=_extract_order_id_from_text(merged_text),
                            side=side,
                            send_time_text=send_time_text,
                            wrapper_bounds_sig=wrapper_sig,
                        ),
                    )
                )
        elif card_parts:
            merged_card = " | ".join(card_parts).strip()
            if merged_card:
                extracted.append(
                    (
                        wrapper.bounds.cy,
                        idx,
                        ExtractedMessage(
                            message_type="card",
                            text=merged_card,
                            message_id_hint=(
                                f"{row_signature_base}|type=card|text="
                                f"{hashlib.md5(merged_card.encode('utf-8', errors='ignore')).hexdigest()[:8]}"
                            ),
                            order_id=_extract_order_id_from_text(merged_card),
                            side=side,
                            send_time_text=send_time_text,
                            wrapper_bounds_sig=wrapper_sig,
                        ),
                    )
                )
        elif image_hits > 0:
            image_bounds_sig = _bounds_sig(image_best.bounds) if image_best is not None else ""
            extracted.append(
                (
                    wrapper.bounds.cy,
                    idx,
                    ExtractedMessage(
                        message_type="image",
                        text="[图片]",
                        message_id_hint=f"{row_signature_base}|type=image|img={image_bounds_sig or image_hits}",
                        order_id="",
                        side=side,
                        send_time_text=send_time_text,
                        wrapper_bounds_sig=wrapper_sig,
                        image_bounds_sig=image_bounds_sig,
                    ),
                )
            )

    # 只保留“最近一次客服消息之后”的顾客消息，避免历史顾客消息反复触发。
    last_right_idx = max(right_wrapper_indexes) if right_wrapper_indexes else -1
    if last_right_idx >= 0:
        extracted = [row for row in extracted if row[1] > last_right_idx]

    extracted.sort(key=lambda x: x[0])

    dedup: list[ExtractedMessage] = []
    seen: set[str] = set()
    for _, _, msg in extracted:
        key = msg.message_id_hint
        if key in seen:
            continue
        seen.add(key)
        dedup.append(msg)

    if limit > 0 and len(dedup) > limit:
        dedup = dedup[-limit:]
    return dedup


def _build_message_id(session_key: str, extracted: ExtractedMessage) -> str:
    src = f"{session_key}|{extracted.message_id_hint}"
    return hashlib.md5(src.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _count_agent_reply_matches(
    nodes: list[UiNode],
    reply_text: str,
    *,
    platform_nickname: str,
) -> int:
    probe = _norm_for_compare(reply_text)
    if not probe:
        return 0
    prefix = probe[: min(len(probe), 10)]

    wrappers = [n for n in nodes if n.resource_id == CHAT_WRAPPER_ID]
    total = 0
    for wrapper in wrappers:
        sender_nodes = [n for n in nodes if n.resource_id == CHAT_USER_NAME_ID and _in_wrapper_bounds(n, wrapper)]
        sender = ""
        if sender_nodes:
            sender_nodes.sort(key=lambda n: (n.bounds.y1, n.bounds.x1))
            sender = (sender_nodes[0].text or sender_nodes[0].content_desc or "").strip()

        if sender and sender == platform_nickname:
            continue
        if (not sender) and wrapper.bounds.x1 <= 280:
            # no sender label + left bubble -> most likely customer side
            continue

        text_nodes = [n for n in nodes if n.resource_id == CHAT_TEXT_ID and _in_wrapper_bounds(n, wrapper)]
        for n in text_nodes:
            val = _norm_for_compare(n.text or n.content_desc or "")
            if not val:
                continue
            if probe in val or (prefix and prefix in val):
                total += 1
                break
    return total


def _extract_input_text(nodes: list[UiNode]) -> str:
    for n in nodes:
        if n.resource_id == INPUT_ID:
            return (n.text or n.content_desc or "").strip()
    return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="淘宝千牛手机客户端 Worker（Appium + UiAutomator2）")
    parser.add_argument("--tenant-id", default="default", help="租户 ID")
    parser.add_argument("--store-id", default=DEFAULT_STORE_ID, help="店铺 ID")
    parser.add_argument("--store-name", default=DEFAULT_STORE_NAME, help="店铺展示名")

    parser.add_argument("--appium-server-url", default=DEFAULT_APPIUM_SERVER_URL, help="Appium server URL")
    parser.add_argument("--udid", required=True, help="Android 设备 UDID")
    parser.add_argument("--app-package", default=DEFAULT_APP_PACKAGE, help="App package")
    parser.add_argument("--app-activity", default=DEFAULT_APP_ACTIVITY, help="App main activity")

    parser.add_argument("--decision-url", default=DEFAULT_DECISION_URL, help="决策服务地址")
    parser.add_argument("--decision-key", default=DEFAULT_DECISION_KEY, help="决策服务 API Key")
    parser.add_argument("--fallback-text", default=DEFAULT_FALLBACK_REPLY, help="兜底回复")
    parser.add_argument("--state-db", default=DEFAULT_STATE_DB, help="本地 SQLite 状态文件")
    parser.add_argument("--media-dir", default=DEFAULT_MEDIA_DIR, help="图片消息截图保存目录")
    parser.add_argument("--decision-timeout-sec", type=float, default=DEFAULT_DECISION_TIMEOUT_SEC)
    parser.add_argument("--min-reply-interval-sec", type=float, default=0.8)
    parser.add_argument("--poll-interval-sec", type=float, default=1.2)
    parser.add_argument("--open-chat-wait-sec", type=float, default=0.8)
    parser.add_argument("--back-to-list-wait-sec", type=float, default=0.4)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if webdriver is None:
        raise RuntimeError(
            "appium_python_client_missing: 请先在当前环境安装 Appium-Python-Client"
        )

    decision_client = DecisionClient(
        base_url=args.decision_url,
        api_key=args.decision_key,
        timeout_sec=float(args.decision_timeout_sec),
        fallback_text=args.fallback_text,
    )
    state_store = LocalStateStore(db_path=args.state_db)

    _log(
        "[INFO] 启动参数: "
        f"udid={args.udid}, "
        f"appium={args.appium_server_url}, "
        f"store_id={args.store_id}, "
        f"store_name={args.store_name}, "
        f"decision_url={args.decision_url}, "
        f"state_db={args.state_db}, "
        f"media_dir={args.media_dir}, "
        f"timeout_sec={float(args.decision_timeout_sec)}"
    )

    appium_session = AppiumSession(
        server_url=args.appium_server_url,
        udid=args.udid,
        app_package=args.app_package,
        app_activity=args.app_activity,
    )

    runtime = WorkerRuntime(
        appium_session=appium_session,
        decision_client=decision_client,
        state_store=state_store,
        tenant_id=args.tenant_id,
        store_id=args.store_id,
        store_name=args.store_name,
        min_reply_interval_ms=max(0, int(float(args.min_reply_interval_sec) * 1000)),
        poll_interval_sec=max(0.3, float(args.poll_interval_sec)),
        open_chat_wait_sec=max(0.2, float(args.open_chat_wait_sec)),
        back_to_list_wait_sec=max(0.2, float(args.back_to_list_wait_sec)),
        media_dir=args.media_dir,
    )

    try:
        asyncio.run(runtime.run())
    finally:
        appium_session.quit()


if __name__ == "__main__":
    main()
