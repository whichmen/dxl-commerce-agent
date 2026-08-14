from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from kefu_identity_service.app import create_app
from kefu_identity_service.store import IdentityStore


class IdentityServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_upsert_lookup_and_blacklist_protocol(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dxl-identity-test-") as temp_dir:
            store = IdentityStore(Path(temp_dir) / "identity.db")
            self.addCleanup(store.close)
            with patch.dict("os.environ", {"IDENTITY_API_KEY": "identity-test-key"}):
                app = create_app(store)
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Api-Key": "identity-test-key"}
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                denied = await client.post(
                    "/statistic/lookup_user_identity",
                    json={
                        "org_id": "org_example",
                        "platform": "抖音",
                        "room_name": "示例抖音店铺",
                        "alias_value": "guest_example",
                    },
                )
                saved = await client.post(
                    "/admin/identities",
                    headers=headers,
                    json={
                        "org_id": "org_example",
                        "platform": "抖音",
                        "room_name": "示例抖音店铺",
                        "platform_nickname": "guest_example",
                        "km_name": "buyer_example",
                        "dxl_nickname": "customer_example",
                        "km_identity_type": "mapped",
                        "last_tid": "ORDER-EXAMPLE-001",
                    },
                )
                resolved = await client.post(
                    "/statistic/lookup_user_identity",
                    headers=headers,
                    json={
                        "org_id": "org_example",
                        "platform": "抖音",
                        "room_name": "示例抖音店铺",
                        "alias_value": "guest_example",
                    },
                )
                added = await client.post(
                    "/zhibo/add_black_house",
                    headers=headers,
                    json={"org_id": "org_example", "wangwang_id": "buyer_example", "days": 7},
                )
                removed = await client.post(
                    "/zhibo/black_house_delete",
                    headers=headers,
                    json={"org_id": "org_example", "wangwang_id": "buyer_example"},
                )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(saved.json()["message"], "identity_saved")
        self.assertEqual(resolved.json()["content"]["result"], "resolved")
        self.assertEqual(resolved.json()["content"]["items"][0]["km_name"], "buyer_example")
        self.assertEqual(added.json()["message"], "blacklist_added")
        self.assertTrue(removed.json()["removed"])


if __name__ == "__main__":
    unittest.main()
