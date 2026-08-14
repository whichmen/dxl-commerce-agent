#!/usr/bin/env python3
"""Small CLI wrapper for worker health updates."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.worker_health import WorkerHealthReporter  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="worker health helper")
    parser.add_argument(
        "action",
        choices=("start", "ready", "manual_action", "degraded", "fatal", "stopped"),
    )
    parser.add_argument("detail", nargs="?", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reporter = WorkerHealthReporter.from_env(
        worker_key="",
        worker_kind="",
        store_id=os.environ.get("STORE_ID", ""),
        store_name=os.environ.get("STORE_NAME", ""),
    )
    detail = args.detail or args.action
    if args.action == "start":
        reporter.start(detail)
    elif args.action == "ready":
        reporter.ready(detail or "ready", progress=True)
    elif args.action == "manual_action":
        reporter.manual_action(detail)
    elif args.action == "degraded":
        reporter.degraded(detail)
    elif args.action == "fatal":
        reporter.fatal(detail)
    elif args.action == "stopped":
        reporter.stopped(detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
