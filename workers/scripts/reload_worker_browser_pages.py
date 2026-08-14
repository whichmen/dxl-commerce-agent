#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reload customer-service pages for configured browser workers."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from playwright.async_api import Browser, Page, async_playwright


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("NODE_NO_WARNINGS", "1")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worker_watchdog import BROWSER_KINDS, DEFAULT_CONFIG_PATH, load_workers  # noqa: E402


INTERNAL_URL_PREFIXES = (
    "about:",
    "chrome:",
    "chrome-untrusted:",
    "devtools:",
)


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _log(message: str) -> None:
    print(f"[{_ts()}] {message}", flush=True)


def _same_target_path(page_url: str, target_url: str) -> bool:
    if not page_url or not target_url:
        return False
    if page_url.startswith(target_url):
        return True
    page = urlparse(page_url)
    target = urlparse(target_url)
    if not page.netloc or not target.netloc:
        return False
    if page.netloc != target.netloc:
        return False
    target_path = target.path.rstrip("/")
    if not target_path:
        return True
    return page.path.rstrip("/").startswith(target_path)


def _is_internal_page(url: str) -> bool:
    normalized = str(url or "").strip().lower()
    return not normalized or normalized.startswith(INTERNAL_URL_PREFIXES)


async def _pick_target_page(browser: Browser, *, target_url: str) -> Page | None:
    pages: list[Page] = []
    for context in browser.contexts:
        pages.extend(context.pages)

    for page in pages:
        if _same_target_path(page.url or "", target_url):
            return page

    useful = [page for page in pages if not _is_internal_page(page.url or "")]
    if len(useful) == 1:
        return useful[0]
    return None


def _disconnect_browser(browser: Browser) -> None:
    connection = getattr(browser, "_connection", None)
    if connection and hasattr(connection, "close"):
        try:
            connection.close()
            return
        except Exception:
            pass


async def _reload_one(
    *,
    key: str,
    kind: str,
    store_name: str,
    port: int,
    target_url: str,
    timeout_sec: float,
    dry_run: bool,
) -> bool:
    endpoint = f"http://127.0.0.1:{port}"
    async with async_playwright() as playwright:
        browser: Browser | None = None
        try:
            browser = await playwright.chromium.connect_over_cdp(endpoint)
            page = await _pick_target_page(browser, target_url=target_url)
            if page is None:
                _log(f"[WARN][{key}] 未找到客服页 kind={kind} store={store_name} port={port}")
                return False
            if dry_run:
                _log(f"[DRY][{key}] would reload url={page.url}")
                return True
            await page.reload(wait_until="domcontentloaded", timeout=int(timeout_sec * 1000))
            _log(f"[OK][{key}] reloaded store={store_name} port={port} url={page.url}")
            return True
        except Exception as exc:
            _log(f"[WARN][{key}] reload failed: {type(exc).__name__}: {exc}")
            return False
        finally:
            if browser is not None:
                _disconnect_browser(browser)


async def _run(args: argparse.Namespace) -> int:
    workers = load_workers(Path(args.config).expanduser().resolve())
    targets = [worker for worker in workers if worker.kind in BROWSER_KINDS]
    if args.kinds:
        wanted = {item.strip() for item in args.kinds.split(",") if item.strip()}
        targets = [worker for worker in targets if worker.kind in wanted]

    if not targets:
        _log("[WARN] no browser workers found")
        return 0

    _log(f"[INFO] reload start, workers={len(targets)}, stagger={args.stagger_sec:.1f}s")
    ok_count = 0
    fail_count = 0
    for idx, worker in enumerate(targets):
        if idx > 0 and args.stagger_sec > 0:
            await asyncio.sleep(args.stagger_sec)
        env = worker.env
        ok = await _reload_one(
            key=worker.key,
            kind=worker.kind,
            store_name=str(env.get("STORE_NAME") or ""),
            port=int(env.get("REMOTE_PORT") or "0"),
            target_url=str(env.get("TARGET_URL") or ""),
            timeout_sec=args.timeout_sec,
            dry_run=args.dry_run,
        )
        if ok:
            ok_count += 1
        else:
            fail_count += 1

    _log(f"[INFO] reload done, ok={ok_count}, failed={fail_count}")
    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="reload configured kefu worker browser pages")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="workers.watchdog.conf path")
    parser.add_argument(
        "--stagger-sec",
        type=float,
        default=float(os.environ.get("WORKER_BROWSER_RELOAD_STAGGER_SEC", "30")),
        help="seconds to wait between workers",
    )
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=float(os.environ.get("WORKER_BROWSER_RELOAD_TIMEOUT_SEC", "30")),
        help="page reload timeout seconds",
    )
    parser.add_argument(
        "--kinds",
        default=os.environ.get("WORKER_BROWSER_RELOAD_KINDS", ""),
        help="optional comma-separated kind filter, e.g. weixin,pdd",
    )
    parser.add_argument("--dry-run", action="store_true", help="only print matched pages")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    return asyncio.run(_run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
