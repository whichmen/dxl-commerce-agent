from __future__ import annotations

import math
import os
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from kefu_tool_backend.tool_backend_service import ToolBackendService


_AMOUNT_EPSILON = 0.01
_RETURN_HINTS = (
    "寄件人签收",
    "发件人签收",
    "商家签收",
    "成功退回",
    "退回至寄件人",
    "退件",
    "退回",
    "拒收",
    "拦截",
)
_RETURN_SIGNED_HINTS = (
    "寄件人签收",
    "发件人签收",
    "商家签收",
    "成功退回",
    "退回签收",
    "拦截退回已签收",
)
_BUYER_SIGN_HINTS = (
    "买家已签收",
    "已签收",
    "已代收",
    "妥投",
    "已取走",
    "已取件",
)
_ORDER_SIGNED_HINTS = (
    "FINISHED",
    "已签收",
    "已收货",
    "待确认收货",
)
_ORDER_CLOSED_HINTS = (
    "CLOSED",
    "交易关闭",
    "已关闭",
    "已取消",
    "退款成功",
    "已退款",
)
_ORDER_UNSHIPPED_HINTS = (
    "WAIT_PACKAGE",
    "WAIT_SEND",
    "待发货",
    "待商家发货",
)
_ORDER_SHIPPED_HINTS = (
    "已发货",
    "运输中",
    "派送中",
    "待取件",
    "待签收",
    "待收货",
    "已揽收",
    "已揽件",
)
_AFTERSALE_EXCLUDED_HINTS = (
    "已作废",
    "作废",
    "已取消",
    "取消申请",
    "交易关闭",
    "已关闭",
)
_GIFT_OUTER_ID = str(
    os.getenv("CLAWBOT_TARGET_OUTER_ID", "") or os.getenv("TARGET_OUTER_ID", "") or ""
).strip().lower()


