#!/usr/bin/env python3
"""CLI wrapper for decision failure/recovery alerts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.decision_alert import DecisionAlertNotifier  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="decision alert helper")
    parser.add_argument("action", choices=("failure", "recovery"))
    parser.add_argument("--platform", default="", help="worker platform kind")
    parser.add_argument("--store-id", default="", help="store id")
    parser.add_argument("--store-name", default="", help="store name")
    parser.add_argument("--detail", default="", help="detail message")
    parser.add_argument("--session-key", default="", help="session key")
    parser.add_argument("--platform-nickname", default="", help="customer nickname")
    parser.add_argument("--message-id", default="", help="message id")
    parser.add_argument("--message-type", default="", help="message type")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    notifier = DecisionAlertNotifier.from_env(
        platform=args.platform,
        store_id=args.store_id,
        store_name=args.store_name,
    )
    if args.action == "failure":
        ok, result = notifier.record_failure(
            detail=args.detail,
            session_key=args.session_key,
            platform_nickname=args.platform_nickname,
            message_id=args.message_id,
            message_type=args.message_type,
        )
    else:
        ok, result = notifier.record_recovery(detail=args.detail or "decision_api_ok")
    print(f"ok={int(bool(ok))} result={result}")
    return 0 if ok or result in {"already_notified", "normal", "issue_not_notified", "retry_later"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
