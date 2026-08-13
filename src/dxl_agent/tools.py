from __future__ import annotations

from copy import deepcopy
from typing import Any

from .fixtures import SyntheticFixtures


class CommerceTools:
    """Strictly scoped tools backed only by synthetic fixtures."""

    def __init__(self, fixtures: SyntheticFixtures) -> None:
        self.fixtures = fixtures

    def order_lookup(
        self,
        *,
        tenant_id: str,
        store_id: str,
        customer_id: str,
        order_id: str | None = None,
    ) -> list[dict[str, Any]]:
        matches = [
            order
            for order in self.fixtures.orders
            if order["tenant_id"] == tenant_id
            and order["store_id"] == store_id
            and order["customer_id"] == customer_id
            and (order_id is None or order["order_id"] == order_id)
        ]
        return deepcopy(matches)

    def logistics_lookup(self, *, order_id: str) -> dict[str, Any] | None:
        shipment = next(
            (item for item in self.fixtures.shipments if item["order_id"] == order_id),
            None,
        )
        return deepcopy(shipment)

    def product_lookup(
        self, *, tenant_id: str, store_id: str, sku: str | None
    ) -> list[dict[str, Any]]:
        if sku is None:
            return []
        return deepcopy(
            [
                item
                for item in self.fixtures.products
                if item["tenant_id"] == tenant_id
                and item["store_id"] == store_id
                and item["sku"] == sku
            ]
        )

    def evidence_lookup(
        self,
        *,
        tenant_id: str,
        store_id: str,
        customer_id: str,
        order_id: str,
        evidence_ids: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        requested = set(evidence_ids)
        return deepcopy(
            [
                item
                for item in self.fixtures.evidence
                if item["evidence_id"] in requested
                and item["tenant_id"] == tenant_id
                and item["store_id"] == store_id
                and item["customer_id"] == customer_id
                and item["order_id"] == order_id
                and item["scan_status"] == "clean"
                and item["review_status"] == "verified"
            ]
        )