def build_intercept_snapshot(
    *,
    backend: ToolBackendService,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload_obj = payload if isinstance(payload, dict) else {}
    args_obj = payload_obj.get("args")
    args = dict(args_obj) if isinstance(args_obj, dict) else {}

    lookback_days = _safe_int(args.get("lookback_days"), default=90, lo=1, hi=365)
    # limit=0 means scan the full candidate set within upstream pagination bounds.
    limit = _safe_int(args.get("limit"), default=0, lo=0, hi=5000)
    page_size = _safe_int(args.get("page_size"), default=100, lo=1, hi=200)
    only_open = _as_bool(args.get("only_open"), default=False)
    shop_name = str(args.get("shop_name") or args.get("store_name") or "").strip()
    required_aftersale_type = str(args.get("required_aftersale_type") or "已发货仅退款").strip() or "已发货仅退款"
    required_status_text = str(args.get("required_status_text") or "").strip()
    include_resolved_failed_followup = _as_bool(args.get("include_resolved_failed_followup"), default=False)
    include_logistics_warnings = _as_bool(args.get("include_logistics_warnings"), default=False)
    warning_lookback_days = _safe_int(
        args.get("warning_lookback_days"),
        default=min(max(lookback_days, 7), 30),
        lo=1,
        hi=60,
    )
    warning_limit = _safe_int(args.get("warning_limit"), default=limit, lo=1, hi=200)
    warning_scan_all = _as_bool(args.get("warning_scan_all"), default=False)
    warning_scan_limit = _safe_int(
        args.get("warning_scan_limit"),
        default=max(warning_limit * 4, 120),
        lo=20,
        hi=500,
    )
    warning_stale_hours = _safe_int(args.get("warning_stale_hours"), default=24, lo=1, hi=240)

    aftersale_result = _fetch_aftersale_candidates(
        backend=backend,
        lookback_days=lookback_days,
        limit=limit,
        page_size=page_size,
        only_open=only_open,
        shop_name=shop_name,
        required_aftersale_type=required_aftersale_type,
        required_status_text=required_status_text,
    )
    if not aftersale_result.ok:
        return {
            "ok": False,
            "message": aftersale_result.message,
            "data": {},
        }

    aftersale_data = dict(aftersale_result.data or {})
    rows = aftersale_data.get("aftersales")
    aftersales = list(rows) if isinstance(rows, list) else []
    primary_aftersale_ids = {
        str(dict(item).get("aftersale_id") or "").strip()
        for item in aftersales
        if isinstance(item, dict)
    }
    resolved_followup_aftersales: list[dict[str, Any]] = []
    if include_resolved_failed_followup and (only_open or required_status_text):
        resolved_result = _fetch_aftersale_candidates(
            backend=backend,
            lookback_days=max(lookback_days, 90),
            limit=0,
            page_size=200,
            only_open=False,
            shop_name=shop_name,
            required_aftersale_type=required_aftersale_type,
            required_status_text="",
        )
        if resolved_result.ok:
            resolved_rows = (resolved_result.data or {}).get("aftersales") or []
            if isinstance(resolved_rows, list):
                for item in resolved_rows:
                    row = dict(item) if isinstance(item, dict) else {}
                    aftersale_id = str(row.get("aftersale_id") or "").strip()
                    if not aftersale_id or aftersale_id in primary_aftersale_ids:
                        continue
                    if _is_voided_aftersale(row):
                        continue
                    resolved_followup_aftersales.append(row)

    need_intercept: list[dict[str, Any]] = []
    intercept_failed: list[dict[str, Any]] = []
    intercept_returning: list[dict[str, Any]] = []
    problem_orders: list[dict[str, Any]] = []
    logistics_warnings: list[dict[str, Any]] = []
    gift_order_cache: dict[str, dict[str, Any]] = {}
    warning_error = ""
    warning_meta: dict[str, Any] = {
        "enabled": include_logistics_warnings,
        "stale_hours": warning_stale_hours,
        "lookback_days": warning_lookback_days,
        "scan_all": warning_scan_all,
        "scan_limit": warning_scan_limit,
        "result_limit": warning_limit,
        "scanned_order_count": 0,
        "lookup_failed_count": 0,
        "matched_total": 0,
    }

    for item in aftersales:
        row = dict(item) if isinstance(item, dict) else {}
        if str(row.get("aftersale_type") or "").strip() != "已发货仅退款":
            continue
        if _is_excluded_aftersale(row):
            continue
        if _has_receive_goods_time(row):
            continue

        base_record = _build_base_record(row)
        order_id = str(base_record.get("platform_order_id") or base_record.get("order_id") or "").strip()
        if not order_id:
            base_record["status"] = "problem_order"
            base_record["problem_reason"] = "order_id_missing"
            base_record["problem_detail"] = "缺少平台订单号，无法查询物流"
            base_record["action_suggestion"] = "人工核实订单号"
            base_record["case_key"] = _build_case_key(base_record)
            problem_orders.append(base_record)
            continue

        gift_check = _check_gift_order_exclusion(
            backend=backend,
            order_id=order_id,
            shop_name=base_record["shop_name"],
            lookback_days=max(lookback_days, 120),
            cache=gift_order_cache,
        )
        if bool(gift_check.get("excluded")):
            continue
        if not bool(gift_check.get("lookup_ok")):
            base_record["status"] = "problem_order"
            base_record["problem_reason"] = "gift_lookup_failed"
            base_record["problem_detail"] = f"礼物单识别失败: {gift_check.get('lookup_message') or 'unknown'}"
            base_record["action_suggestion"] = "人工核实是否礼物单"
            base_record["case_key"] = _build_case_key(base_record)
            problem_orders.append(base_record)
            continue

        amount_problem = _classify_amount_problem(row)
        if amount_problem is not None:
            base_record["status"] = "problem_order"
            base_record["problem_reason"] = amount_problem["reason"]
            base_record["problem_detail"] = amount_problem["detail"]
            base_record["action_suggestion"] = "人工核实金额字段"
            base_record["case_key"] = _build_case_key(base_record)
            problem_orders.append(base_record)
            continue

        logistics_result = backend.handle(
            tool_name="logistics_lookup",
            payload={
                "args": {
                    "order_id": order_id,
                    "shop_name": base_record["shop_name"],
                    "lookback_days": max(lookback_days, 120),
                    "detail_level": "trace",
                },
                "event": {},
            },
        )
        if not logistics_result.ok:
            base_record["status"] = "problem_order"
            base_record["problem_reason"] = "logistics_lookup_failed"
            base_record["problem_detail"] = f"物流查询失败: {logistics_result.message}"
            base_record["action_suggestion"] = "稍后重试物流查询"
            base_record["case_key"] = _build_case_key(base_record)
            problem_orders.append(base_record)
            continue

        logistics = dict(logistics_result.data or {})
        merged = _merge_logistics(base_record=base_record, logistics=logistics)
        merged["case_key"] = _build_case_key(merged)

        status_info = _classify_logistics(logistics)
        merged["status"] = status_info["status"]
        merged["classification_source"] = status_info["source"]
        if status_info["status"] == "need_intercept":
            merged["action_suggestion"] = "尽快联系快递拦截"
            need_intercept.append(merged)
        elif status_info["status"] == "intercept_failed":
            merged["action_suggestion"] = "包裹已签收，核实售后处理进度"
            intercept_failed.append(merged)
        else:
            merged["action_suggestion"] = "持续跟进回流签收"
            intercept_returning.append(merged)

    for item in resolved_followup_aftersales:
        row = dict(item) if isinstance(item, dict) else {}
        if str(row.get("aftersale_type") or "").strip() != "已发货仅退款":
            continue
        if _is_excluded_aftersale(row):
            continue
        if _has_receive_goods_time(row):
            continue

        base_record = _build_base_record(row)
        order_id = str(base_record.get("platform_order_id") or base_record.get("order_id") or "").strip()
        if not order_id:
            continue

        gift_check = _check_gift_order_exclusion(
            backend=backend,
            order_id=order_id,
            shop_name=base_record["shop_name"],
            lookback_days=max(lookback_days, 120),
            cache=gift_order_cache,
        )
        if bool(gift_check.get("excluded")):
            continue
        if not bool(gift_check.get("lookup_ok")):
            base_record["status"] = "problem_order"
            base_record["problem_reason"] = "gift_lookup_failed"
            base_record["problem_detail"] = f"礼物单识别失败: {gift_check.get('lookup_message') or 'unknown'}"
            base_record["action_suggestion"] = "人工核实是否礼物单"
            base_record["case_key"] = _build_case_key(base_record)
            problem_orders.append(base_record)
            continue

        if _classify_amount_problem(row) is not None:
            continue

        logistics_result = backend.handle(
            tool_name="logistics_lookup",
            payload={
                "args": {
                    "order_id": order_id,
                    "shop_name": base_record["shop_name"],
                    "lookback_days": max(lookback_days, 120),
                    "detail_level": "trace",
                },
                "event": {},
            },
        )
        if not logistics_result.ok:
            continue

        logistics = dict(logistics_result.data or {})
        status_info = _classify_logistics(logistics)
        if status_info["status"] != "intercept_failed":
            continue

        merged = _merge_logistics(base_record=base_record, logistics=logistics)
        merged["case_key"] = _build_case_key(merged)
        merged["status"] = "intercept_failed"
        merged["classification_source"] = status_info["source"]
        merged["action_suggestion"] = "包裹已签收，核实售后处理进度"
        intercept_failed.append(merged)

    if include_logistics_warnings:
        warning_result = _collect_logistics_warnings(
            backend=backend,
            shop_name=shop_name,
            lookback_days=warning_lookback_days,
            scan_all=warning_scan_all,
            scan_limit=warning_scan_limit,
            result_limit=warning_limit,
            stale_hours=warning_stale_hours,
        )
        if bool(warning_result.get("ok")):
            warning_data = dict(warning_result.get("data") or {})
            warning_meta.update(
                {
                    "scanned_order_count": _safe_int(
                        warning_data.get("scanned_order_count"),
                        default=0,
                        lo=0,
                        hi=100000,
                    ),
                    "lookup_failed_count": _safe_int(
                        warning_data.get("lookup_failed_count"),
                        default=0,
                        lo=0,
                        hi=100000,
                    ),
                    "matched_total": _safe_int(
                        warning_data.get("matched_total"),
                        default=0,
                        lo=0,
                        hi=100000,
                    ),
                }
            )
            warning_items = warning_data.get("items")
            logistics_warnings = list(warning_items) if isinstance(warning_items, list) else []
        else:
            warning_error = str(warning_result.get("message") or "logistics_warning_lookup_failed")

    snapshot_time_ms = int(time.time() * 1000)
    return {
        "ok": True,
        "message": "ok",
        "data": {
            "snapshot_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "snapshot_time_ms": snapshot_time_ms,
            "filters": {
                "lookback_days": lookback_days,
                "limit": limit,
                "page_size": page_size,
                "only_open": only_open,
                "shop_name": shop_name,
                "required_aftersale_type": required_aftersale_type,
                "required_status_text": required_status_text,
                "include_resolved_failed_followup": include_resolved_failed_followup,
            },
            "summary": {
                "aftersale_total": len(aftersales) + len(resolved_followup_aftersales),
                "need_intercept_count": len(need_intercept),
                "intercept_failed_count": len(intercept_failed),
                "intercept_returning_count": len(intercept_returning),
                "problem_order_count": len(problem_orders),
                "logistics_warning_count": len(logistics_warnings),
            },
            "need_intercept": need_intercept,
            "intercept_failed": intercept_failed,
            "intercept_returning": intercept_returning,
            "problem_orders": problem_orders,
            "logistics_warnings": logistics_warnings,
            "warning_config": warning_meta,
            "warning_error": warning_error,
        },
    }


def _fetch_aftersale_candidates(
    *,
    backend: ToolBackendService,
    lookback_days: int,
    limit: int,
    page_size: int,
    only_open: bool,
    shop_name: str,
    required_aftersale_type: str,
    required_status_text: str,
):
    if int(limit) <= 0:
        kuaimai = getattr(backend, "kuaimai", None)
        if kuaimai is not None:
            remote = kuaimai.aftersales_list(
                shop_name=shop_name,
                lookback_days=lookback_days,
                limit=0,
                page_size=page_size,
                only_open=only_open,
                required_aftersale_type=required_aftersale_type,
                required_status_text=required_status_text,
            )
            return SimpleNamespace(
                ok=bool(remote.get("ok", False)),
                message=str(remote.get("message") or "kuaimai_aftersale_list_lookup_failed"),
                data=(remote.get("data") if isinstance(remote.get("data"), dict) else {}),
            )
    return backend.handle(
        tool_name="aftersale_list_lookup",
        payload={
            "args": {
                "lookback_days": lookback_days,
                "limit": limit,
                "page_size": page_size,
                "only_open": only_open,
                "shop_name": shop_name,
                "required_aftersale_type": required_aftersale_type,
                "required_status_text": required_status_text,
            },
            "event": {},
        },
    )


def _build_base_record(row: dict[str, Any]) -> dict[str, Any]:
    platform_order_id = str(row.get("platform_order_id") or row.get("order_id") or "").strip()
    return {
        "aftersale_id": str(row.get("aftersale_id") or "").strip(),
        "sid": str(row.get("sid") or "").strip(),
        "order_id": str(row.get("order_id") or "").strip(),
        "platform_order_id": platform_order_id,
        "shop_name": str(row.get("shop_name") or "").strip(),
        "platform": str(row.get("platform") or "").strip(),
        "reason": str(row.get("aftersale_reason") or "").strip(),
        "aftersale_reason": str(row.get("aftersale_reason") or "").strip(),
        "aftersale_type": str(row.get("aftersale_type") or "").strip(),
        "status_text": str(row.get("status_text") or "").strip(),
        "platform_status": str(row.get("platform_status") or "").strip(),
        "paid_amount": _maybe_float(row.get("paid_amount")),
        "platform_refund_amount": _maybe_float(row.get("platform_refund_amount")),
        "receive_goods_time_ms": _safe_int(row.get("receive_goods_time_ms"), default=0, lo=0, hi=10**15),
        "receive_goods_operator": str(row.get("receive_goods_operator") or "").strip(),
        "storage_progress": _safe_int(row.get("storage_progress"), default=0, lo=0, hi=10**9),
        "tracking_no": "",
        "logistics_company": "",
        "logistics_trace_status": "",
        "logistics_trace_status_raw": "",
        "trace_judgement": "",
        "created_at_ms": row.get("created_at_ms"),
        "updated_at_ms": row.get("updated_at_ms"),
    }


def _is_voided_aftersale(row: dict[str, Any]) -> bool:
    return str(row.get("status_text") or "").strip() == "已作废"


def _is_excluded_aftersale(row: dict[str, Any]) -> bool:
    status_text = str(row.get("status_text") or "").strip()
    platform_status = str(row.get("platform_status") or "").strip()
    haystack = "\n".join(item for item in (status_text, platform_status) if item)
    return any(hint in haystack for hint in _AFTERSALE_EXCLUDED_HINTS)


def _has_receive_goods_time(row: dict[str, Any]) -> bool:
    return _safe_int(row.get("receive_goods_time_ms"), default=0, lo=0, hi=10**15) > 0


def _check_gift_order_exclusion(
    *,
    backend: ToolBackendService,
    order_id: str,
    shop_name: str,
    lookback_days: int,
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    wanted_order_id = str(order_id or "").strip()
    if not wanted_order_id:
        return {"lookup_ok": False, "excluded": False, "lookup_message": "order_id_missing"}
    cached = cache.get(wanted_order_id)
    if cached is not None:
        return cached

    kuaimai = getattr(backend, "kuaimai", None)
    if kuaimai is None:
        result = {"lookup_ok": False, "excluded": False, "lookup_message": "kuaimai_client_missing"}
        cache[wanted_order_id] = result
        return result

    remote = kuaimai.orders_by_order_id(
        order_id=wanted_order_id,
        shop_name=str(shop_name or "").strip(),
        lookback_days=lookback_days,
        limit=1,
    )
    if not bool(remote.get("ok")):
        result = {
            "lookup_ok": False,
            "excluded": False,
            "lookup_message": str(remote.get("message") or "order_lookup_failed"),
        }
        cache[wanted_order_id] = result
        return result

    data = dict(remote.get("data") or {})
    orders = data.get("orders")
    is_gift = False
    if isinstance(orders, list):
        for order in orders:
            if not isinstance(order, dict):
                continue
            if _order_matches_gift_outer_id(order):
                is_gift = True
                break

    result = {
        "lookup_ok": True,
        "excluded": is_gift,
        "lookup_message": str(remote.get("message") or "ok"),
    }
    cache[wanted_order_id] = result
    return result


def _order_matches_gift_outer_id(order: dict[str, Any]) -> bool:
    if not _GIFT_OUTER_ID:
        return False
    items = order.get("items")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        outer_id = str(item.get("outer_id") or "").strip()
        system_outer_id = str(item.get("system_outer_id") or "").strip()
        if outer_id.lower() == _GIFT_OUTER_ID or system_outer_id.lower() == _GIFT_OUTER_ID:
            return True
    return False


def _classify_amount_problem(row: dict[str, Any]) -> dict[str, str] | None:
    paid_amount = _maybe_float(row.get("paid_amount"))
    refund_amount = _maybe_float(row.get("platform_refund_amount"))
    if paid_amount is None or refund_amount is None:
        return {
            "reason": "amount_missing",
            "detail": "平台实付或平台实退金额缺失/非数字/小于等于0",
        }
    if abs(paid_amount - refund_amount) > _AMOUNT_EPSILON:
        return {
            "reason": "amount_mismatch",
            "detail": "平台实付金额与平台实退金额不相等",
        }
    return None


def _merge_logistics(*, base_record: dict[str, Any], logistics: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base_record)
    latest_event = logistics.get("trace_latest_event")
    latest_event_obj = dict(latest_event) if isinstance(latest_event, dict) else {}
    merged["tracking_no"] = str(
        logistics.get("trace_tracking_no")
        or logistics.get("tracking_no")
        or ""
    ).strip()
    merged["logistics_company"] = str(
        logistics.get("trace_company")
        or logistics.get("logistics_company")
        or ""
    ).strip()
    merged["logistics_trace_status"] = str(
        logistics.get("trace_status_effective")
        or logistics.get("trace_status")
        or latest_event_obj.get("desc")
        or ""
    ).strip()
    merged["logistics_trace_status_raw"] = str(logistics.get("trace_status_raw") or "").strip()
    merged["trace_judgement"] = str(logistics.get("trace_judgement") or "").strip().lower()
    merged["trace_latest_event"] = latest_event_obj
    merged["trace_found"] = bool(logistics.get("trace_found"))
    merged["trace_node_count"] = _safe_int(logistics.get("trace_node_count"), default=0, lo=0, hi=100000)
    return merged


def _classify_logistics(logistics: dict[str, Any]) -> dict[str, str]:
    if not bool(logistics.get("found", True)):
        return {"status": "intercept_failed", "source": "logistics_not_found"}

    judgement = str(logistics.get("trace_judgement") or "").strip().lower()
    if judgement == "buyer_signed":
        return {"status": "intercept_failed", "source": "trace_judgement"}
    if judgement in {"intercept_returning", "intercept_returned"}:
        return {"status": "intercept_returning", "source": "trace_judgement"}
    if judgement == "in_transit":
        return {"status": "need_intercept", "source": "trace_judgement"}

    status_candidates = [
        str(logistics.get("trace_status_effective") or "").strip(),
        str(logistics.get("trace_status") or "").strip(),
        str(logistics.get("trace_status_raw") or "").strip(),
    ]
    latest_event = logistics.get("trace_latest_event")
    latest_event_obj = dict(latest_event) if isinstance(latest_event, dict) else {}
    latest_desc = str(latest_event_obj.get("desc") or "").strip()
    if latest_desc:
        status_candidates.append(latest_desc)

    tracking_no = str(
        logistics.get("trace_tracking_no")
        or logistics.get("tracking_no")
        or ""
    ).strip()
    trace_found = bool(logistics.get("trace_found"))
    if (not tracking_no) and (not trace_found) and (not any(status_candidates)):
        return {"status": "intercept_failed", "source": "logistics_info_missing"}

    combined = "\n".join(item for item in status_candidates if item)
    if _looks_like_return(combined):
        return {"status": "intercept_returning", "source": "trace_text"}
    if _looks_like_buyer_signed(combined):
        return {"status": "intercept_failed", "source": "trace_text"}
    return {"status": "need_intercept", "source": "fallback_default"}


def _collect_logistics_warnings(
    *,
    backend: ToolBackendService,
    shop_name: str,
    lookback_days: int,
    scan_all: bool,
    scan_limit: int,
    result_limit: int,
    stale_hours: int,
) -> dict[str, Any]:
    kuaimai = getattr(backend, "kuaimai", None)
    if kuaimai is None:
        return {
            "ok": False,
            "message": "kuaimai_client_not_configured",
            "data": {},
        }

    start_ms = int((time.time() - max(1, int(lookback_days)) * 86400) * 1000)
    end_ms = int(time.time() * 1000) + 60 * 1000
    effective_scan_limit = max(1, int(scan_limit))
    page_size = min(100, max(20, effective_scan_limit))
    max_pages = 10**9 if scan_all else min(20, max(1, math.ceil(effective_scan_limit / page_size) + 2))
    scanned_candidates: list[dict[str, Any]] = []
    seen_order_ids: set[str] = set()

    page_no = 1
    while (scan_all or len(scanned_candidates) < effective_scan_limit) and page_no <= max_pages:
        query = kuaimai.trade_search(
            buyer_nick="",
            tid="",
            start_ms=start_ms,
            end_ms=end_ms,
            page_no=page_no,
            page_size=page_size,
        )
        if not bool(query.get("ok")):
            return {
                "ok": False,
                "message": str(query.get("message") or "trade_search_failed"),
                "data": {},
            }

        rows = (query.get("data") or {}).get("rows") or []
        if not isinstance(rows, list) or not rows:
            break

        for raw in rows:
            if not isinstance(raw, dict):
                continue
            order_id = str(raw.get("tid") or "").strip()
            if not order_id or order_id in seen_order_ids:
                continue
            seen_order_ids.add(order_id)

            normalized = dict(kuaimai._normalize_order(raw, wanted_tid=order_id))
            if shop_name and str(normalized.get("shop_name") or "").strip() != shop_name:
                continue
            if not _is_warning_order_candidate(raw=raw, normalized=normalized):
                continue

            scanned_candidates.append(
                {
                    "order": normalized,
                    "consign_time_ms": _safe_int(raw.get("consignTime"), default=0, lo=0, hi=10**15),
                }
            )
            if not scan_all and len(scanned_candidates) >= effective_scan_limit:
                break

        if len(rows) < page_size:
            break
        page_no += 1

    now_dt = datetime.now().astimezone()
    warnings: list[dict[str, Any]] = []
    lookup_failed_count = 0

    for candidate in scanned_candidates:
        order = dict(candidate.get("order") or {})
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            continue

        logistics_result = backend.handle(
            tool_name="logistics_lookup",
            payload={
                "args": {
                    "order_id": order_id,
                    "shop_name": str(order.get("shop_name") or "").strip(),
                    "lookback_days": max(lookback_days, 120),
                    "detail_level": "trace",
                },
                "event": {},
            },
        )
        if not logistics_result.ok:
            lookup_failed_count += 1
            continue

        logistics = dict(logistics_result.data or {})
        if bool(logistics.get("found", True)):
            status_info = _classify_logistics(logistics)
            if status_info["status"] in {"intercept_failed", "intercept_returning"}:
                continue
        else:
            logistics = {}

        warning_record = _build_logistics_warning_record(
            order=order,
            logistics=logistics,
            consign_time_ms=_safe_int(candidate.get("consign_time_ms"), default=0, lo=0, hi=10**15),
            now_dt=now_dt,
            stale_hours=stale_hours,
        )
        if warning_record is None:
            continue
        warnings.append(warning_record)

    warnings.sort(
        key=lambda item: (
            -float(item.get("warning_hours") or 0.0),
            str(item.get("latest_trace_time") or ""),
            str(item.get("platform_order_id") or ""),
        )
    )
    return {
        "ok": True,
        "message": "ok",
        "data": {
            "items": warnings[:result_limit],
            "scanned_order_count": len(scanned_candidates),
            "lookup_failed_count": lookup_failed_count,
            "matched_total": len(warnings),
        },
    }


def _is_warning_order_candidate(*, raw: dict[str, Any], normalized: dict[str, Any]) -> bool:
    status_code = str(normalized.get("status") or "").strip()
    status_text = str(normalized.get("status_text") or "").strip()
    status_haystack = "\n".join(item for item in (status_code, status_text) if item)
    if _looks_like_closed_order_status(status_haystack):
        return False
    if _looks_like_order_signed_status(status_haystack):
        return False
    if _looks_like_unshipped_order_status(status_haystack):
        return False

    consign_time_ms = _safe_int(raw.get("consignTime"), default=0, lo=0, hi=10**15)
    if not _is_effective_order_time_ms(consign_time_ms):
        consign_time_ms = 0
    tracking_no = str(
        normalized.get("tracking_no")
        or raw.get("logisticsCode")
        or raw.get("outSid")
        or ""
    ).strip()
    if consign_time_ms > 0:
        return True
    if tracking_no:
        return True
    return any(item in status_haystack for item in _ORDER_SHIPPED_HINTS)


def _build_logistics_warning_record(
    *,
    order: dict[str, Any],
    logistics: dict[str, Any],
    consign_time_ms: int,
    now_dt: datetime,
    stale_hours: int,
) -> dict[str, Any] | None:
    latest_event = logistics.get("trace_latest_event")
    latest_event_obj = dict(latest_event) if isinstance(latest_event, dict) else {}
    latest_trace_time = str(latest_event_obj.get("time") or "").strip()
    latest_trace_dt = _parse_trace_time(latest_trace_time, fallback_tz=now_dt.tzinfo)
    latest_trace_source = "trace_latest_event"

    if latest_trace_dt is None and _is_effective_order_time_ms(consign_time_ms):
        latest_trace_dt = datetime.fromtimestamp(consign_time_ms / 1000, tz=now_dt.tzinfo)
        latest_trace_time = latest_trace_dt.strftime("%Y-%m-%d %H:%M:%S")
        latest_trace_source = "consign_time"

    if latest_trace_dt is None:
        return None

    warning_hours = round(max(0.0, (now_dt - latest_trace_dt).total_seconds()) / 3600, 1)
    if warning_hours < float(stale_hours):
        return None

    platform_order_id = str(order.get("order_id") or "").strip()
    tracking_no = str(
        logistics.get("trace_tracking_no")
        or logistics.get("tracking_no")
        or order.get("tracking_no")
        or ""
    ).strip()
    note_key = f"warning:{platform_order_id}|{tracking_no or str(order.get('sid') or '').strip() or platform_order_id}"
    item_summary = _summarize_item_names(order.get("item_names"), order.get("spec"))
    logistics_status = str(
        logistics.get("trace_status_effective")
        or logistics.get("trace_status")
        or latest_event_obj.get("desc")
        or ""
    ).strip()
    latest_trace_desc = str(latest_event_obj.get("desc") or "").strip()
    if latest_trace_source == "consign_time" and not latest_trace_desc:
        latest_trace_desc = "暂无有效物流轨迹，按发货时间计算"
    if latest_trace_source == "consign_time" and not logistics_status:
        logistics_status = latest_trace_desc

    record = {
        "aftersale_id": note_key,
        "sid": str(order.get("sid") or "").strip(),
        "order_id": platform_order_id,
        "platform_order_id": platform_order_id,
        "shop_name": str(order.get("shop_name") or "").strip(),
        "platform": str(order.get("platform") or "").strip(),
        "reason": item_summary,
        "aftersale_reason": "",
        "aftersale_type": "",
        "status_text": str(order.get("status") or "").strip(),
        "platform_status": str(order.get("status") or "").strip(),
        "paid_amount": None,
        "platform_refund_amount": None,
        "tracking_no": tracking_no,
        "logistics_company": str(
            logistics.get("trace_company")
            or logistics.get("logistics_company")
            or order.get("logistics_company")
            or ""
        ).strip(),
        "logistics_trace_status": logistics_status,
        "logistics_trace_status_raw": str(logistics.get("trace_status_raw") or "").strip(),
        "trace_judgement": str(logistics.get("trace_judgement") or "").strip().lower(),
        "trace_latest_event": latest_event_obj,
        "trace_found": bool(logistics.get("trace_found")),
        "trace_node_count": _safe_int(logistics.get("trace_node_count"), default=0, lo=0, hi=100000),
        "created_at_ms": None,
        "updated_at_ms": None,
        "status": "logistics_warning",
        "classification_source": "stale_trace_rule",
        "action_suggestion": "优先核实物流停滞原因，必要时主动催件或联系快递",
        "case_key": f"{platform_order_id}|{tracking_no or str(order.get('sid') or '').strip() or platform_order_id}",
        "record_type": "logistics_warning",
        "display_id_label": "平台单",
        "display_id_value": platform_order_id,
        "item_summary": item_summary,
        "order_amount": _maybe_float(order.get("amount")),
        "latest_trace_time": latest_trace_time,
        "latest_trace_desc": latest_trace_desc,
        "latest_trace_source": latest_trace_source,
        "warning_hours": warning_hours,
        "warning_threshold_hours": stale_hours,
    }
    return record


def _looks_like_return(text: str) -> bool:
    haystack = str(text or "").strip()
    if not haystack:
        return False
    return any(item in haystack for item in _RETURN_HINTS)


def _looks_like_buyer_signed(text: str) -> bool:
    haystack = str(text or "").strip()
    if not haystack:
        return False
    if any(item in haystack for item in _RETURN_SIGNED_HINTS):
        return False
    return any(item in haystack for item in _BUYER_SIGN_HINTS)


def _looks_like_order_signed_status(text: str) -> bool:
    haystack = str(text or "").strip()
    if not haystack:
        return False
    return any(item in haystack for item in _ORDER_SIGNED_HINTS)


def _looks_like_closed_order_status(text: str) -> bool:
    haystack = str(text or "").strip()
    if not haystack:
        return False
    return any(item in haystack for item in _ORDER_CLOSED_HINTS)


def _looks_like_unshipped_order_status(text: str) -> bool:
    haystack = str(text or "").strip()
    if not haystack:
        return False
    return any(item in haystack for item in _ORDER_UNSHIPPED_HINTS)


def _build_case_key(record: dict[str, Any]) -> str:
    platform_order_id = str(record.get("platform_order_id") or record.get("order_id") or "").strip()
    tracking_no = str(record.get("tracking_no") or "").strip()
    aftersale_id = str(record.get("aftersale_id") or "").strip()
    if platform_order_id and tracking_no:
        return f"{platform_order_id}|{tracking_no}"
    if platform_order_id and aftersale_id:
        return f"{platform_order_id}|{aftersale_id}"
    return aftersale_id or platform_order_id or ""


def _parse_trace_time(raw: Any, *, fallback_tz: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=fallback_tz)
        except Exception:
            continue
    return None


def _is_effective_order_time_ms(value: int) -> bool:
    return int(value or 0) >= 1262304000000


def _summarize_item_names(raw_names: Any, raw_spec: Any) -> str:
    names = raw_names if isinstance(raw_names, list) else []
    cleaned = [str(item or "").strip() for item in names if str(item or "").strip()]
    if cleaned:
        summary = " / ".join(cleaned[:2])
        if len(cleaned) > 2:
            summary = f"{summary} 等{len(cleaned)}件"
        return summary
    return str(raw_spec or "").strip()


def _safe_int(raw: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        value = int(float(raw))
    except Exception:
        value = default
    return max(lo, min(value, hi))


def _as_bool(raw: Any, *, default: bool) -> bool:
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def _maybe_float(raw: Any) -> float | None:
    try:
        value = float(raw)
    except Exception:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value
