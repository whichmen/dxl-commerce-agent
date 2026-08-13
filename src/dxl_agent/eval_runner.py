"""Installed entry point for the repository's synthetic evaluation suite."""

from __future__ import annotations

import sys
from pathlib import Path


def _asset_root() -> Path:
    module_path = Path(__file__).resolve()
    source_checkout = module_path.parents[2]
    if (source_checkout / "evals" / "cases.jsonl").is_file():
        return source_checkout
    installed_wheel = module_path.parents[1]
    if (installed_wheel / "evals" / "cases.jsonl").is_file():
        return installed_wheel
    raise RuntimeError("Synthetic evaluation assets are not installed")


def main(argv: list[str] | None = None) -> int:
    root = _asset_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from evals.runner import main as eval_main

    arguments = list(argv or sys.argv[1:])
    if "--project-root" not in arguments:
        arguments = ["--project-root", str(root), *arguments]
    return eval_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
