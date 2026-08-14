from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except Exception:
        return default


def _to_int(raw: Any, default: int = 0) -> int:
    try:
        return int(float(raw))
    except Exception:
        return default


BUYER_SIGN_KEYWORDS: tuple[str, ...] = (
    "已签收",
    "签收",
    "妥投",
    "投递成功",
    "已收件",
    "本人收",
    "本人签收",
    "代收",
    "快递柜",
    "驿站",
    "门卫",
    "前台",
)

RETURN_TO_SENDER_KEYWORDS: tuple[str, ...] = (
    "拦截",
    "退回",
    "退件",
    "返件",
    "回寄",
    "回流",
    "原路退回",
    "退回发货地",
    "退回寄件",
    "发件人签收",
    "寄件人签收",
    "商家签收",
    "原寄地",
)

IN_TRANSIT_KEYWORDS: tuple[str, ...] = (
    "揽收",
    "发出",
    "发往",
    "到达",
    "分拣",
    "派送",
    "运输",
    "在途",
    "待取",
    "集散",
    "处理中心",
)


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    val = str(text or "").strip()
    if not val:
        return False
    return any(k in val for k in keywords)


def _normalize_trace_event_time(raw: Any) -> str:
    value = str(raw or "").strip()
    # Expected upstream format: YYYY-MM-DD HH:MM:SS. Keep lexical order stable.
    return value


def _is_return_to_sender_text(text: str) -> bool:
    return _contains_any(text, RETURN_TO_SENDER_KEYWORDS)


def _looks_like_buyer_signed_text(text: str) -> bool:
    if not _contains_any(text, BUYER_SIGN_KEYWORDS):
        return False
    if _is_return_to_sender_text(text):
        # "退回后商家签收" is return-to-sender, not buyer signed.
        return False
    return True


