"""End-to-end tests against the running docker compose stack.

Skipped when the gateway is not reachable at GATEWAY_URL.
"""
import os
from unittest import SkipTest, TestCase

import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")


class GatewayE2ETests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = httpx.Client(base_url=GATEWAY_URL, timeout=10, trust_env=False)
        try:
            cls.client.get("/health/").raise_for_status()
        except httpx.HTTPError:
            cls.client.close()
            raise SkipTest(f"Gateway is not running at {GATEWAY_URL}")

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_get_users_forwards_path_and_query(self):
        response = self.client.get("/api/users/1?tag=a&tag=b&q=hello world")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "service": "user-service",
            "method": "GET",
            "path": "/v1/users/1/",
            "query_params": {"tag": ["a", "b"], "q": ["hello world"]},
        })

    def test_get_orders(self):
        response = self.client.get("/api/orders/42/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "order-service")
        self.assertEqual(response.json()["path"], "/v1/orders/42/")

    def test_post_orders_forwards_json_body(self):
        body = {"user_id": 1, "items": [{"sku": "a", "qty": 2}]}

        response = self.client.post("/api/orders/?source=web", json=body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/json")
        data = response.json()
        self.assertEqual(data["method"], "POST")
        self.assertEqual(data["body"], body)
        self.assertEqual(data["query_params"], {"source": ["web"]})

    def test_upstream_error_status_is_forwarded(self):
        response = self.client.get("/api/users/not-a-number/")

        self.assertEqual(response.status_code, 404)
        self.assertIn("/v1/users/not-a-number/", response.text)

    def test_unknown_service_returns_404(self):
        response = self.client.get("/api/unknown/")

        self.assertEqual(response.status_code, 404)

    def test_slow_upstream_returns_504(self):
        response = self.client.get("/api/users/slow/")

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json(), {"detail": "Upstream timeout"})
