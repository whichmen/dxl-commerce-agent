from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SyntheticFixtures:
    def __init__(self, fixture_dir: Path) -> None:
        self.orders = self._load(fixture_dir / "orders.json")
        self.shipments = self._load(fixture_dir / "shipments.json")
        self.products = self._load(fixture_dir / "products.json")
        self.evidence = self._load(fixture_dir / "evidence.json")

    @staticmethod
    def _load(path: Path) -> list[dict[str, Any]]:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, list):
            raise ValueError(f"Fixture must be a JSON list: {path.name}")
        return data
