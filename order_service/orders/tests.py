from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase, override_settings


@override_settings(ALLOWED_HOSTS=["testserver"])
class OrderViewsetTests(SimpleTestCase):
    order_retrieve_url = "/v1/orders/1/"
    order_create_url = "/v1/orders/"

    async def test_get_echoes_request_details(self):
        response = await self.async_client.get(self.order_retrieve_url, 
                                               query_params={"status": "shipped"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertEqual(response.json(), {
            "service": "order-service",
            "method": "GET",
            "path": self.order_retrieve_url,
            "query_params": {"status": ["shipped"]},
        })

    async def test_post_echoes_request_details(self):
        response = await self.async_client.post(self.order_create_url, 
                                                data={"user_id": 23}, 
                                                query_params={"commit": "true"})
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertEqual(response.json(), {
            "service": "order-service",
            "method": "POST",
            "path": self.order_create_url,
            "body": {"user_id": "23"},
            "query_params": {"commit": ["true"]},
        })

    async def test_disallowed_methods(self):
        for method in ("put", "patch", "delete"):
            with self.subTest(method=method):
                response = await getattr(self.async_client, method)(
                    self.order_create_url, data="{}", content_type="application/json"
                )

                self.assertEqual(response.status_code, 405)
                self.assertIn("detail", response.json())
                self.assertNotIn(method.upper(), response.headers["Allow"])


@override_settings(ALLOWED_HOSTS=["testserver"])
class SlowResponseTests(SimpleTestCase):
    url = "/v1/orders/slow/"

    async def test_waits_for_requested_seconds(self):
        with patch("orders.views.asyncio.sleep", new_callable=AsyncMock) as sleep:
            response = await self.async_client.get(self.url, {"seconds": "2.5"})

        sleep.assert_awaited_once_with(2.5)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "service": "order-service",
            "slow": True,
            "delayed_seconds": 2.5,
        })

    async def test_invalid_seconds_returns_400(self):
        for seconds in ("abc", "-1", "31"):
            with self.subTest(seconds=seconds):
                with patch("orders.views.asyncio.sleep", new_callable=AsyncMock) as sleep:
                    response = await self.async_client.get(self.url, {"seconds": seconds})

                sleep.assert_not_awaited()
                self.assertEqual(response.status_code, 400)
                self.assertIn("detail", response.json())


@override_settings(ALLOWED_HOSTS=["testserver"])
class ErrorResponseTests(SimpleTestCase):
    url = "/v1/orders/error/"

    async def test_returns_requested_error_status(self):
        for code in (500, 503):
            with self.subTest(code=code):
                response = await self.async_client.get(self.url, {"error_code": code})

                self.assertEqual(response.status_code, code)
                self.assertEqual(response.json(), {
                    "service": "order-service",
                    "error": True,
                })

    async def test_invalid_error_code_returns_400(self):
        for params in ({}, {"error_code": "404"}, {"error_code": "abc"}):
            with self.subTest(params=params):
                response = await self.async_client.get(self.url, params)

                self.assertEqual(response.status_code, 400)
                self.assertIn("detail", response.json())
