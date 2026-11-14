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
            "query_params": {"status": "shipped"},
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
            "query_params": {"commit": "true"},
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