class KuaimaiClient:
    def __init__(
        self,
        *,
        erp_host: str,
        company_name: str,
        account: str,
        password: str,
        company_id: str,
        timeout_sec: float,
        snapshot_ttl_sec: float,
        storage_state_path: str,
        login_headless: bool,
        chromium_path: str,
        browser_channel: str,
    ) -> None:
        self.erp_host = (erp_host or "https://erp.superboss.cc").strip().rstrip("/")
        self.company_name = (company_name or "").strip()
        self.account = (account or "").strip()
        self.password = str(password or "").strip()
        self.company_id = (company_id or "").strip()
        self.timeout_sec = max(3.0, float(timeout_sec))
        self.snapshot_ttl_sec = max(5.0, float(snapshot_ttl_sec))
        self.storage_state_path = (storage_state_path or "").strip()
        self.login_headless = bool(login_headless)
        self.chromium_path = (chromium_path or "").strip()
        self.browser_channel = (browser_channel or "").strip()

        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_at = 0.0

    def orders_by_identity(
        self,
        *,
        km_name: str,
        shop_name: str,
        lookback_days: int,
        limit: int,
    ) -> dict[str, Any]:
        nickname = str(km_name or "").strip()
        if not nickname:
            return {"ok": False, "message": "missing_km_name", "data": {}}

        start_ms = _now_ms() - max(1, int(lookback_days)) * 86400 * 1000
        end_ms = _now_ms() + 60 * 1000
        page_size = min(max(int(limit), 20), 100)

        rows: list[dict[str, Any]] = []
        page_no = 1
        while len(rows) < int(limit) and page_no <= 8:
            query = self.trade_search(
                buyer_nick=nickname,
                tid="",
                start_ms=start_ms,
                end_ms=end_ms,
                page_no=page_no,
                page_size=page_size,
            )
            if not query.get("ok"):
                return query
            chunk = (query.get("data") or {}).get("rows") or []
            if not isinstance(chunk, list) or not chunk:
                break
            rows.extend(chunk)
            if len(chunk) < page_size:
                break
            page_no += 1

        if shop_name:
            wanted = str(shop_name).strip()
            rows = [r for r in rows if str(r.get("shopName") or "").strip() == wanted]

        # Expand to tid-level orders. A single sid row can contain multiple tids.
        max_orders = int(limit)
        detail_cache: dict[str, list[dict[str, Any]]] = {}
        seen_order_ids: set[str] = set()
        orders: list[dict[str, Any]] = []

        for row in rows:
            if len(orders) >= max_orders:
                break

            sid = str(row.get("sid") or "").strip()
            user_id = str(row.get("userId") or "").strip()

            detail_rows: list[dict[str, Any]] = []
            if sid:
                cached = detail_cache.get(sid)
                if cached is None:
                    created_ms = _to_int(row.get("created"), 0) or _to_int(row.get("payTime"), 0)
                    detail_start_ms = max(0, created_ms - 2 * 86400 * 1000) if created_ms > 0 else start_ms
                    detail_end_ms = created_ms + 2 * 86400 * 1000 if created_ms > 0 else end_ms
                    detail_resp = self.trade_search_sids(
                        sid=sid,
                        user_id=user_id,
                        start_ms=detail_start_ms,
                        end_ms=detail_end_ms,
                        page_no=1,
                        page_size=50,
                    )
                    detail_data = detail_resp.get("data")
                    raw_rows = (
                        detail_data.get("rows")
                        if isinstance(detail_data, dict)
                        else []
                    )
                    detail_rows = [item for item in raw_rows if isinstance(item, dict)] if isinstance(raw_rows, list) else []
                    detail_cache[sid] = detail_rows
                else:
                    detail_rows = cached

            base_tid = str(row.get("tid") or "").strip()
            tids = self._collect_tids_from_detail_rows(detail_rows=detail_rows)
            if base_tid and base_tid not in tids:
                tids.insert(0, base_tid)
            if not tids:
                tids = [base_tid] if base_tid else [""]

            for tid in tids:
                if len(orders) >= max_orders:
                    break
                wanted_tid = str(tid or "").strip()
                detail_row = self._pick_trade_detail_by_tid(detail_rows=detail_rows, tid=wanted_tid)
                normalized = self._normalize_order(
                    row,
                    detail_row=detail_row,
                    wanted_tid=wanted_tid,
                )
                order_id = str(normalized.get("order_id") or "").strip()
                dedup_key = order_id or f"sid:{sid}"
                if dedup_key in seen_order_ids:
                    continue
                seen_order_ids.add(dedup_key)
                orders.append(normalized)

        # Keep newest first.
        orders.sort(key=lambda item: _to_int(item.get("paid_at_ms"), 0), reverse=True)
        return {
            "ok": True,
            "message": "ok",
            "data": {
                "found": bool(orders),
                "km_name": nickname,
                "shop_name": str(shop_name or "").strip(),
                "total": len(orders),
                "orders": orders,
            },
        }

    def orders_by_order_id(
        self,
        *,
        order_id: str,
        shop_name: str,
        lookback_days: int,
        limit: int = 20,
    ) -> dict[str, Any]:
        order_key = str(order_id or "").strip()
        if not order_key:
            return {"ok": False, "message": "missing_order_id", "data": {}}

        start_ms = _now_ms() - max(1, int(lookback_days)) * 86400 * 1000
        end_ms = _now_ms() + 60 * 1000
        page_size = min(max(int(limit), 1), 50)

        # Only use platform order id (tid).
        query = self.trade_search(
            buyer_nick="",
            tid=order_key,
            start_ms=start_ms,
            end_ms=end_ms,
            page_no=1,
            page_size=page_size,
        )
        if not query.get("ok"):
            return query
        rows = (query.get("data") or {}).get("rows") or []
        if not isinstance(rows, list):
            rows = []

        # Deduplicate by tid.
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            tid = str(row.get("tid") or "").strip()
            key = tid
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)

        if shop_name:
            wanted = str(shop_name).strip()
            unique = [r for r in unique if str(r.get("shopName") or "").strip() == wanted]

        detail_cache: dict[str, dict[str, Any]] = {}
        normalized_orders: list[dict[str, Any]] = []
        for row in unique[:page_size]:
            sid = str(row.get("sid") or "").strip()
            detail_row: dict[str, Any] | None = None
            if sid:
                detail_cached = detail_cache.get(sid)
                if detail_cached is None:
                    created_ms = _to_int(row.get("created"), 0) or _to_int(row.get("payTime"), 0)
                    detail_start_ms = max(0, created_ms - 2 * 86400 * 1000) if created_ms > 0 else start_ms
                    detail_end_ms = (
                        created_ms + 2 * 86400 * 1000 if created_ms > 0 else end_ms
                    )
                    detail_resp = self.trade_search_sids(
                        sid=sid,
                        user_id=str(row.get("userId") or "").strip(),
                        start_ms=detail_start_ms,
                        end_ms=detail_end_ms,
                        page_no=1,
                        page_size=1,
                    )
                    detail_rows = (
                        (detail_resp.get("data") or {}).get("rows")
                        if isinstance(detail_resp.get("data"), dict)
                        else []
                    )
                    if not isinstance(detail_rows, list):
                        detail_rows = []
                    detail_cached = (
                        self._pick_trade_detail_by_tid(detail_rows=detail_rows, tid=str(row.get("tid") or ""))
                        or {}
                    )
                    detail_cache[sid] = detail_cached
                if detail_cached:
                    detail_row = detail_cached
            normalized_orders.append(
                self._normalize_order(
                    row,
                    detail_row=detail_row,
                    wanted_tid=order_key,
                )
            )
        return {
            "ok": True,
            "message": "ok" if normalized_orders else "not_found",
            "data": {
                "found": bool(normalized_orders),
                "order_id": order_key,
                "shop_name": str(shop_name or "").strip(),
                "total": len(normalized_orders),
                "orders": normalized_orders,
            },
        }

    def aftersales_by_order_id(
        self,
        *,
        order_id: str,
        shop_name: str,
        lookback_days: int,
        limit: int = 20,
        only_open: bool = False,
    ) -> dict[str, Any]:
        raw_lookup = self._collect_aftersale_rows_by_order_id(
            order_id=order_id,
            shop_name=shop_name,
            lookback_days=lookback_days,
            limit=limit,
        )
        if not bool(raw_lookup.get("ok", False)):
            return raw_lookup
        payload_data = raw_lookup.get("data")
        if not isinstance(payload_data, dict):
            payload_data = {}
        rows = payload_data.get("rows")
        if not isinstance(rows, list):
            rows = []

        matched: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            normalized = self._normalize_aftersale(raw)
            if only_open and (not bool(normalized.get("is_open"))):
                continue
            matched.append(normalized)

        matched.sort(key=lambda item: _to_int(item.get("updated_at_ms"), 0), reverse=True)
        return {
            "ok": True,
            "message": "ok" if matched else "not_found",
            "data": {
                "found": bool(matched),
                "order_id": str(payload_data.get("order_id") or "").strip(),
                "shop_name": str(payload_data.get("shop_name") or "").strip(),
                "total": len(matched),
                "aftersales": matched,
            },
        }

    def aftersales_list(
        self,
        *,
        shop_name: str,
        lookback_days: int,
        limit: int = 200,
        page_size: int = 100,
        only_open: bool = True,
        required_aftersale_type: str = "",
        required_status_text: str = "",
    ) -> dict[str, Any]:
        wanted_shop = str(shop_name or "").strip()
        wanted_type = str(required_aftersale_type or "").strip()
        wanted_status = str(required_status_text or "").strip()
        raw_limit = int(limit)
        scan_all = raw_limit <= 0
        total_limit = 10**9 if scan_all else min(max(raw_limit, 1), 500)
        search_page_size = min(max(int(page_size), 1), 200)
        start_ms = _now_ms() - max(1, int(lookback_days)) * 86400 * 1000
        end_ms = _now_ms() + 60 * 1000

        matched: list[dict[str, Any]] = []
        page_no = 1
        max_pages = 200 if scan_all else min(20, max(1, (total_limit + search_page_size - 1) // search_page_size + 2))

        while len(matched) < total_limit and page_no <= max_pages:
            query = self.aftersale_search(
                tid="",
                start_ms=start_ms,
                end_ms=end_ms,
                page_no=page_no,
                page_size=search_page_size,
            )
            if not bool(query.get("ok", False)):
                return query

            chunk = (query.get("data") or {}).get("rows") or []
            if not isinstance(chunk, list) or not chunk:
                break

            for raw in chunk:
                if not isinstance(raw, dict):
                    continue
                normalized = self._normalize_aftersale(raw)
                if only_open and (not bool(normalized.get("is_open"))):
                    continue
                if wanted_shop and str(normalized.get("shop_name") or "").strip() != wanted_shop:
                    continue
                if wanted_type and str(normalized.get("aftersale_type") or "").strip() != wanted_type:
                    continue
                if wanted_status and str(normalized.get("status_text") or "").strip() != wanted_status:
                    continue
                matched.append(
                    {
                        "aftersale_id": str(raw.get("id") or "").strip(),
                        "sid": str(raw.get("sid") or "").strip(),
                        "order_id": str(normalized.get("order_id") or "").strip(),
                        "platform_order_id": str(normalized.get("order_id") or "").strip(),
                        "shop_name": str(normalized.get("shop_name") or "").strip(),
                        "platform": str(normalized.get("platform") or "").strip(),
                        "aftersale_reason": str(normalized.get("aftersale_reason") or "").strip(),
                        "aftersale_type": str(normalized.get("aftersale_type") or "").strip(),
                        "status_text": str(normalized.get("status_text") or "").strip(),
                        "status_code": str(normalized.get("status_code") or "").strip(),
                        "platform_status": str(normalized.get("platform_status") or "").strip(),
                        "platform_status_code": str(normalized.get("platform_status_code") or "").strip(),
                        "is_open": bool(normalized.get("is_open")),
                        "open_reason": str(normalized.get("open_reason") or "").strip(),
                        "paid_amount": round(_to_float(normalized.get("paid_amount"), 0.0), 2),
                        "platform_refund_amount": round(
                            _to_float(normalized.get("platform_refund_amount"), 0.0), 2
                        ),
                        "return_express_company": str(normalized.get("return_express_company") or "").strip(),
                        "return_tracking_no": str(normalized.get("return_tracking_no") or "").strip(),
                        "receive_goods_time_ms": _to_int(normalized.get("receive_goods_time_ms"), 0),
                        "receive_goods_operator": str(normalized.get("receive_goods_operator") or "").strip(),
                        "storage_progress": _to_int(normalized.get("storage_progress"), 0),
                        "updated_at_ms": _to_int(normalized.get("updated_at_ms"), 0),
                        "created_at_ms": _to_int(normalized.get("created_at_ms"), 0),
                    }
                )
                if len(matched) >= total_limit:
                    break

            if len(chunk) < search_page_size:
                break
            page_no += 1

        matched.sort(key=lambda item: _to_int(item.get("updated_at_ms"), 0), reverse=True)
        return {
            "ok": True,
            "message": "ok" if matched else "not_found",
            "data": {
                "found": bool(matched),
                "shop_name": wanted_shop,
                "lookback_days": max(1, int(lookback_days)),
                "total": len(matched),
                "filters": {
                    "scan_all": bool(scan_all),
                    "only_open": bool(only_open),
                    "required_aftersale_type": wanted_type,
                    "required_status_text": wanted_status,
                },
                "aftersales": matched,
            },
        }

    def approve_refund_by_order_id(
        self,
        *,
        order_id: str,
        shop_name: str,
        lookback_days: int,
        max_refund_amount_yuan: float = 5.0,
        required_aftersale_type: str = "已发货仅退款",
    ) -> dict[str, Any]:
        order_key = str(order_id or "").strip()
        if not order_key:
            return {"ok": False, "message": "missing_order_id", "data": {}}

        raw_lookup = self._collect_aftersale_rows_by_order_id(
            order_id=order_key,
            shop_name=shop_name,
            lookback_days=lookback_days,
            limit=20,
        )
        if not bool(raw_lookup.get("ok", False)):
            return raw_lookup
        payload_data = raw_lookup.get("data")
        if not isinstance(payload_data, dict):
            payload_data = {}
        rows = payload_data.get("rows")
        if not isinstance(rows, list) or not rows:
            return {
                "ok": True,
                "message": "not_found",
                "data": {
                    "found": False,
                    "order_id": order_key,
                    "shop_name": str(payload_data.get("shop_name") or "").strip(),
                    "matched_total": 0,
                },
            }

        required_type = str(required_aftersale_type or "已发货仅退款").strip()
        cap = max(0.0, float(max_refund_amount_yuan))

        candidates: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            normalized = self._normalize_aftersale(raw)
            work_order_id = str(raw.get("id") or "").strip()
            aftersale_type = str(normalized.get("aftersale_type") or "").strip()
            refund_amount = _to_float(normalized.get("platform_refund_amount"), 0.0)
            paid_amount = _to_float(normalized.get("paid_amount"), 0.0)
            is_open = bool(normalized.get("is_open"))
            candidates.append(
                {
                    "work_order_id": work_order_id,
                    "order_id": str(normalized.get("order_id") or order_key).strip(),
                    "shop_name": str(normalized.get("shop_name") or payload_data.get("shop_name") or "").strip(),
                    "aftersale_type": aftersale_type,
                    "paid_amount": round(paid_amount, 2),
                    "platform_refund_amount": round(refund_amount, 2),
                    "is_open": is_open,
                    "status_text": str(normalized.get("status_text") or "").strip(),
                    "platform_status": str(normalized.get("platform_status") or "").strip(),
                    "updated_at_ms": _to_int(normalized.get("updated_at_ms"), 0),
                }
            )

        eligible = [
            item
            for item in candidates
            if item.get("work_order_id")
            and bool(item.get("is_open"))
            and str(item.get("aftersale_type") or "").strip() == required_type
            and _to_float(item.get("platform_refund_amount"), 0.0) <= cap
            and _to_float(item.get("platform_refund_amount"), 0.0)
            <= _to_float(item.get("paid_amount"), 0.0)
        ]
        eligible.sort(key=lambda item: _to_int(item.get("updated_at_ms"), 0), reverse=True)

        if not eligible:
            return {
                "ok": False,
                "message": "approve_refund_guard_failed",
                "data": {
                    "order_id": order_key,
                    "required_aftersale_type": required_type,
                    "max_refund_amount_yuan": round(cap, 2),
                    "candidates": candidates[:10],
                    "eligible_total": 0,
                },
            }

        if len(eligible) > 1:
            return {
                "ok": False,
                "message": "approve_refund_multiple_candidates",
                "data": {
                    "order_id": order_key,
                    "required_aftersale_type": required_type,
                    "max_refund_amount_yuan": round(cap, 2),
                    "eligible_total": len(eligible),
                    "eligible_candidates": eligible[:10],
                },
            }

        target = eligible[0]
        work_order_id = str(target.get("work_order_id") or "").strip()
        if not work_order_id:
            return {
                "ok": False,
                "message": "approve_refund_missing_work_order_id",
                "data": {"order_id": order_key},
            }

        exec_result = self._agree_return_refund(work_order_ids=[work_order_id])
        if not bool(exec_result.get("ok", False)):
            data = exec_result.get("data")
            if not isinstance(data, dict):
                data = {}
            data["order_id"] = order_key
            data["work_order_id"] = work_order_id
            return {
                "ok": False,
                "message": str(exec_result.get("message") or "approve_refund_failed"),
                "data": data,
            }

        remote_data = exec_result.get("data")
        if not isinstance(remote_data, dict):
            remote_data = {}
        return {
            "ok": True,
            "message": "ok",
            "data": {
                "approved": True,
                "order_id": order_key,
                "work_order_id": work_order_id,
                "shop_name": str(target.get("shop_name") or "").strip(),
                "aftersale_type": str(target.get("aftersale_type") or "").strip(),
                "paid_amount": round(_to_float(target.get("paid_amount"), 0.0), 2),
                "platform_refund_amount": round(_to_float(target.get("platform_refund_amount"), 0.0), 2),
                "status_text": str(target.get("status_text") or "").strip(),
                "platform_status": str(target.get("platform_status") or "").strip(),
                "max_refund_amount_yuan": round(cap, 2),
                "required_aftersale_type": required_type,
                "raw_result": remote_data.get("raw") if isinstance(remote_data.get("raw"), dict) else {},
            },
        }

    def _collect_aftersale_rows_by_order_id(
        self,
        *,
        order_id: str,
        shop_name: str,
        lookback_days: int,
        limit: int,
    ) -> dict[str, Any]:
        order_key = str(order_id or "").strip()
        if not order_key:
            return {"ok": False, "message": "missing_order_id", "data": {}}

        start_ms = _now_ms() - max(1, int(lookback_days)) * 86400 * 1000
        end_ms = _now_ms() + 60 * 1000
        page_size = min(max(int(limit), 1), 50)

        rows: list[dict[str, Any]] = []
        page_no = 1
        while len(rows) < int(limit) and page_no <= 6:
            query = self.aftersale_search(
                tid=order_key,
                start_ms=start_ms,
                end_ms=end_ms,
                page_no=page_no,
                page_size=page_size,
            )
            if not query.get("ok"):
                return query
            chunk = (query.get("data") or {}).get("rows") or []
            if not isinstance(chunk, list) or not chunk:
                break
            rows.extend(chunk)
            if len(chunk) < page_size:
                break
            page_no += 1

        matched_rows: list[dict[str, Any]] = []
        wanted_shop = str(shop_name or "").strip()
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            tid = str(raw.get("tid") or "").strip()
            if tid != order_key:
                continue
            if wanted_shop:
                row_shop = str(raw.get("shopName") or "").strip()
                if row_shop != wanted_shop:
                    continue
            matched_rows.append(raw)

        matched_rows.sort(key=lambda item: _to_int(item.get("modified"), 0), reverse=True)
        return {
            "ok": True,
            "message": "ok" if matched_rows else "not_found",
            "data": {
                "found": bool(matched_rows),
                "order_id": order_key,
                "shop_name": wanted_shop,
                "total": len(matched_rows),
                "rows": matched_rows,
            },
        }

    def _agree_return_refund(
        self,
        *,
        work_order_ids: list[str],
    ) -> dict[str, Any]:
        first = self._agree_return_refund_once(
            work_order_ids=work_order_ids,
            force_refresh=False,
        )
        if first.get("ok"):
            return first
        return self._agree_return_refund_once(
            work_order_ids=work_order_ids,
            force_refresh=True,
        )

    def _agree_return_refund_once(
        self,
        *,
        work_order_ids: list[str],
        force_refresh: bool,
    ) -> dict[str, Any]:
        ids = [str(v or "").strip() for v in work_order_ids if str(v or "").strip()]
        if not ids:
            return {"ok": False, "message": "missing_work_order_ids", "data": {}}

        try:
            snapshot = self._get_snapshot(force_refresh=force_refresh)
        except Exception as exc:
            return {
                "ok": False,
                "message": f"kuaimai_login_failed:{type(exc).__name__}",
                "data": {"error": str(exc)[:300]},
            }

        erp_host = str(snapshot.get("erp_host") or self.erp_host).rstrip("/")
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/x-www-form-urlencoded",
            "cookie": str(snapshot.get("cookie") or ""),
            "origin": erp_host,
            "referer": erp_host + "/index.html",
            "user-agent": str(snapshot.get("user_agent") or ""),
            "x-requested-with": "XMLHttpRequest",
            "Module-Path": "/aftersale/sale_handle_next/",
        }
        if self.company_id:
            headers["companyid"] = self.company_id

        data = {
            "ids": ",".join(ids),
            "api_name": "as_order_platform_agree_batchAgreeReturnRefund",
        }
        req = urllib.request.Request(
            url=erp_host + "/as/order/platform/agree/batchAgreeReturnRefund",
            data=urllib.parse.urlencode(data).encode("utf-8"),
            method="POST",
        )
        for key, value in headers.items():
            req.add_header(key, value)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                return {"ok": False, "message": "agree_return_refund_timeout", "data": {}}
            return {
                "ok": False,
                "message": f"agree_return_refund_request_failed:{type(exc).__name__}",
                "data": {},
            }
        except TimeoutError:
            return {"ok": False, "message": "agree_return_refund_timeout", "data": {}}

        try:
            payload = json.loads(raw or "{}")
        except Exception:
            return {
                "ok": False,
                "message": "agree_return_refund_json_decode_failed",
                "data": {"body_preview": (raw or "")[:300]},
            }

        if int(payload.get("result") or 0) != 1:
            return {
                "ok": False,
                "message": str(payload.get("message") or "agree_return_refund_result_not_ok"),
                "data": {"raw": payload},
            }
        if payload.get("suc") is False:
            return {
                "ok": False,
                "message": str(payload.get("message") or "agree_return_refund_failed"),
                "data": {"raw": payload},
            }

        return {
            "ok": True,
            "message": "ok",
            "data": {"raw": payload},
        }

    def open_aftersales_by_order_ids(
        self,
        *,
        order_ids: list[str],
        shop_name: str,
        lookback_days: int,
        per_order_limit: int = 10,
        total_limit: int = 200,
    ) -> dict[str, Any]:
        wanted_shop = str(shop_name or "").strip()
        unique_order_ids: list[str] = []
        seen: set[str] = set()
        for raw in order_ids:
            order_id = str(raw or "").strip()
            if not order_id or order_id in seen:
                continue
            seen.add(order_id)
            unique_order_ids.append(order_id)

        open_items: list[dict[str, Any]] = []
        failed_orders: list[str] = []
        total_cap = max(1, int(total_limit))
        for order_id in unique_order_ids:
            if len(open_items) >= total_cap:
                break
            lookup = self.aftersales_by_order_id(
                order_id=order_id,
                shop_name=wanted_shop,
                lookback_days=lookback_days,
                limit=max(1, int(per_order_limit)),
                only_open=True,
            )
            if not bool(lookup.get("ok", False)):
                failed_orders.append(order_id)
                continue
            data = lookup.get("data")
            items = data.get("aftersales") if isinstance(data, dict) else []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                open_items.append(item)
                if len(open_items) >= total_cap:
                    break

        open_items.sort(key=lambda item: _to_int(item.get("updated_at_ms"), 0), reverse=True)
        return {
            "ok": True,
            "message": "ok",
            "data": {
                "found": bool(open_items),
                "shop_name": wanted_shop,
                "total": len(open_items),
                "open_aftersales": open_items[:total_cap],
                "failed_order_ids": failed_orders,
                "checked_order_ids": unique_order_ids,
            },
        }

    def logistics_by_order(
        self,
        *,
        order_id: str,
        shop_name: str,
        lookback_days: int,
        detail_level: str = "summary",
    ) -> dict[str, Any]:
        order_key = str(order_id or "").strip()
        if not order_key:
            return {"ok": False, "message": "missing_order_id", "data": {}}
        level = str(detail_level or "summary").strip().lower()
        if level not in {"summary", "trace"}:
            level = "summary"

        start_ms = _now_ms() - max(1, int(lookback_days)) * 86400 * 1000
        end_ms = _now_ms() + 60 * 1000

        # First try as tid (platform order id).
        first = self.trade_search(
            buyer_nick="",
            tid=order_key,
            start_ms=start_ms,
            end_ms=end_ms,
            page_no=1,
            page_size=5,
        )
        if not first.get("ok"):
            return first
        rows = (first.get("data") or {}).get("rows") or []

        if shop_name:
            wanted = str(shop_name).strip()
            rows = [r for r in rows if str(r.get("shopName") or "").strip() == wanted]

        if not rows:
            return {"ok": True, "message": "not_found", "data": {"found": False}}

        row = self._resolve_trade_row_for_tid(
            rows=rows,
            wanted_tid=order_key,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        if row is None:
            return {"ok": True, "message": "not_found", "data": {"found": False}}

        data: dict[str, Any] = {
            "found": True,
            "detail_level": level,
            "order_id": order_key,
            "status": str(row.get("unifiedStatus") or row.get("sysStatus") or row.get("status") or ""),
            "logistics_company": str(row.get("expressName") or row.get("logisticsCompanyName") or ""),
            "tracking_no": str(row.get("logisticsCode") or row.get("outSid") or ""),
            "receiver_name": str(row.get("receiverName") or ""),
            "receiver_mobile_masked": str(row.get("receiverMobile") or ""),
            "shop_name": str(row.get("shopName") or ""),
            "raw": {
                "pay_time_ms": _to_int(row.get("payTime"), 0),
                "consign_time_ms": _to_int(row.get("consignTime"), 0),
                "platform": str(row.get("shopSourceName") or row.get("shopSource") or ""),
            },
        }
        if level == "trace":
            trace_data = self._fetch_logistics_trace(row=row, wanted_tid=order_key)
            data.update(trace_data)
            if (not str(data.get("logistics_company") or "").strip()) and str(
                trace_data.get("trace_company") or ""
            ).strip():
                data["logistics_company"] = str(trace_data.get("trace_company") or "").strip()
            if (not str(data.get("tracking_no") or "").strip()) and str(
                trace_data.get("trace_tracking_no") or ""
            ).strip():
                data["tracking_no"] = str(trace_data.get("trace_tracking_no") or "").strip()
        return {
            "ok": True,
            "message": "ok",
            "data": data,
        }

    def _collect_tids_from_detail_rows(self, *, detail_rows: list[dict[str, Any]]) -> list[str]:
        tids: list[str] = []
        seen: set[str] = set()
        for detail in detail_rows:
            if not isinstance(detail, dict):
                continue
            orders = detail.get("orders")
            if not isinstance(orders, list):
                continue
            for raw in orders:
                if not isinstance(raw, dict):
                    continue
                tid = self._extract_item_tid(raw)
                if not tid or tid in seen:
                    continue
                seen.add(tid)
                tids.append(tid)
        return tids

    def _resolve_trade_row_for_tid(
        self,
        *,
        rows: list[dict[str, Any]],
        wanted_tid: str,
        start_ms: int,
        end_ms: int,
    ) -> dict[str, Any] | None:
        wanted = str(wanted_tid or "").strip()
        if not wanted:
            return rows[0] if rows else None

        for row in rows:
            if str(row.get("tid") or "").strip() == wanted:
                return row

        # Fallback for merged-shipment cases where /trade/search(tid=...) returns
        # another tid with the same sid. Try /trade/search/sids and pick exact tid.
        base = rows[0] if rows else None
        if not isinstance(base, dict):
            return None
        sid = str(base.get("sid") or "").strip()
        if not sid:
            return None
        created_ms = _to_int(base.get("created"), 0) or _to_int(base.get("payTime"), 0)
        detail_start_ms = max(0, created_ms - 2 * 86400 * 1000) if created_ms > 0 else start_ms
        detail_end_ms = created_ms + 2 * 86400 * 1000 if created_ms > 0 else end_ms
        detail_resp = self.trade_search_sids(
            sid=sid,
            user_id=str(base.get("userId") or "").strip(),
            start_ms=detail_start_ms,
            end_ms=detail_end_ms,
            page_no=1,
            page_size=20,
        )
        detail_rows = (
            (detail_resp.get("data") or {}).get("rows")
            if isinstance(detail_resp.get("data"), dict)
            else []
        )
        if not isinstance(detail_rows, list):
            detail_rows = []
        detail_row = self._pick_trade_detail_by_tid(detail_rows=detail_rows, tid=wanted)
        if not isinstance(detail_row, dict):
            return None
        merged = dict(base)
        merged.update(detail_row)
        merged["tid"] = wanted
        return merged

    def _fetch_logistics_trace(self, *, row: dict[str, Any], wanted_tid: str = "") -> dict[str, Any]:
        tid = str(wanted_tid or row.get("tid") or "").strip()
        sid = str(row.get("sid") or "").strip()
        taobao_id = str(row.get("taobaoId") or "").strip()
        oids = self._extract_oids(row)

        source = "trace_search"
        primary = self._logistics_trace_search(
            tid=tid,
            sid=sid,
            taobao_id=taobao_id,
            oids=oids,
        )
        primary_items = self._as_trace_items(primary.get("data"))
        trace_nodes, trace_status_raw, trace_url, trace_company, trace_tracking_no = self._extract_trace_details(
            primary_items
        )
        fallback_message = str(primary.get("message") or "").strip()
        trace_derived = self._derive_trace_judgement(
            trace_nodes=trace_nodes,
            trace_status_raw=trace_status_raw,
            fallback_message=fallback_message,
        )

        return {
            "trace_source": source,
            "trace_found": bool(trace_nodes),
            "trace_node_count": len(trace_nodes),
            "trace": trace_nodes[:80],
            "trace_status": str(trace_derived.get("trace_status_effective") or ""),
            "trace_status_raw": trace_status_raw,
            "trace_status_effective": str(trace_derived.get("trace_status_effective") or ""),
            "trace_status_source": str(trace_derived.get("trace_status_source") or ""),
            "trace_judgement": str(trace_derived.get("trace_judgement") or ""),
            "trace_buyer_signed": bool(trace_derived.get("trace_buyer_signed", False)),
            "trace_return_to_sender": bool(trace_derived.get("trace_return_to_sender", False)),
            "trace_conflict": bool(trace_derived.get("trace_conflict", False)),
            "trace_latest_event": (
                trace_derived.get("trace_latest_event")
                if isinstance(trace_derived.get("trace_latest_event"), dict)
                else {}
            ),
            "trace_latest_buyer_sign_event": (
                trace_derived.get("trace_latest_buyer_sign_event")
                if isinstance(trace_derived.get("trace_latest_buyer_sign_event"), dict)
                else {}
            ),
            "trace_latest_return_event": (
                trace_derived.get("trace_latest_return_event")
                if isinstance(trace_derived.get("trace_latest_return_event"), dict)
                else {}
            ),
            "trace_url": trace_url,
            "trace_company": trace_company,
            "trace_tracking_no": trace_tracking_no,
        }

    def _logistics_trace_search(
        self,
        *,
        tid: str,
        sid: str,
        taobao_id: str,
        oids: str,
    ) -> dict[str, Any]:
        first = self._logistics_trace_search_once(
            tid=tid,
            sid=sid,
            taobao_id=taobao_id,
            oids=oids,
            force_refresh=False,
        )
        if first.get("ok"):
            return first
        return self._logistics_trace_search_once(
            tid=tid,
            sid=sid,
            taobao_id=taobao_id,
            oids=oids,
            force_refresh=True,
        )

    def _logistics_trace_search_once(
        self,
        *,
        tid: str,
        sid: str,
        taobao_id: str,
        oids: str,
        force_refresh: bool,
    ) -> dict[str, Any]:
        try:
            snapshot = self._get_snapshot(force_refresh=force_refresh)
        except Exception as exc:
            return {
                "ok": False,
                "message": f"kuaimai_login_failed:{type(exc).__name__}",
                "data": {},
            }

        erp_host = str(snapshot.get("erp_host") or self.erp_host).rstrip("/")
        headers = self._request_headers(snapshot=snapshot, erp_host=erp_host)
        params: dict[str, Any] = {
            "type": "get",
            "sid": str(sid or "").strip(),
            "tid": str(tid or "").strip(),
        }
        if taobao_id:
            params["taobaoId"] = taobao_id
        if oids:
            params["oids"] = oids
        return self._request_json(
            path="/trade/logistics/trace/search",
            method="GET",
            params=params,
            headers=headers,
            timeout=self.timeout_sec,
        )

    def _request_headers(self, *, snapshot: dict[str, Any], erp_host: str) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "cookie": str(snapshot.get("cookie") or ""),
            "origin": erp_host,
            "referer": erp_host + "/index.html",
            "user-agent": str(snapshot.get("user_agent") or ""),
            "x-requested-with": "XMLHttpRequest",
        }
        if self.company_id:
            headers["companyid"] = self.company_id
        return headers

    def _request_json(
        self,
        *,
        path: str,
        method: str,
        params: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if str(v or "").strip() != ""})
        erp_host = str(headers.get("origin") or self.erp_host).rstrip("/")
        url = erp_host + path
        if query:
            url = f"{url}?{query}"
        req = urllib.request.Request(url=url, method=method.upper())
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                return {"ok": False, "message": "logistics_trace_timeout", "data": {}}
            return {
                "ok": False,
                "message": f"logistics_trace_request_failed:{type(exc).__name__}",
                "data": {},
            }
        except TimeoutError:
            return {"ok": False, "message": "logistics_trace_timeout", "data": {}}

        try:
            payload = json.loads(raw or "{}")
        except Exception:
            return {
                "ok": False,
                "message": "logistics_trace_json_decode_failed",
                "data": {"body_preview": (raw or "")[:300]},
            }
        if int(payload.get("result") or 0) != 1:
            return {
                "ok": False,
                "message": "logistics_trace_result_not_ok",
                "data": {"raw": payload},
            }
        return {
            "ok": True,
            "message": "ok",
            "data": payload.get("data"),
        }

    def _extract_oids(self, row: dict[str, Any]) -> str:
        orders = row.get("orders")
        if not isinstance(orders, list):
            return ""
        values: list[str] = []
        for item in orders:
            if not isinstance(item, dict):
                continue
            oid = str(item.get("oid") or "").strip()
            if oid and (oid not in values):
                values.append(oid)
        return ",".join(values)

    def _as_trace_items(self, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    def _extract_trace_details(
        self, items: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], str, str, str, str]:
        status = ""
        trace_url = ""
        company = ""
        tracking_no = ""
        nodes: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for item in items:
            if not status:
                status = str(item.get("status") or "").strip()
            if not trace_url:
                trace_url = str(item.get("url") or "").strip()
            if not company:
                company = str(item.get("companyName") or item.get("expressName") or "").strip()
            if not tracking_no:
                tracking_no = str(item.get("outSid") or item.get("logisticsCode") or "").strip()
            trace_list = item.get("traceList")
            if not isinstance(trace_list, list):
                trace_list = []
            for node in trace_list:
                if not isinstance(node, dict):
                    continue
                event_time = str(
                    node.get("time")
                    or node.get("acceptTime")
                    or node.get("statusTime")
                    or node.get("ftime")
                    or node.get("scanTime")
                    or ""
                ).strip()
                desc = str(
                    node.get("content")
                    or node.get("context")
                    or node.get("desc")
                    or node.get("remark")
                    or node.get("statusDesc")
                    or node.get("status")
                    or ""
                ).strip()
                location = str(
                    node.get("location")
                    or node.get("city")
                    or node.get("areaName")
                    or node.get("station")
                    or ""
                ).strip()
                node_status = str(node.get("status") or node.get("state") or "").strip()
                action = str(
                    node.get("action")
                    or node.get("event")
                    or node.get("opType")
                    or ""
                ).strip()
                key = (event_time, desc, location, node_status, action)
                if key in seen:
                    continue
                seen.add(key)
                nodes.append(
                    {
                        "time": event_time,
                        "desc": desc,
                        "location": location,
                        "status": node_status,
                        "action": action,
                    }
                )
        return nodes, status, trace_url, company, tracking_no

    def _derive_trace_judgement(
        self,
        *,
        trace_nodes: list[dict[str, Any]],
        trace_status_raw: str,
        fallback_message: str,
    ) -> dict[str, Any]:
        latest_event: dict[str, Any] | None = None
        latest_event_key = ("", -1)
        latest_return_event: dict[str, Any] | None = None
        latest_return_key = ("", -1)
        latest_buyer_sign_event: dict[str, Any] | None = None
        latest_buyer_sign_key = ("", -1)

        for idx, node in enumerate(trace_nodes):
            if not isinstance(node, dict):
                continue
            event_time = _normalize_trace_event_time(node.get("time"))
            event_key = (event_time, idx)
            if event_key >= latest_event_key:
                latest_event = node
                latest_event_key = event_key

            text = " ".join(
                [
                    str(node.get("desc") or "").strip(),
                    str(node.get("status") or "").strip(),
                    str(node.get("action") or "").strip(),
                ]
            ).strip()
            if _is_return_to_sender_text(text) and event_key >= latest_return_key:
                latest_return_event = node
                latest_return_key = event_key
            if _looks_like_buyer_signed_text(text) and event_key >= latest_buyer_sign_key:
                latest_buyer_sign_event = node
                latest_buyer_sign_key = event_key

        judgement = "unknown"
        status_source = "empty"
        status_effective = ""
        buyer_signed = False
        return_to_sender = False

        if latest_buyer_sign_event is not None and latest_buyer_sign_key >= latest_return_key:
            judgement = "buyer_signed"
            status_source = "trace_nodes"
            status_effective = "买家已签收"
            buyer_signed = True
        elif latest_return_event is not None:
            return_desc = str(latest_return_event.get("desc") or "").strip()
            if _contains_any(return_desc, BUYER_SIGN_KEYWORDS):
                judgement = "intercept_returned"
                status_effective = "拦截退回已签收（发货地）"
            else:
                judgement = "intercept_returning"
                status_effective = "拦截退回中"
            status_source = "trace_nodes"
            return_to_sender = True
        elif latest_event is not None:
            latest_desc = str(latest_event.get("desc") or "").strip()
            if _contains_any(latest_desc, IN_TRANSIT_KEYWORDS):
                judgement = "in_transit"
                status_effective = latest_desc or "运输中"
                status_source = "trace_nodes"

        raw_status = str(trace_status_raw or "").strip()
        if not status_effective:
            if _looks_like_buyer_signed_text(raw_status):
                judgement = "buyer_signed"
                status_effective = "买家已签收"
                status_source = "trace_status_raw"
                buyer_signed = True
            elif _is_return_to_sender_text(raw_status):
                judgement = "intercept_returning"
                status_effective = raw_status
                status_source = "trace_status_raw"
                return_to_sender = True
            elif raw_status:
                judgement = "unknown"
                status_effective = raw_status
                status_source = "trace_status_raw"
            elif fallback_message and fallback_message != "ok":
                judgement = "unknown"
                status_effective = fallback_message
                status_source = "fallback_message"

        raw_kind = ""
        if raw_status:
            if _looks_like_buyer_signed_text(raw_status):
                raw_kind = "buyer_signed"
            elif _is_return_to_sender_text(raw_status):
                raw_kind = "return_to_sender"
            else:
                raw_kind = "other"

        effective_kind = ""
        if judgement == "buyer_signed":
            effective_kind = "buyer_signed"
        elif judgement in {"intercept_returning", "intercept_returned"}:
            effective_kind = "return_to_sender"
        elif judgement in {"in_transit", "unknown"} and status_effective:
            effective_kind = "other"

        return {
            "trace_status_effective": status_effective,
            "trace_status_source": status_source,
            "trace_judgement": judgement,
            "trace_buyer_signed": buyer_signed,
            "trace_return_to_sender": return_to_sender,
            "trace_conflict": bool(raw_kind and effective_kind and raw_kind != effective_kind),
            "trace_latest_event": latest_event or {},
            "trace_latest_buyer_sign_event": latest_buyer_sign_event or {},
            "trace_latest_return_event": latest_return_event or {},
        }

    def refund_by_identity(
        self,
        *,
        km_name: str,
        shop_name: str,
        lookback_days: int,
        limit: int,
    ) -> dict[str, Any]:
        base = self.orders_by_identity(
            km_name=km_name,
            shop_name=shop_name,
            lookback_days=lookback_days,
            limit=limit,
        )
        if not base.get("ok"):
            return base

        orders = (base.get("data") or {}).get("orders") or []
        refund_orders = [order for order in orders if bool(order.get("is_refund"))]
        refund_amount_estimate = sum(_to_float(item.get("amount"), 0.0) for item in refund_orders)
        return {
            "ok": True,
            "message": "ok",
            "data": {
                "found": bool(orders),
                "km_name": str(km_name or "").strip(),
                "shop_name": str(shop_name or "").strip(),
                "total_orders": len(orders),
                "refund_order_count": len(refund_orders),
                "refund_amount_estimate": round(refund_amount_estimate, 2),
                "recent_refund_orders": refund_orders[:20],
            },
        }

    def aftersale_search(
        self,
        *,
        tid: str,
        start_ms: int,
        end_ms: int,
        page_no: int,
        page_size: int,
    ) -> dict[str, Any]:
        first = self._aftersale_search_once(
            tid=tid,
            start_ms=start_ms,
            end_ms=end_ms,
            page_no=page_no,
            page_size=page_size,
            force_refresh=False,
        )
        if first.get("ok"):
            return first
        return self._aftersale_search_once(
            tid=tid,
            start_ms=start_ms,
            end_ms=end_ms,
            page_no=page_no,
            page_size=page_size,
            force_refresh=True,
        )

    def trade_search_sids(
        self,
        *,
        sid: str,
        user_id: str,
        start_ms: int,
        end_ms: int,
        page_no: int,
        page_size: int,
    ) -> dict[str, Any]:
        first = self._trade_search_sids_once(
            sid=sid,
            user_id=user_id,
            start_ms=start_ms,
            end_ms=end_ms,
            page_no=page_no,
            page_size=page_size,
            force_refresh=False,
        )
        if first.get("ok"):
            return first
        return self._trade_search_sids_once(
            sid=sid,
            user_id=user_id,
            start_ms=start_ms,
            end_ms=end_ms,
            page_no=page_no,
            page_size=page_size,
            force_refresh=True,
        )

    def _trade_search_sids_once(
        self,
        *,
        sid: str,
        user_id: str,
        start_ms: int,
        end_ms: int,
        page_no: int,
        page_size: int,
        force_refresh: bool,
    ) -> dict[str, Any]:
        if not str(sid or "").strip():
            return {"ok": False, "message": "missing_sid", "data": {}}
        try:
            snapshot = self._get_snapshot(force_refresh=force_refresh)
        except Exception as exc:
            return {
                "ok": False,
                "message": f"kuaimai_login_failed:{type(exc).__name__}",
                "data": {"error": str(exc)[:300]},
            }

        erp_host = str(snapshot.get("erp_host") or self.erp_host).rstrip("/")
        headers = {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "cookie": str(snapshot.get("cookie") or ""),
            "origin": erp_host,
            "referer": erp_host + "/index.html",
            "user-agent": str(snapshot.get("user_agent") or ""),
            "x-requested-with": "XMLHttpRequest",
        }
        if self.company_id:
            headers["companyid"] = self.company_id

        data = {
            "api_name": "trade_search_sids",
            "order": "asc",
            "pageNo": int(page_no),
            "pageSize": int(page_size),
            "sid": str(sid or "").strip(),
            "shortId": "",
            "tid": "",
            "deliverStatus": "",
            "timeType": "pay_time",
            "startTime": int(start_ms),
            "endTime": int(end_ms),
            "outSid": "",
            "buyerNick": "",
            "isExcep": "",
            "expressStatus": "",
            "key": "itemTitle",
            "queryType": 0,
            "text": "",
            "skuProp": "",
            "itemTitle": "",
            "userId": str(user_id or "").strip(),
            "warehouseId": "",
            "highlight": "false",
            "_t": _now_ms(),
            "field": "pay_time",
            "sids": str(sid or "").strip(),
            "withStock": "true",
            "useHasNext": "1",
            "containMemo": "",
            "outerIdAndSysSkuIds": "",
            "outerIdAndSysItemIds": "",
            "onlyOuterIdAndSysSkuIds": "",
            "onlyOuterIdAndSysItemIds": "",
            "excludeOuterIdAndSysItemIds": "",
            "excludeOuterIdAndSysSkuIds": "",
            "outerId": "",
            "onlyOuterIdAndSysSkuIdType": 0,
            "excludeSysOuterIds": "",
            "uniqueCodes": "",
            "minutesAfterPaidOrderAreNotDisplayed": 0,
            "itemType": "",
            "platSkuProp": "",
        }
        req = urllib.request.Request(
            url=erp_host + "/trade/search/sids",
            data=urllib.parse.urlencode(data).encode("utf-8"),
            method="POST",
        )
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                return {"ok": False, "message": "trade_search_sids_timeout", "data": {}}
            return {
                "ok": False,
                "message": f"trade_search_sids_request_failed:{type(exc).__name__}",
                "data": {},
            }
        except TimeoutError:
            return {"ok": False, "message": "trade_search_sids_timeout", "data": {}}

        try:
            payload = json.loads(raw or "{}")
        except Exception:
            return {
                "ok": False,
                "message": "trade_search_sids_json_decode_failed",
                "data": {"body_preview": (raw or "")[:300]},
            }
        if int(payload.get("result") or 0) != 1:
            return {
                "ok": False,
                "message": "trade_search_sids_result_not_ok",
                "data": {"raw": payload},
            }
        rows = ((payload.get("data") or {}).get("list") or [])
        if not isinstance(rows, list):
            rows = []
        return {"ok": True, "message": "ok", "data": {"rows": rows}}

    def _aftersale_search_once(
        self,
        *,
        tid: str,
        start_ms: int,
        end_ms: int,
        page_no: int,
        page_size: int,
        force_refresh: bool,
    ) -> dict[str, Any]:
        try:
            snapshot = self._get_snapshot(force_refresh=force_refresh)
        except Exception as exc:
            return {
                "ok": False,
                "message": f"kuaimai_login_failed:{type(exc).__name__}",
                "data": {"error": str(exc)[:300]},
            }

        erp_host = str(snapshot.get("erp_host") or self.erp_host).rstrip("/")
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/x-www-form-urlencoded",
            "cookie": str(snapshot.get("cookie") or ""),
            "origin": erp_host,
            "referer": erp_host + "/index.html",
            "user-agent": str(snapshot.get("user_agent") or ""),
            "x-requested-with": "XMLHttpRequest",
            "Module-Path": "/aftersale/sale_handle_next/",
        }
        if self.company_id:
            headers["companyid"] = self.company_id

        data = {
            "startDate": int(start_ms),
            "endDate": int(end_ms),
            "timeType": 4,
            "queryFlag": 1,
            "queryType": "id",
            "status": -1,
            "warehouseIds": "",
            "tid": str(tid or "").strip(),
            "page": int(page_no),
            "pageSize": int(page_size),
            "expressId": "",
            "advanceStatus": "",
            "markIntercepts": "",
            "api_name": "as_order_deal_pageList",
        }

        req = urllib.request.Request(
            url=erp_host + "/as/order/deal/pageList",
            data=urllib.parse.urlencode(data).encode("utf-8"),
            method="POST",
        )
        for key, value in headers.items():
            req.add_header(key, value)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                return {"ok": False, "message": "aftersale_search_timeout", "data": {}}
            return {
                "ok": False,
                "message": f"aftersale_search_request_failed:{type(exc).__name__}",
                "data": {},
            }
        except TimeoutError:
            return {"ok": False, "message": "aftersale_search_timeout", "data": {}}

        try:
            payload = json.loads(raw or "{}")
        except Exception:
            return {
                "ok": False,
                "message": "aftersale_search_json_decode_failed",
                "data": {"body_preview": (raw or "")[:300]},
            }

        if int(payload.get("result") or 0) != 1:
            return {
                "ok": False,
                "message": "aftersale_search_result_not_ok",
                "data": {"raw": payload},
            }

        rows = ((payload.get("data") or {}).get("list") or [])
        if not isinstance(rows, list):
            rows = []
        return {"ok": True, "message": "ok", "data": {"rows": rows}}

    def trade_search(
        self,
        *,
        buyer_nick: str,
        tid: str,
        start_ms: int,
        end_ms: int,
        page_no: int,
        page_size: int,
    ) -> dict[str, Any]:
        first = self._trade_search_once(
            buyer_nick=buyer_nick,
            tid=tid,
            start_ms=start_ms,
            end_ms=end_ms,
            page_no=page_no,
            page_size=page_size,
            force_refresh=False,
        )
        if first.get("ok"):
            return first
        # Retry once after forced re-login.
        return self._trade_search_once(
            buyer_nick=buyer_nick,
            tid=tid,
            start_ms=start_ms,
            end_ms=end_ms,
            page_no=page_no,
            page_size=page_size,
            force_refresh=True,
        )

    def _trade_search_once(
        self,
        *,
        buyer_nick: str,
        tid: str,
        start_ms: int,
        end_ms: int,
        page_no: int,
        page_size: int,
        force_refresh: bool,
    ) -> dict[str, Any]:
        try:
            snapshot = self._get_snapshot(force_refresh=force_refresh)
        except Exception as exc:
            return {
                "ok": False,
                "message": f"kuaimai_login_failed:{type(exc).__name__}",
                "data": {"error": str(exc)[:300]},
            }

        erp_host = str(snapshot.get("erp_host") or self.erp_host)
        headers = {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "cookie": str(snapshot.get("cookie") or ""),
            "origin": erp_host,
            "referer": erp_host + "/index.html",
            "user-agent": str(snapshot.get("user_agent") or ""),
            "x-requested-with": "XMLHttpRequest",
        }
        if self.company_id:
            headers["companyid"] = self.company_id

        data = {
            "api_name": "trade_search",
            "queryId": "8",
            "useHasNext": 1,
            "pageNo": int(page_no),
            "pageSize": int(page_size),
            "shortId": "",
            "tid": str(tid or "").strip(),
            "highlightTid": str(tid or "").strip(),
            "uniqueCodes": "",
            "deliverStatus": "",
            "timeType": "pay_time",
            "startTime": int(start_ms),
            "endTime": int(end_ms),
            "dateTypeSelect": 0,
            "minutesAfterPaidOrderAreNotDisplayed": 0,
            "outSid": "",
            "buyerNick": str(buyer_nick or "").strip(),
            "isExcep": "",
            "expressStatus": "",
            "secondUserId": "",
            "orderFields": "",
            "skuProp": "",
            "platSkuProp": "",
            "skuOuterId": "",
            "itemTitle": "",
            "userId": "",
            "warehouseId": "",
            "spcAuthorName": "",
            "spcAuthorId": "",
            "_t": _now_ms(),
            "t": str(_now_ms()),
            "highlight": "true" if str(tid or "").strip() else "false",
            "field": "pay_time",
            "order": "desc",
            "needOrder": "0",
            "useCompress": "0",
            "key": "itemTitle",
            "queryType": 0,
            "itemType": "",
            "text": "",
        }

        req = urllib.request.Request(
            url=erp_host.rstrip("/") + "/trade/search",
            data=urllib.parse.urlencode(data).encode("utf-8"),
            method="POST",
        )
        for key, value in headers.items():
            req.add_header(key, value)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                return {"ok": False, "message": "trade_search_timeout", "data": {}}
            return {
                "ok": False,
                "message": f"trade_search_request_failed:{type(exc).__name__}",
                "data": {},
            }
        except TimeoutError:
            return {"ok": False, "message": "trade_search_timeout", "data": {}}

        try:
            payload = json.loads(raw or "{}")
        except Exception:
            return {
                "ok": False,
                "message": "trade_search_json_decode_failed",
                "data": {"body_preview": (raw or "")[:300]},
            }

        if int(payload.get("result") or 0) != 1:
            return {"ok": False, "message": "trade_search_result_not_ok", "data": {"raw": payload}}

        rows = ((payload.get("data") or {}).get("list") or [])
        if not isinstance(rows, list):
            rows = []
        return {"ok": True, "message": "ok", "data": {"rows": rows}}

    def _normalize_order(
        self,
        row: dict[str, Any],
        *,
        detail_row: dict[str, Any] | None = None,
        wanted_tid: str = "",
    ) -> dict[str, Any]:
        detail = detail_row if isinstance(detail_row, dict) else {}
        merged = detail if detail else row
        item_image_url = self._normalize_image_url(merged.get("itemImage") or row.get("itemImage") or "")
        wanted_tid_clean = str(wanted_tid or row.get("tid") or "").strip()
        detail_items = self._normalize_order_items(detail=detail, wanted_tid=wanted_tid_clean)
        detail_order_rows = self._collect_detail_order_rows(detail=detail, wanted_tid=wanted_tid_clean)
        first_item_spec = self._pick_first_item_spec(detail_items=detail_items)
        item_names = self._collect_item_names(detail_items=detail_items)
        item_specs = self._collect_item_specs(detail_items=detail_items)
        item_quantity_total = 0
        for item in detail_items:
            if not isinstance(item, dict):
                continue
            item_quantity_total += _to_int(item.get("quantity"), 0)

        display_image_url, display_image_source = self._pick_top_display_image(detail_items=detail_items)
        seller_memo = self._resolve_seller_memo(
            row=row,
            detail_row=detail,
            wanted_tid=wanted_tid_clean,
        )

        receiver_state = str(merged.get("receiverState") or row.get("receiverState") or "").strip()
        receiver_city = str(merged.get("receiverCity") or row.get("receiverCity") or "").strip()
        receiver_district = str(merged.get("receiverDistrict") or row.get("receiverDistrict") or "").strip()
        receiver_address = str(merged.get("receiverAddress") or row.get("receiverAddress") or "").strip()
        receiver_name = str(merged.get("receiverName") or row.get("receiverName") or "").strip()
        receiver_mobile = str(merged.get("receiverMobile") or row.get("receiverMobile") or "").strip()

        paid_at_ms = _to_int(row.get("payTime"), 0)
        amount = round(_to_float(row.get("acPayment"), _to_float(row.get("payment"), 0.0)), 2)
        status = str(row.get("unifiedStatus") or row.get("sysStatus") or row.get("status") or "")
        if detail_order_rows:
            pay_candidates = [
                _to_int(raw.get("payTime"), 0)
                or _to_int(raw.get("tradeCreated"), 0)
                or _to_int(raw.get("created"), 0)
                for raw in detail_order_rows
                if isinstance(raw, dict)
            ]
            pay_candidates = [v for v in pay_candidates if v > 0]
            if pay_candidates:
                paid_at_ms = max(pay_candidates)
            amount = round(
                sum(
                    _to_float(raw.get("acPayment"), _to_float(raw.get("payment"), 0.0))
                    for raw in detail_order_rows
                    if isinstance(raw, dict)
                ),
                2,
            )
            for raw in detail_order_rows:
                if not isinstance(raw, dict):
                    continue
                candidate = str(raw.get("unifiedStatus") or raw.get("sysStatus") or raw.get("status") or "").strip()
                if candidate:
                    status = candidate
                    break

        return {
            "order_id": wanted_tid_clean if wanted_tid_clean else str(row.get("tid") or ""),
            "sid": str(row.get("sid") or merged.get("sid") or ""),
            "paid_at_ms": paid_at_ms,
            "amount": amount,
            "status": status,
            "item_names": item_names,
            "item_specs": item_specs,
            "spec": str(row.get("propertiesName") or first_item_spec or ""),
            "shop_name": str(row.get("shopName") or ""),
            "platform": str(row.get("shopSourceName") or row.get("shopSource") or ""),
            "buyer_nick": str(row.get("buyerNick") or ""),
            "open_uid": str(row.get("openUid") or ""),
            "seller_memo": seller_memo,
            "logistics_company": str(row.get("expressName") or row.get("logisticsCompanyName") or ""),
            "tracking_no": str(row.get("logisticsCode") or row.get("outSid") or ""),
            "post_fee": round(_to_float(merged.get("postFee"), _to_float(row.get("postFee"), 0.0)), 2),
            "item_count": (
                int(item_quantity_total)
                if item_quantity_total > 0
                else _to_int(merged.get("itemCount"), _to_int(row.get("itemCount"), 0))
            ),
            "display_image_url": display_image_url,
            "display_image_source": display_image_source,
            "receiver_state": receiver_state,
            "receiver_city": receiver_city,
            "receiver_district": receiver_district,
            "receiver_address_masked": receiver_address,
            "receiver_name": receiver_name,
            "receiver_mobile_masked": receiver_mobile,
            # Backward-compatible address aliases for callers that still read address_* keys.
            "address_province": receiver_state,
            "address_city": receiver_city,
            "address_district": receiver_district,
            "address_detail_masked": receiver_address,
            "receiver": {
                "state": receiver_state,
                "city": receiver_city,
                "district": receiver_district,
                "address_masked": receiver_address,
                "name": receiver_name,
                "mobile_masked": receiver_mobile,
            },
            "items": detail_items,
            "is_refund": self._is_refund(row),
        }

    def _resolve_seller_memo(
        self,
        *,
        row: dict[str, Any],
        detail_row: dict[str, Any] | None = None,
        wanted_tid: str = "",
    ) -> str:
        wanted = str(wanted_tid or row.get("tid") or "").strip()
        sources = []
        if isinstance(detail_row, dict) and detail_row:
            sources.append(detail_row)
        sources.append(row)

        for source in sources:
            message_memos = source.get("messageMemos")
            if isinstance(message_memos, dict):
                if wanted:
                    candidate = message_memos.get(wanted)
                    if isinstance(candidate, dict):
                        memo = str(candidate.get("sellerMemo") or "").strip()
                        if memo:
                            return memo
                for candidate in message_memos.values():
                    if not isinstance(candidate, dict):
                        continue
                    memo = str(candidate.get("sellerMemo") or "").strip()
                    if memo:
                        return memo

            memo = str(source.get("sellerMemo") or "").strip()
            if memo:
                return memo

        return ""

    def _collect_detail_order_rows(self, *, detail: dict[str, Any], wanted_tid: str) -> list[dict[str, Any]]:
        if not isinstance(detail, dict):
            return []
        orders = detail.get("orders")
        if not isinstance(orders, list):
            return []
        wanted = str(wanted_tid or "").strip()
        if not wanted:
            return [raw for raw in orders if isinstance(raw, dict)]
        matched: list[dict[str, Any]] = []
        for raw in orders:
            if not isinstance(raw, dict):
                continue
            if self._extract_item_tid(raw) == wanted:
                matched.append(raw)
        return matched

    def _normalize_order_items(self, *, detail: dict[str, Any], wanted_tid: str = "") -> list[dict[str, Any]]:
        if not isinstance(detail, dict):
            return []
        orders = detail.get("orders")
        if not isinstance(orders, list):
            return []
        candidates: list[dict[str, Any]] = []
        for raw in orders:
            if not isinstance(raw, dict):
                continue
            order_tid = self._extract_item_tid(raw)
            platform_image_url = self._normalize_image_url(raw.get("picPath") or "")
            system_image_url = self._normalize_image_url(raw.get("sysPicPath") or "")
            show_pic_switch = str(raw.get("showPicSwitch") or "").strip()
            display_image_url, display_image_source = self._pick_item_display_image(
                show_pic_switch=show_pic_switch,
                platform_image_url=platform_image_url,
                system_image_url=system_image_url,
            )
            best_image_url = self._pick_best_image(
                platform_image_url=platform_image_url,
                system_image_url=system_image_url,
            )
            candidates.append(
                {
                    "order_tid": order_tid,
                    "id": str(raw.get("id") or ""),
                    "oid": str(raw.get("oid") or ""),
                    "outer_id": str(raw.get("outerId") or ""),
                    "system_outer_id": str(raw.get("sysOuterId") or ""),
                    "platform_title": str(raw.get("platFormTitle") or ""),
                    "title": str(raw.get("title") or ""),
                    "platform_spec": str(raw.get("platSkuPropertiesName") or ""),
                    "system_spec": str(raw.get("sysSkuPropertiesName") or ""),
                    "sku_properties_name": str(raw.get("skuPropertiesName") or ""),
                    "show_pic_switch": show_pic_switch,
                    "platform_image_url": platform_image_url,
                    "system_image_url": system_image_url,
                    "display_image_url": display_image_url,
                    "display_image_source": display_image_source,
                    "best_image_url": best_image_url,
                    "quantity": _to_int(raw.get("num"), 0),
                    "amount": round(_to_float(raw.get("acPayment"), _to_float(raw.get("payment"), 0.0)), 2),
                    "num_iid": str(raw.get("numIid") or ""),
                    "sku_id": str(raw.get("skuId") or ""),
                    "item_sys_id": str(raw.get("itemSysId") or ""),
                    "sku_sys_id": str(raw.get("skuSysId") or ""),
                }
            )

        wanted = str(wanted_tid or "").strip()
        if not wanted:
            return candidates

        matched = [item for item in candidates if str(item.get("order_tid") or "").strip() == wanted]
        if matched:
            return matched

        has_any_tid = any(str(item.get("order_tid") or "").strip() for item in candidates)
        if has_any_tid:
            # All child lines belong to other tids in the same merged shipment.
            return []

        # Fallback only when source data does not carry per-item tid.
        return candidates

    def _pick_trade_detail_by_tid(self, *, detail_rows: list[dict[str, Any]], tid: str) -> dict[str, Any] | None:
        if not detail_rows:
            return None
        wanted_tid = str(tid or "").strip()
        if wanted_tid:
            for row in detail_rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("tid") or "").strip() == wanted_tid:
                    return row
        for row in detail_rows:
            if isinstance(row, dict):
                return row
        return None

    def _pick_top_display_image(self, *, detail_items: list[dict[str, Any]]) -> tuple[str, str]:
        for item in detail_items:
            display = str(item.get("display_image_url") or "").strip()
            source = str(item.get("display_image_source") or "").strip()
            if display:
                return display, source or "item.display"
        return "", ""

    def _collect_item_names(self, *, detail_items: list[dict[str, Any]]) -> list[str]:
        names: list[str] = []
        for item in detail_items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("platform_title") or item.get("title") or "").strip()
            if not name:
                continue
            names.append(name)
        return names

    def _pick_first_item_spec(self, *, detail_items: list[dict[str, Any]]) -> str:
        for item in detail_items:
            if not isinstance(item, dict):
                continue
            spec = str(item.get("platform_spec") or item.get("system_spec") or "").strip()
            if spec:
                return spec
        return ""

    def _collect_item_specs(self, *, detail_items: list[dict[str, Any]]) -> list[str]:
        specs: list[str] = []
        for item in detail_items:
            if not isinstance(item, dict):
                continue
            spec = str(item.get("platform_spec") or item.get("system_spec") or "").strip()
            if not spec:
                continue
            specs.append(spec)
        return specs

    def _pick_item_display_image(
        self,
        *,
        show_pic_switch: str,
        platform_image_url: str,
        system_image_url: str,
    ) -> tuple[str, str]:
        # Match ERP front-end behavior: "平" => platform pic; "系" => system pic.
        sw = str(show_pic_switch or "").strip()
        if sw == "平":
            if platform_image_url:
                return platform_image_url, "picPath"
            if system_image_url:
                return system_image_url, "sysPicPath"
        elif sw == "系":
            if system_image_url:
                return system_image_url, "sysPicPath"
            if platform_image_url:
                return platform_image_url, "picPath"
        else:
            if platform_image_url and (not self._is_no_pic_url(platform_image_url)):
                return platform_image_url, "picPath"
            if system_image_url and (not self._is_no_pic_url(system_image_url)):
                return system_image_url, "sysPicPath"
            if platform_image_url:
                return platform_image_url, "picPath"
            if system_image_url:
                return system_image_url, "sysPicPath"
        return "", ""

    def _pick_best_image(
        self,
        *,
        platform_image_url: str,
        system_image_url: str,
    ) -> str:
        if platform_image_url and (not self._is_no_pic_url(platform_image_url)):
            return platform_image_url
        if system_image_url and (not self._is_no_pic_url(system_image_url)):
            return system_image_url
        if platform_image_url:
            return platform_image_url
        if system_image_url:
            return system_image_url
        return ""

    def _normalize_image_url(self, raw: Any) -> str:
        val = str(raw or "").strip()
        if not val:
            return ""
        if val.startswith("http://") or val.startswith("https://"):
            return val
        if val.startswith("/"):
            return self.erp_host.rstrip("/") + val
        return val

    def _extract_item_tid(self, raw: dict[str, Any]) -> str:
        if not isinstance(raw, dict):
            return ""
        direct = str(raw.get("tid") or "").strip()
        if direct:
            return direct
        ext = raw.get("orderExt")
        if isinstance(ext, dict):
            ext_tid = str(ext.get("tid") or "").strip()
            if ext_tid:
                return ext_tid
        return ""

    def _is_no_pic_url(self, url: str) -> bool:
        val = str(url or "").strip().lower()
        if not val:
            return True
        return val.endswith("/resources/css/build/images/no_pic.png")

    def _normalize_aftersale(self, row: dict[str, Any]) -> dict[str, Any]:
        platform_status = str(row.get("platformStatusText") or "").strip()
        if not platform_status:
            platform_status = str(row.get("statusText") or row.get("status") or "").strip()
        reason = str(
            row.get("reason")
            or row.get("reasonText")
            or row.get("refundReason")
            or row.get("refundReasonText")
            or ""
        ).strip()
        aftersale_type = str(row.get("afterSaleTypeText") or row.get("afterSaleType") or "").strip()
        status_text = str(row.get("statusText") or row.get("status") or "").strip()
        status_code = str(row.get("status") or "").strip()

        return {
            "order_id": str(row.get("tid") or "").strip(),
            "shop_name": str(row.get("shopName") or "").strip(),
            "platform": str(row.get("sourceName") or row.get("source") or "").strip(),
            "paid_amount": round(_to_float(row.get("payment"), 0.0) / 100.0, 2),
            "platform_refund_amount": round(_to_float(row.get("refundMoney"), 0.0) / 100.0, 2),
            "platform_status": platform_status,
            "platform_status_code": str(row.get("platformStatus") or "").strip(),
            "return_express_company": str(row.get("refundExpressCompany") or "").strip(),
            "return_tracking_no": str(row.get("refundExpressId") or "").strip(),
            "receive_goods_time_ms": _to_int(row.get("receiveGoodsTime"), 0),
            "receive_goods_operator": str(
                row.get("receiveGoodsOperatorNames")
                or row.get("receiveGoodsOperatorName")
                or ""
            ).strip(),
            "storage_progress": _to_int(row.get("storageProgress"), 0),
            "aftersale_reason": reason,
            "aftersale_type": aftersale_type,
            "is_open": self._is_open_aftersale(status_text=status_text, status_code=status_code),
            "open_reason": status_text or status_code,
            "status_text": status_text,
            "status_code": status_code,
            "updated_at_ms": _to_int(row.get("modified"), 0),
            "created_at_ms": _to_int(row.get("created"), 0),
        }

    def _is_open_aftersale(self, *, status_text: str, status_code: str) -> bool:
        text = str(status_text or "").strip()
        code = str(status_code or "").strip()
        if "未解决" in text:
            return True
        if text and ("已解决" in text or "作废" in text):
            return False
        return code in {"0", "1", "2", "3", "4", "5"}


    def _is_refund(self, row: dict[str, Any]) -> bool:
        if str(row.get("isRefund") or "").strip() in {"1", "true", "True"}:
            return True
        status = " ".join(
            [
                str(row.get("status") or ""),
                str(row.get("unifiedStatus") or ""),
                str(row.get("chStatus") or ""),
                str(row.get("refundStatus") or ""),
            ]
        ).lower()
        if "refund" in status or "退" in status:
            return True
        return False

    def _get_snapshot(self, *, force_refresh: bool) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            if (
                (not force_refresh)
                and self._snapshot is not None
                and (now - self._snapshot_at) < self.snapshot_ttl_sec
            ):
                return dict(self._snapshot)

            snapshot = asyncio.run(self._login_with_playwright())
            self._snapshot = dict(snapshot)
            self._snapshot_at = now
            return dict(snapshot)

    async def _login_with_playwright(self) -> dict[str, Any]:
        if not self.company_name or not self.account or not self.password:
            raise RuntimeError("kuaimai_credentials_not_configured")

        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise RuntimeError("playwright_not_installed") from exc

        launch_kwargs: dict[str, Any] = {"headless": self.login_headless}
        if self.chromium_path:
            if not os.path.exists(self.chromium_path):
                raise RuntimeError(f"kuaimai_chromium_path_not_found:{self.chromium_path}")
            launch_kwargs["executable_path"] = self.chromium_path

        async with async_playwright() as playwright:
            if "executable_path" in launch_kwargs:
                browser = await playwright.chromium.launch(**launch_kwargs)
            else:
                if not self.browser_channel:
                    raise RuntimeError("kuaimai_browser_channel_required")
                try:
                    browser = await playwright.chromium.launch(
                        channel=self.browser_channel,
                        **launch_kwargs,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"kuaimai_browser_launch_failed:{self.browser_channel}"
                    ) from exc
            try:
                state_path = Path(self.storage_state_path).expanduser() if self.storage_state_path else None
                context_kwargs: dict[str, Any] = {}
                if state_path and state_path.exists():
                    context_kwargs["storage_state"] = str(state_path)
                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()
                await page.goto(self.erp_host, wait_until="domcontentloaded")
                await page.wait_for_timeout(1200)

                if not self._is_logged_url(page.url):
                    await self._do_login(page)
                    # wait login jump
                    ok = False
                    for _ in range(25):
                        if self._is_logged_url(page.url):
                            ok = True
                            break
                        await page.wait_for_timeout(500)
                    if not ok:
                        raise RuntimeError(f"kuaimai_login_redirect_failed:{page.url}")

                if state_path:
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    await context.storage_state(path=str(state_path))

                cookies = await context.cookies()
                pairs = []
                for cookie in cookies:
                    domain = str(cookie.get("domain") or "")
                    if "superboss.cc" not in domain:
                        continue
                    name = str(cookie.get("name") or "").strip()
                    value = str(cookie.get("value") or "").strip()
                    if name:
                        pairs.append(f"{name}={value}")
                if not pairs:
                    raise RuntimeError("kuaimai_cookie_empty")

                parsed = urllib.parse.urlparse(page.url or "")
                erp_host = self.erp_host
                if parsed.scheme and parsed.netloc and "superboss.cc" in parsed.netloc:
                    erp_host = f"{parsed.scheme}://{parsed.netloc}"
                ua = str(await page.evaluate("() => navigator.userAgent")).strip()
                return {
                    "erp_host": erp_host,
                    "page_url": page.url or "",
                    "cookie": ";".join(pairs),
                    "user_agent": ua,
                }
            finally:
                await browser.close()

    async def _do_login(self, page: Any) -> None:
        company = page.locator("#login-company").first
        account = page.locator("#login-account").first
        password = page.locator("#login-password").first
        login_btn = page.locator("#login-btn").first

        if await company.count() == 0 or await account.count() == 0 or await password.count() == 0:
            raise RuntimeError("kuaimai_login_form_not_found")

        await company.fill("")
        await company.fill(self.company_name)
        await account.fill("")
        await account.fill(self.account)
        await password.fill("")
        await password.fill(self.password)

        checkbox = page.locator("div.icheckbox ins").first
        if await checkbox.count() > 0:
            try:
                await checkbox.click(timeout=700)
            except Exception:
                pass

        await login_btn.click(timeout=5000)

    def _is_logged_url(self, url: str) -> bool:
        value = (url or "").strip().lower()
        if not value:
            return False
        if "login" in value:
            return False
        return "superboss.cc/index.html" in value
