from __future__ import annotations

import json
import os
import re
import shlex
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .kuaimai_client import KuaimaiClient
from .qwen_vision_client import QwenVisionClient


PLATFORM_CODE_TO_CN = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "xhs": "小红书",
    "taobao": "淘宝",
    "tianmao": "淘宝",
    "pdd": "拼多多",
    "kuaishou": "快手",
    "wechat": "视频号",
    "weixin": "视频号",
    "shipinhao": "视频号",
}

PLATFORM_CN_TO_DB_CODE = {
    "抖音": "douyin",
    "小红书": "xiaohongshu",
    "淘宝": "taobao",
    "拼多多": "pdd",
    "快手": "kuaishou",
    "视频号": "weixin",
}

IDENTITY_LOOKUP_PLATFORMS = (
    "淘宝",
    "抖音",
    "快手",
    "视频号",
    "小红书",
    "拼多多",
)

RECLASS_CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "missing_item": (
        "少发", "漏发", "少件", "缺件", "缺一件", "少一件", "空袋", "空袋子", "空包", "漏寄", "只发了一件",
    ),
    "wrong_item": (
        "发错", "错发", "发成", "寄错", "货不对", "不是这个", "不是我买的",
        "款式不对", "颜色不对", "尺码不对", "码数不对",
    ),
    "quality": (
        "质量", "破洞", "烂", "破了", "开线", "掉色", "污", "脏", "瑕疵", "线头", "坏了", "漏水", "有洞",
    ),
    "logistics": (
        "物流", "快递", "发货", "没发货", "催", "到货", "几天到", "单号", "签收", "在路上", "揽收", "派件",
    ),
    "address_modify": (
        "改地址", "地址错", "修改地址", "地址改",
    ),
    "shipping_or_price_diff": (
        "运费", "邮费", "免运费", "差价", "补差", "退一元", "多付",
    ),
    "size_or_style_consult": (
        "尺码", "码数", "多大", "能穿吗", "偏大", "偏小", "身高", "男童", "女童", "要男款", "要女款",
    ),
    "refund_or_exchange": (
        "退款", "退货", "换货", "换码", "换色", "补偿", "赔偿", "理赔", "退差价", "售后",
    ),
    "order_query": (
        "订单号", "查订单", "查一下订单", "有没有订单", "查单", "这个订单", "订单",
    ),
    "human_transfer": (
        "转人工", "人工客服",
    ),
}

RECLASS_PRIORITY = (
    "missing_item",
    "wrong_item",
    "quality",
    "logistics",
    "address_modify",
    "shipping_or_price_diff",
    "size_or_style_consult",
    "refund_or_exchange",
    "order_query",
    "human_transfer",
)

RECLASS_TIME_RE = re.compile(
    r"((\d{4}年)?\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2})|(昨天\s*\d{1,2}:\d{2})|(今天\s*\d{1,2}:\d{2})"
)

RECLASS_CATEGORY_DISPLAY: dict[str, str] = {
    "missing_item": "漏发/少件/空包",
    "wrong_item": "发错货/错款错色错码",
    "quality": "质量问题",
    "logistics": "物流/发货",
    "address_modify": "改地址",
    "shipping_or_price_diff": "运费/差价",
    "size_or_style_consult": "尺码/选款咨询",
    "refund_or_exchange": "退款/退货/换货",
    "order_query": "订单查询/核单",
    "human_transfer": "转人工",
    "unsupported_message": "不支持消息类型",
    "empty_or_noise": "空消息/噪声",
    "greeting_ack": "寒暄/确认类短句",
    "pre_sale_consult": "售前咨询",
    "other_actionable": "其他待人工研判",
    "clarify_needed": "需追问澄清",
}


def _env_enabled(name: str) -> bool:
    return str(os.getenv(name, "0") or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_string_list(raw: str, *, separator: str) -> tuple[str, ...]:
    """Parse a JSON string array or a deliberately simple delimited list."""
    value = str(raw or "").strip()
    if not value:
        return ()

    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return ()
        if not isinstance(parsed, list):
            return ()
        items = parsed
    else:
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        items = []
        for line in normalized.split("\n"):
            items.extend(line.split(separator))

    result: list[str] = []
    for item in items:
        entry = str(item or "").strip()
        if entry and entry not in result:
            result.append(entry)
    return tuple(result)


@dataclass
class BackendResult:
    ok: bool
    message: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "data": self.data,
        }


class ToolBackendService:
    def __init__(
        self,
        *,
        identity_base_url: str,
        identity_org_id: str,
        identity_room_name: str,
        identity_api_key: str,
        kuaimai_client: KuaimaiClient,
        qwen_vision_client: QwenVisionClient,
        timeout_sec: float,
        clawbot_db_path: str = "",
        admin_tools_enabled: bool | None = None,
        file_read_enabled: bool | None = None,
        shell_exec_enabled: bool | None = None,
        file_read_roots: str | None = None,
        shell_allowed_commands: str | None = None,
    ) -> None:
        self.identity_base_url = (identity_base_url or "").strip().rstrip("/")
        self.identity_org_id = (identity_org_id or "").strip()
        self.identity_room_name = (identity_room_name or "").strip()
        self.identity_api_key = (identity_api_key or "").strip()
        self.kuaimai = kuaimai_client
        self.qwen_vision = qwen_vision_client
        self.timeout_sec = max(1.0, float(timeout_sec))
        self.clawbot_db_path = str(clawbot_db_path or "").strip()
        if admin_tools_enabled is None:
            admin_tools_enabled = str(
                os.getenv("CLAWBOT_ADMIN_TOOLS_ENABLED", "0") or ""
            ).strip().lower() in {"1", "true", "yes", "on"}
        self.admin_tools_enabled = bool(admin_tools_enabled)
        if file_read_enabled is None:
            file_read_enabled = _env_enabled("CLAWBOT_ENABLE_FILE_READ_TOOL")
        if shell_exec_enabled is None:
            shell_exec_enabled = _env_enabled("CLAWBOT_ENABLE_SHELL_EXEC_TOOL")
        self.file_read_enabled = self.admin_tools_enabled or bool(file_read_enabled)
        self.shell_exec_enabled = self.admin_tools_enabled or bool(shell_exec_enabled)

        roots_raw = (
            os.getenv("CLAWBOT_FILE_READ_ROOTS", "")
            if file_read_roots is None
            else file_read_roots
        )
        commands_raw = (
            os.getenv("CLAWBOT_SHELL_ALLOWED_COMMANDS", "")
            if shell_allowed_commands is None
            else shell_allowed_commands
        )
        self.file_read_roots = _parse_string_list(roots_raw, separator=os.pathsep)
        self.shell_allowed_commands = _parse_string_list(commands_raw, separator=";;")
        self.file_read_allow_all = "*" in self.file_read_roots
        self.shell_exec_allow_all = "*" in self.shell_allowed_commands

    def handle(self, *, tool_name: str, payload: dict[str, Any]) -> BackendResult:
        args_raw = payload.get("args")
        event_raw = payload.get("event")
        args = args_raw if isinstance(args_raw, dict) else {}
        event = event_raw if isinstance(event_raw, dict) else {}

        if tool_name == "order_lookup":
            return self._order_lookup(args=args, event=event)
        if tool_name == "logistics_lookup":
            return self._logistics_lookup(args=args, event=event)
        if tool_name == "aftersale_list_lookup":
            if not _env_enabled("CLAWBOT_ENABLE_ADMIN_QUERY_TOOLS"):
                return BackendResult(ok=False, message="admin_query_tools_disabled", data={})
            return self._aftersale_list_lookup(args=args, event=event)
        if tool_name == "aftersale_lookup_by_order_id":
            return self._aftersale_lookup_by_order_id(args=args, event=event)
        if tool_name == "approve_refund_by_order_id":
            if not _env_enabled("CLAWBOT_ENABLE_REFUND_WRITE_TOOL"):
                return BackendResult(ok=False, message="refund_write_tool_disabled", data={})
            return self._approve_refund_by_order_id(args=args, event=event)
        if tool_name == "refund_history_lookup":
            return self._refund_history_lookup(args=args, event=event)
        if tool_name == "refund_case_lookup":
            return self._refund_case_lookup(args=args, event=event)
        if tool_name == "blacklist_add":
            if not _env_enabled("CLAWBOT_ENABLE_BLACKLIST_WRITE_TOOLS"):
                return BackendResult(ok=False, message="blacklist_write_tools_disabled", data={})
            return self._blacklist_add(args=args, event=event)
        if tool_name == "blacklist_remove":
            if not _env_enabled("CLAWBOT_ENABLE_BLACKLIST_WRITE_TOOLS"):
                return BackendResult(ok=False, message="blacklist_write_tools_disabled", data={})
            return self._blacklist_remove(args=args, event=event)
        if tool_name == "image_inspect":
            if not self.qwen_vision.enabled:
                return BackendResult(ok=False, message="image_inspect_disabled", data={})
            return self._image_inspect(args=args, event=event)
        if tool_name == "batch_reclass":
            if not _env_enabled("CLAWBOT_ENABLE_OFFLINE_ADMIN_TOOLS"):
                return BackendResult(ok=False, message="offline_admin_tools_disabled", data={})
            return self._batch_reclass(args=args, event=event)
        if tool_name == "other_actionable_review":
            if not _env_enabled("CLAWBOT_ENABLE_OFFLINE_ADMIN_TOOLS"):
                return BackendResult(ok=False, message="offline_admin_tools_disabled", data={})
            return self._other_actionable_review(args=args, event=event)
        if tool_name == "file_read":
            if not self.file_read_enabled:
                return BackendResult(ok=False, message="admin_tools_disabled", data={})
            return self._file_read(args=args, event=event)
        if tool_name == "shell_exec":
            if not self.shell_exec_enabled:
                return BackendResult(ok=False, message="admin_tools_disabled", data={})
            return self._shell_exec(args=args, event=event)
        return BackendResult(ok=False, message="unsupported_tool", data={})

    def _image_inspect(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        media_id = str(args.get("media_id") or event.get("media_id") or "").strip()
        media_path = str(args.get("media_path") or event.get("media_path") or "").strip()
        if not media_path:
            return BackendResult(ok=False, message="missing_media_path", data={"media_id": media_id})

        task = str(args.get("task") or "damage_check").strip().lower()
        if task in {"check_clothing_code_and_quality", "read_code", "ocr_text"}:
            task = "ocr_code"
        if task not in {"damage_check", "same_item_compare", "ocr_code"}:
            task = "damage_check"

        reference_image_url = (
            str(args.get("reference_image_url") or "").strip()
            or str(args.get("reference_url") or "").strip()
        )
        customer_text = (
            str(args.get("customer_text") or "").strip()
            or str(args.get("context_text") or "").strip()
            or str(event.get("text") or "").strip()
        )
        result = self.qwen_vision.inspect(
            image_url=media_path,
            task=task,
            customer_text=customer_text,
            reference_image_url=reference_image_url,
        )
        if not result.ok:
            return BackendResult(ok=False, message=result.message, data=result.data)

        data = dict(result.data)
        if media_id:
            data["media_id"] = media_id
        data["media_path"] = media_path
        if reference_image_url:
            data["reference_image_url"] = reference_image_url
        return BackendResult(ok=True, message="ok", data=data)

    def _order_lookup(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        order_id = (
            str(args.get("order_id") or "").strip()
            or str(args.get("tid") or "").strip()
            or str(args.get("platform_order_id") or "").strip()
        )
        if not order_id:
            order_id = self._extract_order_id_from_text(str(event.get("text") or ""))
        lookback_days = self._safe_int(args.get("lookback_days"), default=90, lo=1, hi=180)
        limit = self._safe_int(args.get("limit"), default=50, lo=1, hi=200)
        if order_id:
            shop_name = str(args.get("shop_name") or event.get("store_name") or "").strip()
            remote = self.kuaimai.orders_by_order_id(
                order_id=order_id,
                shop_name=shop_name,
                lookback_days=lookback_days,
                limit=min(limit, 50),
            )
            if not bool(remote.get("ok", False)):
                return BackendResult(
                    ok=False,
                    message=str(remote.get("message") or "kuaimai_order_lookup_failed"),
                    data=(remote.get("data") if isinstance(remote.get("data"), dict) else {}),
                )

            target_data = (
                dict(remote.get("data"))
                if isinstance(remote.get("data"), dict)
                else {}
            )
            target_orders_raw = target_data.get("orders")
            target_orders = target_orders_raw if isinstance(target_orders_raw, list) else []
            first_order = target_orders[0] if target_orders else {}
            platform = self._normalize_platform(
                str(args.get("platform") or event.get("platform") or "").strip()
            )
            platform_nickname = str(
                args.get("platform_nickname")
                or args.get("alias_value")
                or event.get("platform_nickname")
                or ""
            ).strip()
            resolved_shop_name = str(
                shop_name
                or first_order.get("shop_name")
                or target_data.get("shop_name")
                or ""
            ).strip()
            km_name = self._extract_km_name_from_orders(target_orders)
            merged: dict[str, Any] = {
                "found": bool(target_orders),
                "order_id": order_id,
                "shop_name": resolved_shop_name,
                "total": len(target_orders),
                "orders": target_orders,
                # history is returned by default for order-id lookup.
                "history_found": False,
                "history_total": 0,
                "history_orders": [],
                "history_shop_name": resolved_shop_name,
                "km_name": km_name,
                "identity": {
                    "platform": platform,
                    "shop_name": resolved_shop_name,
                    "room_name": str(args.get("room_name") or self.identity_room_name or "").strip(),
                    "platform_nickname": platform_nickname,
                    "km_name": km_name,
                    "km_identity_type": "from_order",
                    "dxl_nickname": "",
                    "platform_nickname_mapped": "",
                    "last_tid": str(first_order.get("order_id") or order_id),
                    "identity_source": "order_id_reverse",
                },
                "open_aftersale_found": False,
                "open_aftersale_total": 0,
                "open_aftersales": [],
            }

            if km_name:
                history_remote = self.kuaimai.orders_by_identity(
                    km_name=km_name,
                    shop_name=resolved_shop_name,
                    lookback_days=lookback_days,
                    limit=limit,
                )
                if bool(history_remote.get("ok", False)):
                    history_data = (
                        dict(history_remote.get("data"))
                        if isinstance(history_remote.get("data"), dict)
                        else {}
                    )
                    merged["history_found"] = bool(history_data.get("found"))
                    merged["history_total"] = int(history_data.get("total") or 0)
                    merged["history_orders"] = (
                        history_data.get("orders")
                        if isinstance(history_data.get("orders"), list)
                        else []
                    )
                    merged["history_shop_name"] = str(history_data.get("shop_name") or resolved_shop_name).strip()
                    merged["km_name"] = str(history_data.get("km_name") or km_name).strip()
                else:
                    merged["history_lookup_message"] = str(history_remote.get("message") or "").strip()
                    if isinstance(merged.get("identity"), dict):
                        merged["identity"]["identity_source"] = "order_id_reverse_partial"
            else:
                merged["history_lookup_message"] = "km_name_not_found_from_target_order"
                if isinstance(merged.get("identity"), dict):
                    merged["identity"]["identity_source"] = "order_id_reverse_no_km_name"

            source_orders = (
                merged.get("history_orders")
                if isinstance(merged.get("history_orders"), list) and merged.get("history_orders")
                else target_orders
            )
            self._attach_open_aftersales(
                data=merged,
                order_rows=(source_orders if isinstance(source_orders, list) else []),
                shop_name=resolved_shop_name,
                lookback_days=max(lookback_days, 90),
            )
            return BackendResult(ok=True, message="ok", data=merged)

        identity = self._resolve_identity_or_direct_km(args=args, event=event)
        if not identity.ok:
            return identity

        remote = self.kuaimai.orders_by_identity(
            km_name=str(identity.data.get("km_name") or ""),
            shop_name=str(identity.data.get("shop_name") or ""),
            lookback_days=lookback_days,
            limit=limit,
        )
        if not bool(remote.get("ok", False)):
            return BackendResult(
                ok=False,
                message=str(remote.get("message") or "kuaimai_order_lookup_failed"),
                data=(remote.get("data") if isinstance(remote.get("data"), dict) else {}),
            )
        data = dict((remote.get("data") if isinstance(remote.get("data"), dict) else {}))
        data["identity"] = identity.data
        self._attach_open_aftersales(
            data=data,
            order_rows=(data.get("orders") if isinstance(data.get("orders"), list) else []),
            shop_name=str(data.get("shop_name") or identity.data.get("shop_name") or ""),
            lookback_days=max(lookback_days, 90),
        )
        return BackendResult(ok=True, message="ok", data=data)

    def _logistics_lookup(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        order_id = (
            str(args.get("order_id") or "").strip()
            or str(args.get("tid") or "").strip()
            or str(args.get("platform_order_id") or "").strip()
        )
        if not order_id:
            order_id = self._extract_order_id_from_text(str(event.get("text") or ""))
        if not order_id:
            return BackendResult(ok=False, message="missing_order_id", data={})

        lookback_days = self._safe_int(args.get("lookback_days"), default=120, lo=1, hi=365)
        shop_name = str(args.get("shop_name") or event.get("store_name") or "").strip()
        detail_level = str(args.get("detail_level") or "summary").strip().lower()
        if detail_level not in {"summary", "trace"}:
            detail_level = "summary"
        remote = self.kuaimai.logistics_by_order(
            order_id=order_id,
            shop_name=shop_name,
            lookback_days=lookback_days,
            detail_level=detail_level,
        )
        return BackendResult(
            ok=bool(remote.get("ok", False)),
            message=str(remote.get("message") or "kuaimai_logistics_lookup_failed"),
            data=(remote.get("data") if isinstance(remote.get("data"), dict) else {}),
        )

    def _aftersale_list_lookup(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        lookback_days = self._safe_int(args.get("lookback_days"), default=30, lo=1, hi=365)
        limit = self._safe_int(args.get("limit"), default=0, lo=0, hi=5000)
        page_size = self._safe_int(args.get("page_size"), default=100, lo=1, hi=200)
        only_open_raw = args.get("only_open")
        if only_open_raw is None:
            only_open = True
        else:
            only_open = str(only_open_raw).strip().lower() in {"1", "true", "yes", "y"}
        shop_name = str(
            args.get("shop_name")
            or args.get("store_name")
            or event.get("store_name")
            or ""
        ).strip()
        required_aftersale_type = str(args.get("required_aftersale_type") or "已发货仅退款").strip()
        required_status_raw = args.get("required_status_text")
        if required_status_raw is None:
            required_status_text = "未解决"
        else:
            required_status_text = str(required_status_raw).strip()
        remote = self.kuaimai.aftersales_list(
            shop_name=shop_name,
            lookback_days=lookback_days,
            limit=limit,
            page_size=page_size,
            only_open=only_open,
            required_aftersale_type=required_aftersale_type,
            required_status_text=required_status_text,
        )
        return BackendResult(
            ok=bool(remote.get("ok", False)),
            message=str(remote.get("message") or "kuaimai_aftersale_list_lookup_failed"),
            data=(remote.get("data") if isinstance(remote.get("data"), dict) else {}),
        )

    def _aftersale_lookup_by_order_id(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        order_id = (
            str(args.get("order_id") or "").strip()
            or str(args.get("tid") or "").strip()
            or str(args.get("platform_order_id") or "").strip()
        )
        if not order_id:
            order_id = self._extract_order_id_from_text(str(event.get("text") or ""))
        if not order_id:
            return BackendResult(ok=False, message="missing_order_id", data={})

        lookback_days = self._safe_int(args.get("lookback_days"), default=180, lo=1, hi=365)
        limit = self._safe_int(args.get("limit"), default=20, lo=1, hi=100)
        only_open = str(args.get("only_open") or "0").strip().lower() in {"1", "true", "yes", "y"}
        shop_name = str(args.get("shop_name") or event.get("store_name") or "").strip()
        remote = self.kuaimai.aftersales_by_order_id(
            order_id=order_id,
            shop_name=shop_name,
            lookback_days=lookback_days,
            limit=limit,
            only_open=only_open,
        )
        return BackendResult(
            ok=bool(remote.get("ok", False)),
            message=str(remote.get("message") or "kuaimai_aftersale_lookup_failed"),
            data=(remote.get("data") if isinstance(remote.get("data"), dict) else {}),
        )

    def _approve_refund_by_order_id(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        order_id = (
            str(args.get("order_id") or "").strip()
            or str(args.get("tid") or "").strip()
            or str(args.get("platform_order_id") or "").strip()
        )
        if not order_id:
            order_id = self._extract_order_id_from_text(str(event.get("text") or ""))
        if not order_id:
            return BackendResult(ok=False, message="missing_order_id", data={})

        lookback_days = self._safe_int(args.get("lookback_days"), default=180, lo=1, hi=365)
        shop_name = str(args.get("shop_name") or event.get("store_name") or "").strip()

        raw_cap = args.get("max_refund_amount_yuan")
        try:
            max_refund_amount_yuan = float(raw_cap) if raw_cap is not None else 5.0
        except Exception:
            max_refund_amount_yuan = 5.0
        if max_refund_amount_yuan < 0.0:
            max_refund_amount_yuan = 5.0
        if max_refund_amount_yuan > 5.0:
            max_refund_amount_yuan = 5.0

        required_aftersale_type = str(args.get("required_aftersale_type") or "已发货仅退款").strip()
        if not required_aftersale_type:
            required_aftersale_type = "已发货仅退款"

        remote = self.kuaimai.approve_refund_by_order_id(
            order_id=order_id,
            shop_name=shop_name,
            lookback_days=lookback_days,
            max_refund_amount_yuan=max_refund_amount_yuan,
            required_aftersale_type=required_aftersale_type,
        )
        return BackendResult(
            ok=bool(remote.get("ok", False)),
            message=str(remote.get("message") or "kuaimai_approve_refund_failed"),
            data=(remote.get("data") if isinstance(remote.get("data"), dict) else {}),
        )

    def _refund_history_lookup(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        identity = self._resolve_identity_or_direct_km(args=args, event=event)
        if not identity.ok:
            return identity

        lookback_days = self._safe_int(args.get("lookback_days"), default=90, lo=1, hi=180)
        limit = self._safe_int(args.get("limit"), default=200, lo=1, hi=300)
        remote = self.kuaimai.refund_by_identity(
            km_name=str(identity.data.get("km_name") or ""),
            shop_name=str(identity.data.get("shop_name") or ""),
            lookback_days=lookback_days,
            limit=limit,
        )
        if not bool(remote.get("ok", False)):
            return BackendResult(
                ok=False,
                message=str(remote.get("message") or "kuaimai_refund_lookup_failed"),
                data=(remote.get("data") if isinstance(remote.get("data"), dict) else {}),
            )
        data = dict((remote.get("data") if isinstance(remote.get("data"), dict) else {}))
        data["identity"] = identity.data
        return BackendResult(ok=True, message="ok", data=data)

    def _refund_case_lookup(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        order_id = (
            str(args.get("order_id") or "").strip()
            or str(args.get("tid") or "").strip()
            or str(args.get("platform_order_id") or "").strip()
        )
        sid = str(args.get("sid") or "").strip()
        aftersale_id = str(args.get("as_id") or args.get("aftersale_id") or "").strip()
        if not order_id and not sid:
            return BackendResult(ok=False, message="missing_order_id_or_sid", data={})

        lookback_days = self._safe_int(args.get("lookback_days"), default=90, lo=1, hi=180)
        dialogue_limit = self._safe_int(args.get("dialogue_limit"), default=12, lo=1, hi=50)
        refund_history_limit = self._safe_int(args.get("refund_history_limit"), default=50, lo=1, hi=200)
        target_outer_id = str(
            args.get("target_outer_id")
            or os.getenv("CLAWBOT_TARGET_OUTER_ID", "")
            or os.getenv("TARGET_OUTER_ID", "")
        ).strip()

        platform = self._normalize_platform(
            str(args.get("platform") or event.get("platform") or "").strip()
        )
        shop_name = str(
            args.get("shop_name")
            or args.get("store_name")
            or event.get("store_name")
            or ""
        ).strip()

        aftersale = self._build_refund_case_aftersale(
            args=args,
            order_id=order_id,
            sid=sid,
            aftersale_id=aftersale_id,
            shop_name=shop_name,
            platform=platform,
            lookback_days=lookback_days,
        )

        resolved_order_id = str(aftersale.get("order_id") or order_id or "").strip()
        resolved_sid = str(aftersale.get("sid") or sid or "").strip()
        resolved_shop_name = str(aftersale.get("shop_name") or shop_name or "").strip()
        resolved_platform = self._normalize_platform(
            str(aftersale.get("platform") or platform or "").strip()
        )

        order_summary = self._build_refund_case_order_summary(
            order_id=resolved_order_id,
            shop_name=resolved_shop_name,
            lookback_days=lookback_days,
        )
        logistics_summary = self._build_refund_case_logistics(
            order_id=resolved_order_id,
            shop_name=resolved_shop_name,
            lookback_days=lookback_days,
        )
        gift_order = self._build_refund_case_gift_order(
            sid=resolved_sid,
            user_id=str(args.get("user_id") or args.get("buyer_id") or "").strip(),
            lookback_days=lookback_days,
            target_outer_id=target_outer_id,
        )
        identity = self._build_refund_case_identity(
            args=args,
            event=event,
            order_summary=order_summary,
            platform=resolved_platform,
            shop_name=resolved_shop_name,
        )
        dialogue_context = self._build_refund_case_dialogue_context(
            platform=resolved_platform,
            shop_name=resolved_shop_name,
            platform_nickname=str(identity.get("platform_nickname") or "").strip(),
            limit=dialogue_limit,
        )
        refund_history = self._build_refund_case_refund_history(
            km_name=str(identity.get("km_name") or "").strip(),
            shop_name=resolved_shop_name,
            lookback_days=lookback_days,
            limit=refund_history_limit,
        )

        warnings: list[str] = []
        if not bool(aftersale.get("found")):
            warnings.append("aftersale_not_confirmed")
        if not bool(order_summary.get("found")):
            warnings.append("order_not_found")
        if not bool(logistics_summary.get("found")):
            warnings.append("logistics_not_found")
        if str(identity.get("identity_status") or "") != "resolved":
            warnings.append(f"identity_{str(identity.get('identity_status') or 'unresolved')}")
        if not bool(dialogue_context.get("found")):
            warnings.append("dialogue_not_found")

        return BackendResult(
            ok=True,
            message="ok",
            data={
                "aftersale": aftersale,
                "gift_order": gift_order,
                "order_summary": order_summary,
                "logistics_summary": logistics_summary,
                "identity": identity,
                "dialogue_context": dialogue_context,
                "refund_history": refund_history,
                "lookup_meta": {
                    "lookback_days": lookback_days,
                    "dialogue_limit": dialogue_limit,
                    "refund_history_limit": refund_history_limit,
                    "target_outer_id": target_outer_id,
                    "warnings": warnings,
                    "partial": bool(warnings),
                },
            },
        )

    def _build_refund_case_aftersale(
        self,
        *,
        args: dict[str, Any],
        order_id: str,
        sid: str,
        aftersale_id: str,
        shop_name: str,
        platform: str,
        lookback_days: int,
    ) -> dict[str, Any]:
        base = {
            "found": False,
            "aftersale_id": aftersale_id,
            "sid": sid,
            "order_id": order_id,
            "shop_name": shop_name,
            "platform": platform,
            "refund_money_fen": self._safe_int(args.get("refundMoney"), default=0, lo=0, hi=10_000_000),
            "refund_money_yuan": round(
                self._safe_float(args.get("refundMoney"), default=0.0) / 100.0,
                2,
            ),
            "aftersale_type": str(
                args.get("aftersale_type")
                or args.get("afterSaleTypeText")
                or ""
            ).strip(),
            "status_text": str(args.get("status_text") or args.get("statusText") or "").strip(),
            "platform_status_text": str(
                args.get("platform_status_text")
                or args.get("platformStatusText")
                or ""
            ).strip(),
            "aftersale_reason": str(
                args.get("aftersale_reason")
                or args.get("reason")
                or args.get("reasonText")
                or ""
            ).strip(),
            "source_row": "input",
        }

        if not order_id:
            return base

        remote = self.kuaimai._collect_aftersale_rows_by_order_id(
            order_id=order_id,
            shop_name=shop_name,
            lookback_days=lookback_days,
            limit=20,
        )
        if not bool(remote.get("ok", False)):
            base["lookup_message"] = str(remote.get("message") or "aftersale_lookup_failed")
            return base

        data = remote.get("data")
        if not isinstance(data, dict):
            data = {}
        rows = data.get("rows")
        if not isinstance(rows, list):
            rows = []
        chosen = self._pick_refund_case_aftersale(
            rows=rows,
            aftersale_id=aftersale_id,
            sid=sid,
            refund_money_fen=base["refund_money_fen"],
        )
        if not isinstance(chosen, dict):
            base["lookup_message"] = "aftersale_not_found_in_order"
            base["candidate_count"] = len(rows)
            return base

        raw_row = dict(chosen)
        chosen_row = self.kuaimai._normalize_aftersale(raw_row)
        result = {
            "found": True,
            "aftersale_id": str(raw_row.get("id") or aftersale_id or "").strip(),
            "sid": str(raw_row.get("sid") or sid or "").strip(),
            "order_id": str(chosen_row.get("order_id") or order_id or "").strip(),
            "shop_name": str(chosen_row.get("shop_name") or shop_name or "").strip(),
            "platform": self._normalize_platform(
                str(chosen_row.get("platform") or platform or "").strip()
            ),
            "refund_money_fen": self._safe_int(
                round(self._safe_float(chosen_row.get("platform_refund_amount"), default=0.0) * 100.0),
                default=base["refund_money_fen"],
                lo=0,
                hi=10_000_000,
            ),
            "refund_money_yuan": round(
                self._safe_float(chosen_row.get("platform_refund_amount"), default=base["refund_money_yuan"]),
                2,
            ),
            "paid_amount_yuan": round(
                self._safe_float(chosen_row.get("paid_amount"), default=0.0),
                2,
            ),
            "aftersale_type": str(chosen_row.get("aftersale_type") or base["aftersale_type"] or "").strip(),
            "status_text": str(chosen_row.get("status_text") or base["status_text"] or "").strip(),
            "platform_status_text": str(
                chosen_row.get("platform_status")
                or base["platform_status_text"]
                or ""
            ).strip(),
            "aftersale_reason": str(chosen_row.get("aftersale_reason") or base["aftersale_reason"] or "").strip(),
            "is_open": bool(chosen_row.get("is_open")),
            "open_reason": str(chosen_row.get("open_reason") or "").strip(),
            "updated_at_ms": self._safe_int(chosen_row.get("updated_at_ms"), default=0, lo=0, hi=4_102_444_800_000),
            "created_at_ms": self._safe_int(chosen_row.get("created_at_ms"), default=0, lo=0, hi=4_102_444_800_000),
            "source_row": "kuaimai_collect_aftersale_rows_by_order_id",
            "candidate_count": len(rows),
        }
        return result

    def _pick_refund_case_aftersale(
        self,
        *,
        rows: list[dict[str, Any]],
        aftersale_id: str,
        sid: str,
        refund_money_fen: int,
    ) -> dict[str, Any] | None:
        wanted_aftersale_id = str(aftersale_id or "").strip()
        wanted_sid = str(sid or "").strip()
        for row in rows:
            if not isinstance(row, dict):
                continue
            if wanted_aftersale_id and str(row.get("aftersale_id") or row.get("id") or "").strip() == wanted_aftersale_id:
                return row
        for row in rows:
            if not isinstance(row, dict):
                continue
            if wanted_sid and str(row.get("sid") or "").strip() == wanted_sid:
                return row
        if refund_money_fen > 0:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if "platform_refund_amount" in row:
                    cents = self._safe_int(
                        round(self._safe_float(row.get("platform_refund_amount"), default=0.0) * 100.0),
                        default=0,
                        lo=0,
                        hi=10_000_000,
                    )
                else:
                    cents = self._safe_int(row.get("refundMoney"), default=0, lo=0, hi=10_000_000)
                if cents == refund_money_fen:
                    return row
        for row in rows:
            if isinstance(row, dict):
                return row
        return None

    def _build_refund_case_gift_order(
        self,
        *,
        sid: str,
        user_id: str,
        lookback_days: int,
        target_outer_id: str,
    ) -> dict[str, Any]:
        if not sid:
            return {
                "found": False,
                "lookup_message": "missing_sid",
                "is_gift": False,
                "outer_ids": [],
                "matched_outer_id": "",
            }

        now_ms = int(time.time() * 1000)
        start_ms = now_ms - max(1, int(lookback_days)) * 86400 * 1000
        remote = self.kuaimai.trade_search_sids(
            sid=sid,
            user_id=user_id,
            start_ms=start_ms,
            end_ms=now_ms + 60 * 1000,
            page_no=1,
            page_size=20,
        )
        if not bool(remote.get("ok", False)):
            return {
                "found": False,
                "lookup_message": str(remote.get("message") or "trade_search_sids_failed"),
                "is_gift": False,
                "outer_ids": [],
                "matched_outer_id": "",
            }

        data = remote.get("data")
        if not isinstance(data, dict):
            data = {}
        rows = data.get("rows")
        if not isinstance(rows, list):
            rows = []
        outer_ids = self._extract_outer_ids_from_trade_detail_rows(rows=rows)
        target = str(target_outer_id or "").strip()
        normalized_outer_ids = {str(value).lower(): str(value) for value in outer_ids if str(value or "").strip()}
        matched = normalized_outer_ids.get(target.lower(), "") if target else ""
        return {
            "found": bool(rows),
            "lookup_message": "ok" if rows else "trade_detail_not_found",
            "is_gift": bool(matched),
            "outer_ids": outer_ids,
            "matched_outer_id": matched,
            "sid": sid,
            "target_outer_id": target,
        }

    def _extract_outer_ids_from_trade_detail_rows(self, *, rows: list[dict[str, Any]]) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            orders = row.get("orders")
            if not isinstance(orders, list):
                continue
            for item in orders:
                if not isinstance(item, dict):
                    continue
                for key in ("outerId", "sysOuterId"):
                    val = str(item.get(key) or "").strip()
                    if not val or val in seen:
                        continue
                    seen.add(val)
                    values.append(val)
        return values

    def _build_refund_case_order_summary(
        self,
        *,
        order_id: str,
        shop_name: str,
        lookback_days: int,
    ) -> dict[str, Any]:
        if not order_id:
            return {
                "found": False,
                "lookup_message": "missing_order_id",
                "order_id": "",
            }

        remote = self.kuaimai.orders_by_order_id(
            order_id=order_id,
            shop_name=shop_name,
            lookback_days=lookback_days,
            limit=10,
        )
        if not bool(remote.get("ok", False)):
            return {
                "found": False,
                "lookup_message": str(remote.get("message") or "order_lookup_failed"),
                "order_id": order_id,
                "shop_name": shop_name,
            }

        data = remote.get("data")
        if not isinstance(data, dict):
            data = {}
        orders = data.get("orders")
        if not isinstance(orders, list):
            orders = []
        chosen = self._pick_refund_case_order(orders=orders, order_id=order_id)
        if not isinstance(chosen, dict):
            return {
                "found": False,
                "lookup_message": "order_not_found",
                "order_id": order_id,
                "shop_name": shop_name,
                "candidate_count": len(orders),
            }

        return {
            "found": True,
            "lookup_message": "ok",
            "order_id": str(chosen.get("order_id") or order_id or "").strip(),
            "sid": str(chosen.get("sid") or "").strip(),
            "shop_name": str(chosen.get("shop_name") or shop_name or "").strip(),
            "platform": self._normalize_platform(str(chosen.get("platform") or "").strip()),
            "paid_amount_yuan": round(self._safe_float(chosen.get("amount"), default=0.0), 2),
            "order_status": str(chosen.get("status") or "").strip(),
            "item_names": list(chosen.get("item_names") or []) if isinstance(chosen.get("item_names"), list) else [],
            "item_specs": list(chosen.get("item_specs") or []) if isinstance(chosen.get("item_specs"), list) else [],
            "item_count": self._safe_int(chosen.get("item_count"), default=0, lo=0, hi=100000),
            "logistics_company": str(chosen.get("logistics_company") or "").strip(),
            "tracking_no": str(chosen.get("tracking_no") or "").strip(),
            "buyer_nick": str(chosen.get("buyer_nick") or "").strip(),
            "open_uid": str(chosen.get("open_uid") or "").strip(),
            "display_image_url": str(chosen.get("display_image_url") or "").strip(),
            "items": list(chosen.get("items") or []) if isinstance(chosen.get("items"), list) else [],
            "candidate_count": len(orders),
        }

    def _pick_refund_case_order(self, *, orders: list[dict[str, Any]], order_id: str) -> dict[str, Any] | None:
        wanted = str(order_id or "").strip()
        for row in orders:
            if not isinstance(row, dict):
                continue
            if str(row.get("order_id") or "").strip() == wanted:
                return row
        for row in orders:
            if isinstance(row, dict):
                return row
        return None

    def _build_refund_case_logistics(
        self,
        *,
        order_id: str,
        shop_name: str,
        lookback_days: int,
    ) -> dict[str, Any]:
        if not order_id:
            return {
                "found": False,
                "lookup_message": "missing_order_id",
                "order_id": "",
            }
        remote = self.kuaimai.logistics_by_order(
            order_id=order_id,
            shop_name=shop_name,
            lookback_days=lookback_days,
            detail_level="trace",
        )
        if not bool(remote.get("ok", False)):
            return {
                "found": False,
                "lookup_message": str(remote.get("message") or "logistics_lookup_failed"),
                "order_id": order_id,
                "shop_name": shop_name,
            }
        data = remote.get("data")
        if not isinstance(data, dict):
            data = {}
        result = dict(data)
        result["lookup_message"] = str(remote.get("message") or "ok")
        result["platform"] = self._normalize_platform(str(result.get("raw", {}).get("platform") if isinstance(result.get("raw"), dict) else "")) if isinstance(result.get("raw"), dict) else ""
        return result

    def _build_refund_case_identity(
        self,
        *,
        args: dict[str, Any],
        event: dict[str, Any],
        order_summary: dict[str, Any],
        platform: str,
        shop_name: str,
    ) -> dict[str, Any]:
        direct_platform_nickname = str(
            args.get("platform_nickname")
            or event.get("platform_nickname")
            or ""
        ).strip()
        direct_km_name = str(
            args.get("km_name")
            or order_summary.get("open_uid")
            or order_summary.get("buyer_nick")
            or ""
        ).strip()
        direct_dxl_nickname = str(args.get("dxl_nickname") or "").strip()
        room_name = str(args.get("room_name") or self.identity_room_name or "").strip()

        candidates: list[str] = []
        for value in (
            direct_platform_nickname,
            direct_km_name,
            direct_dxl_nickname,
        ):
            if value and value not in candidates:
                candidates.append(value)

        if not candidates:
            return {
                "identity_status": "missing_input",
                "platform": platform,
                "shop_name": shop_name,
                "room_name": room_name,
                "platform_nickname": direct_platform_nickname,
                "km_name": direct_km_name,
                "dxl_nickname": direct_dxl_nickname,
                "identity_source": "direct_input_only",
                "candidates": [],
            }

        items, lookup_errors = self._lookup_identity_items(
            query_values=candidates,
            platform=platform,
            room_name=room_name,
        )
        if len(items) == 1:
            item = dict(items[0])
            return {
                "identity_status": "resolved",
                "platform": platform,
                "shop_name": shop_name,
                "room_name": room_name,
                "platform_nickname": str(item.get("platform_nickname") or direct_platform_nickname or "").strip(),
                "km_name": str(item.get("km_name") or direct_km_name or "").strip(),
                "dxl_nickname": str(item.get("dxl_nickname") or direct_dxl_nickname or "").strip(),
                "km_identity_type": str(item.get("km_identity_type") or "").strip(),
                "last_tid": str(item.get("last_tid") or "").strip(),
                "identity_source": str(item.get("_source") or "identity_lookup").strip(),
                "lookup_errors": lookup_errors,
                "candidates": [item],
            }
        if len(items) > 1:
            return {
                "identity_status": "ambiguous",
                "platform": platform,
                "shop_name": shop_name,
                "room_name": room_name,
                "platform_nickname": direct_platform_nickname,
                "km_name": direct_km_name,
                "dxl_nickname": direct_dxl_nickname,
                "identity_source": "identity_lookup",
                "lookup_errors": lookup_errors,
                "candidates": items[:10],
            }
        return {
            "identity_status": "not_found",
            "platform": platform,
            "shop_name": shop_name,
            "room_name": room_name,
            "platform_nickname": direct_platform_nickname,
            "km_name": direct_km_name,
            "dxl_nickname": direct_dxl_nickname,
            "identity_source": "direct_input_only",
            "lookup_errors": lookup_errors,
            "candidates": [],
        }

    def _lookup_identity_items(
        self,
        *,
        query_values: list[str],
        platform: str,
        room_name: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if not self.identity_base_url or not self.identity_org_id:
            return [], ["identity_service_not_configured"]

        platforms = [platform] if platform else list(IDENTITY_LOOKUP_PLATFORMS)
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        errors: list[str] = []
        url = f"{self.identity_base_url}/statistic/lookup_user_identity"

        for query_value in query_values:
            alias_value = str(query_value or "").strip()
            if not alias_value:
                continue
            for query_platform in platforms:
                payload = {
                    "org_id": self.identity_org_id,
                    "platform": query_platform,
                    "room_name": room_name,
                    "alias_value": alias_value,
                }
                try:
                    data = self._post_json(url=url, payload=payload, api_key=self.identity_api_key)
                except TimeoutError:
                    errors.append(f"{query_platform}:timeout")
                    continue
                except urllib.error.URLError as exc:
                    errors.append(f"{query_platform}:{type(exc).__name__}")
                    continue
                except Exception as exc:
                    errors.append(f"{query_platform}:{type(exc).__name__}")
                    continue

                content = data.get("content")
                if not isinstance(content, dict):
                    continue
                rows = content.get("items")
                if not isinstance(rows, list):
                    rows = []
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    item = {
                        "platform_nickname": str(raw.get("platform_nickname") or "").strip(),
                        "km_name": str(raw.get("km_name") or "").strip(),
                        "dxl_nickname": str(raw.get("dxl_nickname") or "").strip(),
                        "km_identity_type": str(raw.get("km_identity_type") or "").strip(),
                        "last_tid": str(raw.get("last_tid") or "").strip(),
                        "_query_platform": query_platform,
                        "_query_value": alias_value,
                        "_source": "identity_lookup",
                    }
                    key = (
                        str(item.get("platform_nickname") or ""),
                        str(item.get("km_name") or ""),
                        str(item.get("dxl_nickname") or ""),
                    )
                    if not any(key):
                        continue
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(item)
        return items, errors

    def _build_refund_case_dialogue_context(
        self,
        *,
        platform: str,
        shop_name: str,
        platform_nickname: str,
        limit: int,
    ) -> dict[str, Any]:
        if not platform_nickname:
            return {
                "found": False,
                "lookup_message": "missing_platform_nickname",
                "lookup_key": {
                    "platform": self._normalize_platform_db_code(platform),
                    "store_name": shop_name,
                    "platform_nickname": "",
                },
                "turns": [],
            }
        if not self.clawbot_db_path:
            return {
                "found": False,
                "lookup_message": "clawbot_db_not_configured",
                "lookup_key": {
                    "platform": self._normalize_platform_db_code(platform),
                    "store_name": shop_name,
                    "platform_nickname": platform_nickname,
                },
                "turns": [],
            }

        db_path = os.path.abspath(os.path.expanduser(self.clawbot_db_path))
        if not os.path.isfile(db_path):
            return {
                "found": False,
                "lookup_message": "clawbot_db_not_found",
                "lookup_key": {
                    "platform": self._normalize_platform_db_code(platform),
                    "store_name": shop_name,
                    "platform_nickname": platform_nickname,
                },
                "turns": [],
            }

        rows = self._query_dialogue_context(
            db_path=db_path,
            platform=self._normalize_platform_db_code(platform),
            store_name=shop_name,
            platform_nickname=platform_nickname,
            limit=limit,
        )
        return {
            "found": bool(rows),
            "lookup_message": "ok" if rows else "dialogue_not_found",
            "lookup_key": {
                "platform": self._normalize_platform_db_code(platform),
                "store_name": shop_name,
                "platform_nickname": platform_nickname,
            },
            "turns": rows,
        }

    def _query_dialogue_context(
        self,
        *,
        db_path: str,
        platform: str,
        store_name: str,
        platform_nickname: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not platform or not store_name or not platform_nickname:
            return []

        capped = max(1, min(int(limit), 50))
        sql = """
            SELECT
                e.text,
                e.media_url,
                e.received_at_ms,
                CASE
                    WHEN e.platform = 'wecom'
                        THEN COALESCE(NULLIF(dr.sent_text, ''), d.reply_text, '')
                    ELSE COALESCE(dr.sent_text, '')
                END AS reply_text,
                CASE
                    WHEN e.platform = 'wecom'
                        THEN COALESCE(NULLIF(dr.reason, ''), d.reason, '')
                    ELSE COALESCE(dr.reason, '')
                END AS reason
            FROM events e
            LEFT JOIN decisions d ON d.trace_id = e.trace_id
            LEFT JOIN (
                SELECT r.tenant_id, r.platform, r.store_id, r.message_id, r.sent_text, r.reason, r.sent_at_ms
                FROM delivery_receipts r
                JOIN (
                    SELECT tenant_id, platform, store_id, message_id, MAX(sent_at_ms) AS max_sent_at_ms
                    FROM delivery_receipts
                    GROUP BY tenant_id, platform, store_id, message_id
                ) m
                ON r.tenant_id = m.tenant_id
                   AND r.platform = m.platform
                   AND r.store_id = m.store_id
                   AND r.message_id = m.message_id
                   AND r.sent_at_ms = m.max_sent_at_ms
            ) dr
            ON dr.tenant_id = e.tenant_id
               AND dr.platform = e.platform
               AND dr.store_id = e.store_id
               AND dr.message_id = e.message_id
            WHERE e.platform = ?
              AND e.store_name = ?
              AND e.platform_nickname = ?
            ORDER BY e.id DESC
            LIMIT ?
        """
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.5)
        conn.row_factory = sqlite3.Row
        try:
            raw_rows = conn.execute(sql, (platform, store_name, platform_nickname, capped)).fetchall()
        finally:
            conn.close()

        turns: list[dict[str, Any]] = []
        for row in reversed(raw_rows):
            customer_text = str(row["text"] or "").strip()
            media_url = str(row["media_url"] or "").strip()
            if not customer_text and media_url:
                customer_text = f"[图片消息 URL={media_url}]"
            turns.append(
                {
                    "customer": customer_text,
                    "agent": str(row["reply_text"] or "").strip(),
                    "reason": str(row["reason"] or "").strip(),
                    "created_at_ms": self._safe_int(row["received_at_ms"], default=0, lo=0, hi=4_102_444_800_000),
                }
            )
        return turns

    def _build_refund_case_refund_history(
        self,
        *,
        km_name: str,
        shop_name: str,
        lookback_days: int,
        limit: int,
    ) -> dict[str, Any]:
        if not km_name:
            return {
                "found": False,
                "lookup_message": "missing_km_name",
                "km_name": "",
                "shop_name": shop_name,
            }
        remote = self.kuaimai.refund_by_identity(
            km_name=km_name,
            shop_name=shop_name,
            lookback_days=lookback_days,
            limit=limit,
        )
        if not bool(remote.get("ok", False)):
            return {
                "found": False,
                "lookup_message": str(remote.get("message") or "refund_history_lookup_failed"),
                "km_name": km_name,
                "shop_name": shop_name,
            }
        data = remote.get("data")
        if not isinstance(data, dict):
            data = {}
        result = dict(data)
        result["lookup_message"] = str(remote.get("message") or "ok")
        return result

    def _blacklist_add(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        org_id = str(args.get("org_id") or self.identity_org_id or "").strip()
        if not org_id:
            return BackendResult(ok=False, message="missing_org_id", data={})

        wangwang_id = str(
            args.get("wangwang_id")
            or args.get("user_id")
            or args.get("km_name")
            or event.get("platform_nickname")
            or ""
        ).strip()
        if not wangwang_id:
            return BackendResult(ok=False, message="missing_wangwang_id", data={})

        days = self._safe_int(args.get("days"), default=7, lo=1, hi=3650)
        base_url = self._resolve_blacklist_base_url(args=args)
        if not base_url:
            return BackendResult(ok=False, message="missing_blacklist_base_url", data={})

        req_payload = {"org_id": org_id, "wangwang_id": wangwang_id, "days": days}
        try:
            remote = self._post_json(
                url=f"{base_url}/zhibo/add_black_house",
                payload=req_payload,
                api_key=self.identity_api_key,
            )
        except TimeoutError:
            return BackendResult(
                ok=False,
                message="blacklist_add_timeout",
                data={"org_id": org_id, "wangwang_id": wangwang_id, "days": days},
            )
        except urllib.error.URLError as exc:
            return BackendResult(
                ok=False,
                message=f"blacklist_add_request_failed:{type(exc).__name__}",
                data={"org_id": org_id, "wangwang_id": wangwang_id, "days": days},
            )
        except Exception as exc:
            return BackendResult(
                ok=False,
                message=f"blacklist_add_exception:{type(exc).__name__}",
                data={"org_id": org_id, "wangwang_id": wangwang_id, "days": days},
            )

        return self._normalize_blacklist_result(
            action="add",
            remote=remote,
            req_payload=req_payload,
        )

    def _blacklist_remove(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        org_id = str(args.get("org_id") or self.identity_org_id or "").strip()
        if not org_id:
            return BackendResult(ok=False, message="missing_org_id", data={})

        wangwang_id = str(
            args.get("wangwang_id")
            or args.get("user_id")
            or args.get("km_name")
            or event.get("platform_nickname")
            or ""
        ).strip()
        if not wangwang_id:
            return BackendResult(ok=False, message="missing_wangwang_id", data={})

        base_url = self._resolve_blacklist_base_url(args=args)
        if not base_url:
            return BackendResult(ok=False, message="missing_blacklist_base_url", data={})

        req_payload = {"org_id": org_id, "wangwang_id": wangwang_id}
        try:
            remote = self._post_json(
                url=f"{base_url}/zhibo/black_house_delete",
                payload=req_payload,
                api_key=self.identity_api_key,
            )
        except TimeoutError:
            return BackendResult(
                ok=False,
                message="blacklist_remove_timeout",
                data={"org_id": org_id, "wangwang_id": wangwang_id},
            )
        except urllib.error.URLError as exc:
            return BackendResult(
                ok=False,
                message=f"blacklist_remove_request_failed:{type(exc).__name__}",
                data={"org_id": org_id, "wangwang_id": wangwang_id},
            )
        except Exception as exc:
            return BackendResult(
                ok=False,
                message=f"blacklist_remove_exception:{type(exc).__name__}",
                data={"org_id": org_id, "wangwang_id": wangwang_id},
            )

        return self._normalize_blacklist_result(
            action="remove",
            remote=remote,
            req_payload=req_payload,
        )

    def _resolve_blacklist_base_url(self, *, args: dict[str, Any]) -> str:
        return str(
            args.get("base_url")
            or args.get("bridge_url")
            or self.identity_base_url
            or ""
        ).strip().rstrip("/")

    def _normalize_blacklist_result(
        self,
        *,
        action: str,
        remote: dict[str, Any],
        req_payload: dict[str, Any],
    ) -> BackendResult:
        payload = dict(remote) if isinstance(remote, dict) else {}
        code_raw = payload.get("code")
        code: int | None = None
        if code_raw is not None:
            try:
                code = int(float(code_raw))
            except Exception:
                code = None

        ok = False
        if isinstance(payload.get("ok"), bool):
            ok = bool(payload.get("ok"))
        elif isinstance(payload.get("success"), bool):
            ok = bool(payload.get("success"))
        elif code is not None:
            ok = code in {0, 200}
        else:
            text = str(payload.get("result") or payload.get("message") or "").strip().lower()
            ok = text in {"ok", "success", "succeeded"}

        if code is not None and (code >= 400 or code >= 40000):
            ok = False

        message = str(payload.get("result") or payload.get("message") or "").strip()
        if not message:
            message = "ok" if ok else f"blacklist_{action}_failed"

        return BackendResult(
            ok=ok,
            message=message,
            data={
                "action": action,
                "request": req_payload,
                "response": payload,
            },
        )

    def _resolve_identity_or_direct_km(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        """Prefer direct km_name (shop optional), fallback to identity mapping lookup."""
        platform = self._normalize_platform(
            str(args.get("platform") or event.get("platform") or "").strip()
        )
        shop_name = str(args.get("shop_name") or event.get("store_name") or "").strip()
        alias_value = (
            str(args.get("platform_nickname") or "").strip()
            or str(args.get("alias_value") or "").strip()
            or str(event.get("platform_nickname") or "").strip()
        )
        room_name = str(args.get("room_name") or self.identity_room_name or "").strip()

        km_name = (
            str(args.get("km_name") or "").strip()
            or str(args.get("buyer_nick") or "").strip()
            or str(args.get("open_uid") or "").strip()
        )
        if km_name:
            return BackendResult(
                ok=True,
                message="ok",
                data={
                    "platform": platform,
                    "shop_name": shop_name,
                    "room_name": room_name,
                    "platform_nickname": alias_value,
                    "km_name": km_name,
                    "km_identity_type": str(args.get("km_identity_type") or "").strip() or "direct",
                    "dxl_nickname": "",
                    "platform_nickname_mapped": "",
                    "last_tid": "",
                    "identity_source": "direct_km_name",
                },
            )

        return self._resolve_identity(args=args, event=event)

    def _resolve_identity(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        if not self.identity_base_url:
            return BackendResult(ok=False, message="identity_service_not_configured", data={})
        if not self.identity_org_id:
            return BackendResult(ok=False, message="identity_org_id_not_configured", data={})

        platform = self._normalize_platform(
            str(args.get("platform") or event.get("platform") or "").strip()
        )
        shop_name = str(args.get("shop_name") or event.get("store_name") or "").strip()
        alias_value = (
            str(args.get("platform_nickname") or "").strip()
            or str(args.get("alias_value") or "").strip()
            or str(event.get("platform_nickname") or "").strip()
        )
        room_name = str(args.get("room_name") or self.identity_room_name or "").strip()

        if not platform:
            return BackendResult(ok=False, message="missing_platform", data={})
        if not alias_value:
            return BackendResult(ok=False, message="missing_platform_nickname", data={})

        url = f"{self.identity_base_url}/statistic/lookup_user_identity"
        body = {
            "org_id": self.identity_org_id,
            "platform": platform,
            "room_name": room_name,
            "alias_value": alias_value,
        }
        data = self._post_json(url=url, payload=body, api_key=self.identity_api_key)
        content = data.get("content")
        if not isinstance(content, dict):
            return BackendResult(ok=False, message="identity_bad_response", data={"raw": data})

        result = str(content.get("result") or "").strip().lower()
        items = content.get("items")
        if not isinstance(items, list):
            items = []

        if result == "not_found" or not items:
            return BackendResult(ok=False, message="identity_not_found", data={})
        if result == "ambiguous" or len(items) != 1:
            return BackendResult(
                ok=False,
                message="identity_ambiguous",
                data={"candidates": items[:5]},
            )

        item = items[0] if isinstance(items[0], dict) else {}
        km_name = str(item.get("km_name") or "").strip()
        if not km_name:
            return BackendResult(ok=False, message="identity_empty_km_name", data={"raw": item})

        return BackendResult(
            ok=True,
            message="ok",
            data={
                "platform": platform,
                "shop_name": shop_name,
                "room_name": room_name,
                "platform_nickname": alias_value,
                "km_name": km_name,
                "km_identity_type": str(item.get("km_identity_type") or "").strip() or "open_uid",
                "dxl_nickname": str(item.get("dxl_nickname") or "").strip(),
                "platform_nickname_mapped": str(item.get("platform_nickname") or "").strip(),
                "last_tid": str(item.get("last_tid") or "").strip(),
            },
        )

    def _normalize_platform(self, value: str) -> str:
        if not value:
            return ""
        key = value.strip().lower()
        return PLATFORM_CODE_TO_CN.get(key, value.strip())

    def _normalize_platform_db_code(self, value: str) -> str:
        platform_cn = self._normalize_platform(value)
        if platform_cn in PLATFORM_CN_TO_DB_CODE:
            return PLATFORM_CN_TO_DB_CODE[platform_cn]
        key = str(value or "").strip().lower()
        if key in {"wechat", "weixin", "shipinhao"}:
            return "weixin"
        return key

    def _safe_int(self, raw: Any, *, default: int, lo: int, hi: int) -> int:
        try:
            value = int(float(raw))
        except Exception:
            value = default
        if value < lo:
            value = lo
        if value > hi:
            value = hi
        return value

    def _safe_float(self, raw: Any, *, default: float) -> float:
        try:
            return float(raw)
        except Exception:
            return float(default)

    def _extract_km_name_from_orders(self, orders: list[dict[str, Any]]) -> str:
        for row in orders:
            if not isinstance(row, dict):
                continue
            for key in ("open_uid", "buyer_nick"):
                val = str(row.get(key) or "").strip()
                if val:
                    return val
        return ""

    def _extract_order_ids_from_orders(self, orders: list[dict[str, Any]]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for row in orders:
            if not isinstance(row, dict):
                continue
            order_id = str(row.get("order_id") or "").strip()
            if not order_id or order_id in seen:
                continue
            seen.add(order_id)
            result.append(order_id)
        return result

    def _attach_open_aftersales(
        self,
        *,
        data: dict[str, Any],
        order_rows: list[dict[str, Any]],
        shop_name: str,
        lookback_days: int,
    ) -> None:
        all_order_ids = self._extract_order_ids_from_orders(order_rows)
        if not all_order_ids:
            data["open_aftersale_found"] = False
            data["open_aftersale_total"] = 0
            data["open_aftersales"] = []
            return

        refund_order_ids = self._extract_order_ids_from_orders(
            [
                row
                for row in order_rows
                if isinstance(row, dict) and bool(row.get("is_refund"))
            ]
        )
        if len(all_order_ids) <= 20:
            order_ids = all_order_ids
        elif refund_order_ids:
            order_ids = refund_order_ids[:20]
        else:
            order_ids = all_order_ids[:20]
        if len(order_ids) < len(all_order_ids):
            data["open_aftersale_checked_order_count"] = len(order_ids)
            data["open_aftersale_order_total"] = len(all_order_ids)

        remote = self.kuaimai.open_aftersales_by_order_ids(
            order_ids=order_ids,
            shop_name=str(shop_name or "").strip(),
            lookback_days=lookback_days,
            per_order_limit=10,
            total_limit=200,
        )
        if not bool(remote.get("ok", False)):
            data["open_aftersale_found"] = False
            data["open_aftersale_total"] = 0
            data["open_aftersales"] = []
            data["open_aftersale_lookup_message"] = str(remote.get("message") or "").strip()
            return

        remote_data = remote.get("data")
        if not isinstance(remote_data, dict):
            remote_data = {}
        open_items = remote_data.get("open_aftersales")
        if not isinstance(open_items, list):
            open_items = []
        data["open_aftersale_found"] = bool(open_items)
        data["open_aftersale_total"] = int(remote_data.get("total") or len(open_items))
        data["open_aftersales"] = open_items
        failed_ids = remote_data.get("failed_order_ids")
        if isinstance(failed_ids, list) and failed_ids:
            data["open_aftersale_failed_order_ids"] = [str(v) for v in failed_ids if str(v).strip()]

    def _extract_order_id_from_text(self, text: str) -> str:
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
                return str(m.group(1) or "").strip()
        return ""

    def _batch_reclass(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        role = str(event.get("role") or "").strip().lower()
        if role not in {"admin", "ops", "operator", "manager"}:
            return BackendResult(ok=False, message="tool_forbidden_for_role", data={})

        input_path = str(
            args.get("input_path") or "/tmp/weixin_kf_allshops_combined_for_analysis.json"
        ).strip()
        output_json_path = str(
            args.get("output_json_path") or "/tmp/weixin_kf_agent_reclass_result.json"
        ).strip()
        output_md_path = str(
            args.get("output_md_path") or "/tmp/weixin_kf_agent_reclass_summary.md"
        ).strip()
        max_items = self._safe_int(args.get("max_items"), default=0, lo=0, hi=50000)

        if not input_path:
            return BackendResult(ok=False, message="missing_input_path", data={})
        if not os.path.isfile(input_path):
            return BackendResult(ok=False, message="input_file_not_found", data={"input_path": input_path})

        try:
            with open(input_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:
            return BackendResult(ok=False, message=f"input_load_failed:{type(exc).__name__}", data={})

        items_raw: list[Any]
        if isinstance(payload, dict):
            items_raw = payload.get("items") if isinstance(payload.get("items"), list) else []
        elif isinstance(payload, list):
            items_raw = payload
        else:
            items_raw = []
        if not items_raw:
            return BackendResult(ok=False, message="input_items_empty", data={"input_path": input_path})

        items = items_raw[:max_items] if max_items > 0 else items_raw
        result_items: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()

        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            text = self._extract_customer_text_from_item(item)
            category, matched = self._classify_issue(text)
            counts[category] += 1
            result_items.append(
                {
                    "index": idx,
                    "shop": str(item.get("_shop") or ""),
                    "month": str(item.get("_month") or ""),
                    "platform_nickname": str(
                        ((item.get("user_metrics") if isinstance(item.get("user_metrics"), dict) else {}) or {}).get("platform_nickname")
                        or ""
                    ),
                    "category": category,
                    "matched_keywords": matched,
                    "customer_text": text,
                }
            )

        total = max(1, len(result_items))
        summary = {
            "total": len(result_items),
            "counts": dict(counts),
            "ratio": {k: round(v * 100.0 / total, 2) for k, v in counts.items()},
        }
        output_payload = {
            "ok": True,
            "input_path": input_path,
            "summary": summary,
            "items": result_items,
        }
        try:
            self._write_json(output_json_path, output_payload)
            self._write_markdown(output_md_path, summary)
        except Exception as exc:
            return BackendResult(
                ok=False,
                message=f"output_write_failed:{type(exc).__name__}",
                data={"output_json_path": output_json_path, "output_md_path": output_md_path},
            )

        return BackendResult(
            ok=True,
            message="ok",
            data={
                "input_path": input_path,
                "output_json_path": output_json_path,
                "output_md_path": output_md_path,
                "summary": summary,
            },
        )

    def _other_actionable_review(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        role = str(event.get("role") or "").strip().lower()
        if role not in {"admin", "ops", "operator", "manager"}:
            return BackendResult(ok=False, message="tool_forbidden_for_role", data={})

        input_path = str(
            args.get("input_path") or "/tmp/weixin_kf_allshops_combined_for_analysis.json"
        ).strip()
        output_json_path = str(
            args.get("output_json_path") or "/tmp/weixin_kf_other_actionable_review.json"
        ).strip()
        output_md_path = str(
            args.get("output_md_path") or "/tmp/weixin_kf_other_actionable_review.md"
        ).strip()
        max_items = self._safe_int(args.get("max_items"), default=0, lo=0, hi=10000)
        sample_count = self._safe_int(args.get("sample_count"), default=0, lo=0, hi=20)
        focus_detail_category = str(args.get("focus_detail_category") or "").strip()
        include_full_record = bool(args.get("include_full_record", False))

        if not input_path:
            return BackendResult(ok=False, message="missing_input_path", data={})
        if not os.path.isfile(input_path):
            return BackendResult(ok=False, message="input_file_not_found", data={"input_path": input_path})

        try:
            with open(input_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:
            return BackendResult(ok=False, message=f"input_load_failed:{type(exc).__name__}", data={})

        items_raw: list[Any]
        if isinstance(payload, dict):
            items_raw = payload.get("items") if isinstance(payload.get("items"), list) else []
        elif isinstance(payload, list):
            items_raw = payload
        else:
            items_raw = []
        if not items_raw:
            return BackendResult(ok=False, message="input_items_empty", data={"input_path": input_path})

        # First pass: keep only other_actionable.
        candidates: list[dict[str, Any]] = []
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            text = self._extract_customer_text_from_item(item)
            category, _ = self._classify_issue(text)
            if category != "other_actionable":
                continue
            candidates.append(item)

        if max_items > 0:
            candidates = candidates[:max_items]

        review_items: list[dict[str, Any]] = []
        detail_counts: Counter[str] = Counter()
        for idx, item in enumerate(candidates, start=1):
            text = self._extract_customer_text_from_item(item)
            detail_category, reason, followup = self._classify_other_actionable_detail(text)
            detail_counts[detail_category] += 1
            record = {
                "index": idx,
                "shop": str(item.get("_shop") or ""),
                "month": str(item.get("_month") or ""),
                "platform_nickname": str(
                    ((item.get("user_metrics") if isinstance(item.get("user_metrics"), dict) else {}) or {}).get("platform_nickname")
                    or ""
                ),
                "detail_category": detail_category,
                "reason": reason,
                "followup_suggestion": followup,
                "customer_text": text,
            }
            if include_full_record:
                record["full_record"] = item
            review_items.append(record)

        total_candidates = len(candidates)
        total_base = max(1, total_candidates)
        summary = {
            "total_other_actionable": total_candidates,
            "detail_counts": dict(detail_counts),
            "detail_ratio": {k: round(v * 100.0 / total_base, 2) for k, v in detail_counts.items()},
        }

        samples_by_detail: dict[str, list[dict[str, Any]]] = {}
        if sample_count > 0:
            for row in review_items:
                key = str(row.get("detail_category") or "")
                if not key:
                    continue
                bucket = samples_by_detail.setdefault(key, [])
                if len(bucket) >= sample_count:
                    continue
                bucket.append(row)

        focus_samples: list[dict[str, Any]] = []
        if focus_detail_category:
            focus_samples = list(samples_by_detail.get(focus_detail_category, []))
        elif sample_count > 0:
            focus_samples = list(samples_by_detail.get("clarify_needed", []))

        output_payload = {
            "ok": True,
            "input_path": input_path,
            "summary": summary,
            "items": review_items,
            "samples_by_detail": samples_by_detail,
            "focus_detail_category": focus_detail_category or ("clarify_needed" if sample_count > 0 else ""),
            "focus_samples": focus_samples,
        }
        try:
            self._write_json(output_json_path, output_payload)
            self._write_other_review_markdown(output_md_path, summary)
        except Exception as exc:
            return BackendResult(
                ok=False,
                message=f"output_write_failed:{type(exc).__name__}",
                data={"output_json_path": output_json_path, "output_md_path": output_md_path},
            )

        return BackendResult(
            ok=True,
            message="ok",
            data={
                "input_path": input_path,
                "output_json_path": output_json_path,
                "output_md_path": output_md_path,
                "summary": summary,
                "samples_by_detail": samples_by_detail,
                "focus_detail_category": output_payload.get("focus_detail_category"),
                "focus_samples": focus_samples,
            },
        )

    def _authorized_file_read_path(self, raw_path: str) -> tuple[Path | None, str]:
        if not self.file_read_roots:
            return None, "file_read_roots_not_configured"

        try:
            candidate = Path(raw_path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None, "file_read_path_invalid"

        if self.file_read_allow_all:
            return candidate, ""

        for raw_root in self.file_read_roots:
            try:
                root = Path(raw_root).expanduser().resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                continue
            if root.exists() and not root.is_dir():
                continue
            if candidate == root or root in candidate.parents:
                return candidate, ""
        return None, "file_read_path_denied"

    @staticmethod
    def _shell_command_has_control_syntax(command: str) -> bool:
        if "\n" in command or "\r" in command or "$(" in command or "`" in command:
            return True
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            return True
        control_tokens = {
            ";",
            "&",
            "&&",
            "|",
            "||",
            "|&",
            "<",
            "<<",
            "<<<",
            ">",
            ">>",
            "<>",
            "<&",
            ">&",
            "&>",
            "&>>",
            "(",
            ")",
        }
        return any(token in control_tokens for token in tokens)

    def _authorized_shell_rule(self, command: str) -> str:
        if not self.shell_allowed_commands:
            return ""
        if self.shell_exec_allow_all:
            return "*"

        for rule in self.shell_allowed_commands:
            if command == rule:
                return rule

        if self._shell_command_has_control_syntax(command):
            return ""
        for rule in self.shell_allowed_commands:
            if command.startswith(rule) and command[len(rule) : len(rule) + 1].isspace():
                return rule
        return ""

    def _file_read(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        path = str(args.get("path") or args.get("file_path") or "").strip()
        if not path:
            return BackendResult(ok=False, message="missing_path", data={})

        authorized_path, authorization_error = self._authorized_file_read_path(path)
        if authorized_path is None:
            return BackendResult(
                ok=False,
                message=authorization_error,
                data={"path": path},
            )
        resolved_path = str(authorized_path)
        if not authorized_path.is_file():
            return BackendResult(ok=False, message="file_not_found", data={"path": path})

        max_bytes = self._safe_int(args.get("max_bytes"), default=80000, lo=1000, hi=500000)
        start_line = self._safe_int(args.get("start_line"), default=1, lo=1, hi=2000000)
        line_count = self._safe_int(args.get("line_count"), default=0, lo=0, hi=2000000)
        mode = str(args.get("mode") or "text").strip().lower()
        if mode not in {"text", "raw"}:
            mode = "text"

        try:
            with authorized_path.open("rb") as fh:
                raw = fh.read(max_bytes + 1)
        except Exception as exc:
            return BackendResult(
                ok=False,
                message=f"file_read_failed:{type(exc).__name__}",
                data={"path": resolved_path},
            )

        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]

        try:
            text = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            encoding = "utf-8-replace"

        if mode == "text":
            lines = text.splitlines()
            st = max(1, int(start_line)) - 1
            if line_count > 0:
                ed = st + int(line_count)
                slice_lines = lines[st:ed]
            else:
                slice_lines = lines[st:]
            content = "\n".join(slice_lines)
        else:
            content = text

        return BackendResult(
            ok=True,
            message="ok",
            data={
                "path": resolved_path,
                "encoding": encoding,
                "truncated": truncated,
                "max_bytes": max_bytes,
                "start_line": start_line,
                "line_count": line_count,
                "content": content,
            },
        )

    def _shell_exec(self, *, args: dict[str, Any], event: dict[str, Any]) -> BackendResult:
        cmd = str(args.get("cmd") or args.get("command") or "").strip()
        if not cmd:
            return BackendResult(ok=False, message="missing_cmd", data={})

        if not self.shell_allowed_commands:
            return BackendResult(
                ok=False,
                message="shell_allowed_commands_not_configured",
                data={"cmd": cmd},
            )
        matched_rule = self._authorized_shell_rule(cmd)
        if not matched_rule:
            return BackendResult(
                ok=False,
                message="shell_command_denied",
                data={"cmd": cmd},
            )

        timeout_sec = self._safe_int(args.get("timeout_sec"), default=30, lo=1, hi=180)
        cwd = str(args.get("cwd") or "").strip() or None
        max_output = self._safe_int(args.get("max_output_chars"), default=12000, lo=1000, hi=120000)

        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=int(timeout_sec),
                cwd=cwd,
            )
            stdout = str(proc.stdout or "")
            stderr = str(proc.stderr or "")
            out_truncated = False
            err_truncated = False
            if len(stdout) > max_output:
                stdout = stdout[:max_output]
                out_truncated = True
            if len(stderr) > max_output:
                stderr = stderr[:max_output]
                err_truncated = True
            return BackendResult(
                ok=True,
                message="ok",
                data={
                    "cmd": cmd,
                    "allowed_by": matched_rule,
                    "cwd": cwd or "",
                    "timeout_sec": timeout_sec,
                    "exit_code": int(proc.returncode),
                    "stdout": stdout,
                    "stderr": stderr,
                    "stdout_truncated": out_truncated,
                    "stderr_truncated": err_truncated,
                },
            )
        except subprocess.TimeoutExpired as exc:
            return BackendResult(
                ok=False,
                message="exec_timeout",
                data={
                    "cmd": cmd,
                    "allowed_by": matched_rule,
                    "cwd": cwd or "",
                    "timeout_sec": timeout_sec,
                    "stdout": str(exc.stdout or "")[:max_output],
                    "stderr": str(exc.stderr or "")[:max_output],
                },
            )
        except Exception as exc:
            return BackendResult(
                ok=False,
                message=f"exec_failed:{type(exc).__name__}",
                data={"cmd": cmd, "allowed_by": matched_rule, "cwd": cwd or ""},
            )

    def _classify_other_actionable_detail(self, text: str) -> tuple[str, str, str]:
        value = (text or "").strip()
        if not value:
            return ("empty_or_noise", "empty_text", "请补充具体问题和订单号。")

        checks: list[tuple[str, tuple[str, ...], str, str]] = [
            ("address_modify", ("改地址", "地址错", "修改地址", "收货地址"), "address_keyword", "请发订单号和新收货地址（省市区街道门牌）。"),
            ("order_query", ("订单号", "查订单", "查单", "这个订单"), "order_query_keyword", "请发订单号或下单时间，我马上核对。"),
            ("refund_or_exchange", ("退款", "退货", "换货", "申请退", "申请换", "退回", "售后"), "refund_exchange_keyword", "请发订单号并说明要退款还是换货。"),
            ("shipping_or_price_diff", ("运费", "邮费", "差价", "补差", "多付"), "shipping_price_keyword", "请发订单号和金额，我核对后处理差价/运费。"),
            ("missing_item", ("少发", "漏发", "少件", "缺件", "空包", "空袋"), "missing_item_keyword", "请发订单号和包裹面单照片，我核实漏发并处理。"),
            ("wrong_item", ("发错", "错发", "寄错", "不是这个", "颜色不对", "尺码不对", "款式不对"), "wrong_item_keyword", "请发订单号和实物照片，我核实错发后安排处理。"),
            ("quality", ("质量", "破洞", "有洞", "脏", "瑕疵", "坏了", "漏水", "开线"), "quality_keyword", "请发订单号和问题部位照片，我马上核实处理。"),
            ("logistics", ("物流", "快递", "发货", "没发货", "催", "签收", "派件", "揽收", "单号"), "logistics_keyword", "请发订单号，我立刻帮您查物流进度。"),
            ("size_or_style_consult", ("尺码", "码数", "多大", "能穿吗", "偏大", "偏小", "男款", "女款", "颜色"), "size_style_keyword", "请告诉我商品名+身高体重/目标尺码颜色，我给您建议。"),
            ("pre_sale_consult", ("主播", "直播间", "链接", "拍", "下单"), "pre_sale_keyword", "这是售前咨询，请说明商品名和需求（尺码/颜色/数量）。"),
        ]
        for category, tokens, reason, followup in checks:
            if any(token in value for token in tokens):
                return (category, reason, followup)

        if re.fullmatch(r"[0-9\-\s]{8,}", value):
            return ("order_query", "numeric_text", "请确认这是否订单号，并补充下单时间。")
        if any(token in value for token in ("怎么", "咋", "为什么", "可以吗", "能不能")):
            return ("clarify_needed", "question_but_unclear", "请补充具体诉求（发货/质量/退换/地址）并附订单号。")
        return ("clarify_needed", "insufficient_context", "请补充具体问题和订单号，我马上处理。")

    def _extract_customer_text_from_item(self, item: dict[str, Any]) -> str:
        session_detail = item.get("session_detail") if isinstance(item.get("session_detail"), dict) else {}
        user_metrics = item.get("user_metrics") if isinstance(item.get("user_metrics"), dict) else {}
        raw_text = str(session_detail.get("raw_text") or "")
        nickname = str(user_metrics.get("platform_nickname") or "").strip()
        staff_name = str(user_metrics.get("staff_name") or "").strip()

        lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        speakers = {x for x in (nickname, staff_name, "智能助手") if x}
        customer_lines: list[str] = []
        idx = 0
        while idx + 2 < len(lines):
            speaker = lines[idx]
            time_token = lines[idx + 1]
            if speaker in speakers and RECLASS_TIME_RE.search(time_token):
                text = lines[idx + 2]
                if speaker == nickname:
                    customer_lines.append(text)
                idx += 3
                continue
            idx += 1

        if customer_lines:
            return "\n".join(customer_lines).strip()

        # Fallback for sessions without raw speaker separation.
        messages = session_detail.get("messages")
        if isinstance(messages, list):
            out: list[str] = []
            for row in messages:
                if not isinstance(row, dict):
                    continue
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                if any(token in text for token in ("智能助手", "很高兴为你服务", "正在接入中", "客服下班")):
                    continue
                out.append(text)
            if out:
                return "\n".join(out).strip()
        return ""

    def _classify_issue(self, text: str) -> tuple[str, list[str]]:
        value = (text or "").strip()
        if not value:
            return "empty_or_noise", []

        # Handle common numeric compensation phrase.
        value = re.sub(r"退\d+元", "退差价", value)
        value = re.sub(r"\s+", " ", value).strip()

        if "不支持的消息类型" in value:
            return "unsupported_message", ["不支持的消息类型"]

        matched_by_cat: dict[str, list[str]] = {}
        for category, patterns in RECLASS_CATEGORY_PATTERNS.items():
            hit_tokens = [token for token in patterns if token and token in value]
            if hit_tokens:
                matched_by_cat[category] = hit_tokens

        # Fallback categories for non-orderly but common operational text.
        if not matched_by_cat:
            if re.fullmatch(r"[0-9\-\s]{8,}", value):
                return "other_actionable", ["numeric_only"]
            if len(value) <= 4:
                return "greeting_ack", [value]
            if any(token in value for token in ("你好", "在吗", "谢谢", "好的", "嗯", "哦", "哈", "收到")) and len(value) <= 40:
                return "greeting_ack", []
            if any(token in value for token in ("主播", "直播间", "链接", "拍", "下单", "能穿吗", "可以穿吗")):
                return "pre_sale_consult", []
            if any(token in value for token in ("怎么", "咋", "为什么", "能不能", "可以吗", "什么意思")):
                return "other_actionable", []
            return "other_actionable", []

        for category in RECLASS_PRIORITY:
            if category in matched_by_cat:
                return category, matched_by_cat[category]
        fallback_cat = next(iter(matched_by_cat))
        return fallback_cat, matched_by_cat[fallback_cat]

    def _write_json(self, path: str, payload: dict[str, Any]) -> None:
        target = self._normalize_output_path(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    def _write_markdown(self, path: str, summary: dict[str, Any]) -> None:
        target = self._normalize_output_path(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
        ratio = summary.get("ratio") if isinstance(summary.get("ratio"), dict) else {}
        total = int(summary.get("total") or 0)
        lines = [
            "# 客服数据重分类结果",
            "",
            f"- 总会话数: {total}",
            "",
            "| 类别 | 数量 | 占比 |",
            "|---|---:|---:|",
        ]
        for key, val in sorted(counts.items(), key=lambda kv: kv[1], reverse=True):
            pct = float(ratio.get(key) or 0.0)
            display = RECLASS_CATEGORY_DISPLAY.get(key, key)
            lines.append(f"| {display} ({key}) | {int(val)} | {pct:.2f}% |")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def _write_other_review_markdown(self, path: str, summary: dict[str, Any]) -> None:
        target = self._normalize_output_path(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        counts = summary.get("detail_counts") if isinstance(summary.get("detail_counts"), dict) else {}
        ratio = summary.get("detail_ratio") if isinstance(summary.get("detail_ratio"), dict) else {}
        total = int(summary.get("total_other_actionable") or 0)
        lines = [
            "# Other Actionable 二次分析结果",
            "",
            f"- 待人工样本总数: {total}",
            "",
            "| 二次类别 | 数量 | 占比 |",
            "|---|---:|---:|",
        ]
        for key, val in sorted(counts.items(), key=lambda kv: kv[1], reverse=True):
            pct = float(ratio.get(key) or 0.0)
            display = RECLASS_CATEGORY_DISPLAY.get(key, key)
            lines.append(f"| {display} ({key}) | {int(val)} | {pct:.2f}% |")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def _normalize_output_path(self, path: str) -> str:
        value = os.path.abspath(os.path.expanduser(path or ""))
        if not value:
            raise ValueError("empty_output_path")
        export_root = os.path.abspath(
            os.path.expanduser(os.getenv("CLAWBOT_EXPORT_ROOT", "data/exports"))
        )
        if value.startswith("/tmp/") or os.path.commonpath([value, export_root]) == export_root:
            return value
        raise ValueError("output_path_not_allowed")

    def _post_json(self, *, url: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url=url, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Accept", "application/json")
        if api_key:
            req.add_header("X-Api-Key", api_key)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise TimeoutError("identity_lookup_timeout") from exc
            raise
        except TimeoutError:
            raise

        if not raw:
            return {}
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {}
